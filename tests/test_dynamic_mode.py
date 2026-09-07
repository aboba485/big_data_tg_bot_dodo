from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.documentation import DocumentationLoader, DocumentationRepository
from app.documentation.repository import build_candidate
from app.dodo.client import DodoApiClient
from app.dodo.executor import DodoExecutor
from app.dodo.profile import ROOT_COLLECTION, ProfileResolver
from app.errors import DodoValidationError, PlanValidationError
from app.planner.schemas import (
    AggregationType,
    DynamicAggregation,
    Granularity,
    OperationArgument,
    PlanMode,
    PlanStatus,
    ReportPlan,
)
from app.planner.validator import ReportPlanValidator
from app.reports.metric_registry import MetricRegistry
from app.reports.service import ReportAggregator
from app.storage.sqlite import SQLiteDatabase
from tests.conftest import UNIT_ID


@pytest.fixture
def repository(settings) -> DocumentationRepository:
    repo = DocumentationRepository(
        SQLiteDatabase(settings.sqlite_path),
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
        index_all_get=True,
    )
    repo.ensure_index()
    return repo


@pytest.fixture
def dynamic_settings(settings) -> Settings:
    return settings.model_copy(
        update={
            "allow_all_get_operations": True,
            "dodo_mock_mode": False,
        }
    )


def _dynamic_plan(**kwargs: Any) -> ReportPlan:
    defaults: dict[str, Any] = {
        "status": PlanStatus.READY,
        "mode": PlanMode.DYNAMIC,
        "operation_ids": ["get-delivery-statistics"],
        "date_from": date(2026, 5, 1),
        "date_to": date(2026, 5, 3),
        "unit_references": [UNIT_ID],
        "dynamic_aggregation": DynamicAggregation(
            value_field="deliverySales",
            aggregation=AggregationType.SUM,
            collection="unitsStatistics",
        ),
    }
    defaults.update(kwargs)
    return ReportPlan(**defaults)


def _executor(repository, client, dynamic_settings) -> DodoExecutor:
    return DodoExecutor(
        client,
        repository,
        MetricRegistry(),
        dynamic_settings.endpoint_overrides,
        dynamic_settings.allowed_operations,
        allow_all_get=True,
    )


def _transport(payload: Any, recorder: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(request)
        return httpx.Response(200, request=request, json=payload)

    return httpx.MockTransport(handler)


async def _client(
    transport: httpx.MockTransport, dynamic_settings: Settings, **kwargs: Any
) -> DodoApiClient:
    http_client = httpx.AsyncClient(transport=transport)
    return DodoApiClient(
        http_client,
        access_token="secret",
        country_id="ru",
        allowed_operations=dynamic_settings.allowed_operations,
        max_retries=0,
        **kwargs,
    )


# --- client ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_allowlist_blocks_uncurated_by_default(repository, dynamic_settings) -> None:
    operation = repository.get("get-cancelled-sales")
    client = await _client(_transport({}), dynamic_settings)
    async with client.http_client:
        with pytest.raises(DodoValidationError, match="не разрешена"):
            await client.request_operation(operation, {})


@pytest.mark.asyncio
async def test_allow_all_get_permits_uncurated(repository, dynamic_settings) -> None:
    operation = repository.get("get-cancelled-sales")
    client = await _client(_transport({"ok": True}), dynamic_settings, allow_all_get=True)
    async with client.http_client:
        assert await client.request_operation(operation, {}) == {"ok": True}


@pytest.mark.asyncio
async def test_path_parameters_are_substituted(repository, dynamic_settings) -> None:
    operation = repository.get("get-manufacture-orders")
    requests: list[httpx.Request] = []
    client = await _client(
        _transport({"orders": []}, requests), dynamic_settings, allow_all_get=True
    )
    async with client.http_client:
        await client.request_operation(operation, {}, {"manufactureId": "m-42"})
    assert requests[0].url.path.endswith("/manufactures/m-42/orders")
    assert "{" not in str(requests[0].url)


@pytest.mark.asyncio
async def test_root_array_response_is_wrapped(repository, dynamic_settings) -> None:
    operation = repository.get("get-roles-list")
    client = await _client(_transport([{"id": 1}]), dynamic_settings, allow_all_get=True)
    async with client.http_client:
        assert await client.request_operation(operation, {}) == {ROOT_COLLECTION: [{"id": 1}]}


# --- executor -------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_dynamic_binds_units_and_period(repository, dynamic_settings) -> None:
    requests: list[httpx.Request] = []
    payload = {"unitsStatistics": [{"deliverySales": 100, "unitId": UNIT_ID}]}
    client = await _client(_transport(payload, requests), dynamic_settings, allow_all_get=True)
    async with client.http_client:
        records = await _executor(repository, client, dynamic_settings).execute(
            _dynamic_plan(), [UNIT_ID]
        )
    assert records["get-delivery-statistics"][0]["deliverySales"] == 100
    query = requests[0].url.params
    assert query["units"] == UNIT_ID
    assert query["from"].startswith("2026-05-01")
    assert query["to"].startswith("2026-05-03")


@pytest.mark.asyncio
async def test_curated_execution_still_uses_hand_tuned_limits(repository, dynamic_settings) -> None:
    """The 10-day cap for daily sales is not derivable and must survive."""
    requests: list[httpx.Request] = []
    client = await _client(
        _transport({"result": []}, requests), dynamic_settings, allow_all_get=True
    )
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["sales"],
        operation_ids=["get-finances-sales-daily-units"],
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 25),
    )
    async with client.http_client:
        await _executor(repository, client, dynamic_settings).execute(plan, [UNIT_ID])
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_raw_mode_executes_uncurated_get_with_pagination(
    repository, dynamic_settings
) -> None:
    requests: list[httpx.Request] = []
    payload = {
        "consumption": [
            {
                "unitdId": UNIT_ID,
                "fromLocal": "2026-05-01T10:00:00",
                "toLocal": "2026-05-01T11:00:00",
                "doughSize": 30,
                "quantity": 12.5,
                "measurementUnit": "Kilogram",
            }
        ],
        "isEndOfListReached": True,
    }
    client = await _client(_transport(payload, requests), dynamic_settings, allow_all_get=True)
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode=PlanMode.RAW,
        operation_ids=["get-dough-consumption"],
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 2),
        unit_references=[UNIT_ID],
        raw_collection="consumption",
    )
    async with client.http_client:
        records = await _executor(repository, client, dynamic_settings).execute(plan, [UNIT_ID])
    assert records["get-dough-consumption"][0]["quantity"] == 12.5
    assert requests[0].url.params["units"] == UNIT_ID
    assert requests[0].url.params["fromDate"] == "2026-05-01"
    assert requests[0].url.params["take"] == "1000"


@pytest.mark.asyncio
async def test_raw_mode_binds_operation_path_arguments(repository, dynamic_settings) -> None:
    requests: list[httpx.Request] = []
    client = await _client(
        _transport({"orders": []}, requests), dynamic_settings, allow_all_get=True
    )
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode=PlanMode.RAW,
        operation_ids=["get-manufacture-orders"],
        raw_collection="orders",
        operation_arguments=[
            OperationArgument(name="manufactureId", value="m-42", location="path"),
            OperationArgument(
                name="modifiedAt",
                value="2026-05-01T00:00:00",
                location="query",
            ),
        ],
    )
    async with client.http_client:
        await _executor(repository, client, dynamic_settings).execute(plan, [])
    assert requests[0].url.path.endswith("/manufactures/m-42/orders")
    assert requests[0].url.params["modifiedAt"] == "2026-05-01T00:00:00"


# --- validator ------------------------------------------------------------


def _validator(repository, dynamic_settings) -> ReportPlanValidator:
    return ReportPlanValidator(
        MetricRegistry(),
        repository,
        dynamic_settings.allowed_operations,
        allow_all_get=True,
        profiles=ProfileResolver(dynamic_settings.endpoint_overrides),
    )


def _candidates(repository, *operation_ids: str):
    return [build_candidate(repository.get(item), 1.0) for item in operation_ids]


def test_validator_accepts_dynamic_plan(repository, dynamic_settings) -> None:
    plan = _dynamic_plan()
    validated = _validator(repository, dynamic_settings).validate(
        plan, _candidates(repository, "get-delivery-statistics"), [UNIT_ID]
    )
    assert validated.mode == PlanMode.DYNAMIC


def test_dynamic_plan_without_dates_asks_for_period(repository, dynamic_settings) -> None:
    plan = _dynamic_plan(date_from=None, date_to=None)
    validated = _validator(repository, dynamic_settings).validate(
        plan, _candidates(repository, "get-delivery-statistics"), [UNIT_ID]
    )
    assert validated.status == PlanStatus.NEEDS_CLARIFICATION
    assert validated.clarification_question == "За какой период нужен отчёт?"
    assert validated.mode == PlanMode.DYNAMIC


def test_validator_normalizes_documented_dynamic_field_path(repository, dynamic_settings) -> None:
    plan = _dynamic_plan(
        operation_ids=["get-dough-consumption"],
        dynamic_aggregation=DynamicAggregation(
            value_field="consumption[].quantity",
            aggregation=AggregationType.SUM,
        ),
        granularity=Granularity.HOUR,
    )
    validated = _validator(repository, dynamic_settings).validate(
        plan, _candidates(repository, "get-dough-consumption"), [UNIT_ID]
    )
    assert validated.dynamic_aggregation.value_field == "quantity"
    assert validated.dynamic_aggregation.collection == "consumption"


def test_validator_accepts_raw_get_with_arguments(repository, dynamic_settings) -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode=PlanMode.RAW,
        operation_ids=["get-products-id"],
        operation_arguments=[OperationArgument(name="id", value="product-42", location="path")],
    )
    validated = _validator(repository, dynamic_settings).validate(
        plan, _candidates(repository, "get-products-id")
    )
    assert validated.mode == PlanMode.RAW


def test_query_intent_safely_aggregates_single_monetary_field(repository, dynamic_settings) -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode=PlanMode.RAW,
        operation_ids=["get-staff-meals"],
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 10),
        unit_references=[UNIT_ID],
        raw_collection="staffMeals",
    )
    resolved = _validator(repository, dynamic_settings).apply_query_intent(
        plan, "Сколько денег потрачено на питание команды?"
    )
    assert resolved.mode == PlanMode.DYNAMIC
    assert resolved.dynamic_aggregation.value_field == "price"
    assert resolved.dynamic_aggregation.aggregation == AggregationType.SUM


def test_query_intent_keeps_detailed_request_raw(repository, dynamic_settings) -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode=PlanMode.RAW,
        operation_ids=["get-staff-meals"],
        raw_collection="staffMeals",
    )
    resolved = _validator(repository, dynamic_settings).apply_query_intent(
        plan, "Покажи все заказы питания команды"
    )
    assert resolved.mode == PlanMode.RAW


def test_validator_requests_missing_required_raw_argument(repository, dynamic_settings) -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode=PlanMode.RAW,
        operation_ids=["get-products-id"],
    )
    validated = _validator(repository, dynamic_settings).validate(
        plan, _candidates(repository, "get-products-id")
    )
    assert validated.status == PlanStatus.NEEDS_CLARIFICATION
    assert "id" in validated.clarification_question


def test_validator_can_prepare_every_indexed_get(repository, dynamic_settings) -> None:
    validator = _validator(repository, dynamic_settings)
    profiles = ProfileResolver(dynamic_settings.endpoint_overrides)
    for endpoint in repository.list():
        profile = profiles.resolve(endpoint)
        auto_bound = {
            profile.units_parameter,
            profile.from_parameter,
            profile.to_parameter,
            profile.skip_parameter if profile.pagination else None,
            profile.take_parameter if profile.pagination else None,
            *profile.settings_parameters.keys(),
        }
        arguments = [
            OperationArgument(
                name=parameter.name,
                value=str(parameter.enum[0]) if parameter.enum else "test-value",
                location=parameter.location or "query",
            )
            for parameter in endpoint.parameters
            if parameter.required and parameter.name not in auto_bound
        ]
        plan = ReportPlan(
            status=PlanStatus.READY,
            mode=PlanMode.RAW,
            operation_ids=[endpoint.operation_id],
            date_from=date(2026, 5, 1) if profile.has_period else None,
            date_to=date(2026, 5, 2) if profile.has_period else None,
            unit_references=[UNIT_ID] if profile.has_units else [],
            operation_arguments=arguments,
            raw_collection=(
                profile.collection_candidates[0]
                if profile.collection_candidates
                else profile.collection
            ),
        )
        validator.validate(
            plan,
            _candidates(repository, endpoint.operation_id),
            [UNIT_ID] if profile.has_units else [],
        )


def test_every_indexed_get_without_required_arguments_returns_a_status(
    repository, dynamic_settings
) -> None:
    validator = _validator(repository, dynamic_settings)
    profiles = ProfileResolver(dynamic_settings.endpoint_overrides)
    for endpoint in repository.list():
        profile = profiles.resolve(endpoint)
        plan = ReportPlan(
            status=PlanStatus.READY,
            mode=PlanMode.RAW,
            operation_ids=[endpoint.operation_id],
            date_from=date(2026, 5, 1) if profile.has_period else None,
            date_to=date(2026, 5, 2) if profile.has_period else None,
            unit_references=[UNIT_ID] if profile.has_units else [],
            raw_collection=(
                profile.collection_candidates[0]
                if profile.collection_candidates
                else profile.collection
            ),
        )
        validated = validator.validate(
            plan,
            _candidates(repository, endpoint.operation_id),
            [UNIT_ID] if profile.has_units else [],
        )
        assert validated.status in {
            PlanStatus.READY,
            PlanStatus.NEEDS_CLARIFICATION,
        }


def test_validator_rejects_operation_outside_candidates(repository, dynamic_settings) -> None:
    with pytest.raises(PlanValidationError, match="вне списка кандидатов"):
        _validator(repository, dynamic_settings).validate(_dynamic_plan(), [], [UNIT_ID])


def test_validator_rejects_missing_dynamic_aggregation(repository, dynamic_settings) -> None:
    plan = _dynamic_plan(dynamic_aggregation=None)
    with pytest.raises(PlanValidationError, match="dynamic_aggregation"):
        _validator(repository, dynamic_settings).validate(
            plan, _candidates(repository, "get-delivery-statistics"), [UNIT_ID]
        )


def test_validator_falls_back_to_raw_for_unknown_value_field(repository, dynamic_settings) -> None:
    plan = _dynamic_plan(
        dynamic_aggregation=DynamicAggregation(
            value_field="totallyMadeUpField",
            aggregation=AggregationType.SUM,
            collection="unitsStatistics",
        )
    )
    validated = _validator(repository, dynamic_settings).validate(
        plan, _candidates(repository, "get-delivery-statistics"), [UNIT_ID]
    )
    assert validated.mode == PlanMode.RAW
    assert validated.dynamic_aggregation is None
    assert validated.raw_collection == "unitsStatistics"


def test_validator_requires_weight_field_for_weighted_average(repository, dynamic_settings) -> None:
    plan = _dynamic_plan(
        dynamic_aggregation=DynamicAggregation(
            value_field="deliverySales",
            aggregation=AggregationType.WEIGHTED_AVERAGE,
            collection="unitsStatistics",
        )
    )
    with pytest.raises(PlanValidationError, match="weight_field"):
        _validator(repository, dynamic_settings).validate(
            plan, _candidates(repository, "get-delivery-statistics"), [UNIT_ID]
        )


def test_validator_requires_ratio_fields(repository, dynamic_settings) -> None:
    plan = _dynamic_plan(
        dynamic_aggregation=DynamicAggregation(
            value_field="deliverySales",
            aggregation=AggregationType.RATIO,
            collection="unitsStatistics",
        )
    )
    with pytest.raises(PlanValidationError, match=r"numerator_field|denominator_field"):
        _validator(repository, dynamic_settings).validate(
            plan, _candidates(repository, "get-delivery-statistics"), [UNIT_ID]
        )


def test_metric_plans_are_unaffected_by_dynamic_branch(repository, dynamic_settings) -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["sales"],
        operation_ids=["get-finances-sales-daily-units"],
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 10),
    )
    validated = _validator(repository, dynamic_settings).validate(
        plan, _candidates(repository, "get-finances-sales-daily-units"), [UNIT_ID]
    )
    assert validated.mode == PlanMode.METRICS


# --- aggregator -----------------------------------------------------------


def test_aggregator_handles_dynamic_sum() -> None:
    plan = _dynamic_plan()
    records = {
        "get-delivery-statistics": [
            {"deliverySales": 100, "unitId": UNIT_ID},
            {"deliverySales": 200, "unitId": UNIT_ID},
        ]
    }
    aggregator = ReportAggregator(MetricRegistry())
    result = aggregator.aggregate(plan, records)
    assert result.totals["deliverySales"] == 300


def test_aggregator_handles_dynamic_count() -> None:
    plan = _dynamic_plan(
        dynamic_aggregation=DynamicAggregation(
            value_field="deliverySales",
            aggregation=AggregationType.COUNT,
            collection="unitsStatistics",
        )
    )
    records = {
        "get-delivery-statistics": [
            {"deliverySales": 100, "unitId": UNIT_ID},
            {"deliverySales": 200, "unitId": UNIT_ID},
            {"deliverySales": 300, "unitId": UNIT_ID},
        ]
    }
    aggregator = ReportAggregator(MetricRegistry())
    result = aggregator.aggregate(plan, records)
    assert result.totals["deliverySales"] == 3


def test_aggregator_handles_dynamic_weighted_average() -> None:
    plan = _dynamic_plan(
        dynamic_aggregation=DynamicAggregation(
            value_field="avgDeliveryTime",
            aggregation=AggregationType.WEIGHTED_AVERAGE,
            weight_field="deliveryOrdersCount",
            collection="unitsStatistics",
        )
    )
    records = {
        "get-delivery-statistics": [
            {"avgDeliveryTime": 10, "deliveryOrdersCount": 100, "unitId": UNIT_ID},
            {"avgDeliveryTime": 20, "deliveryOrdersCount": 100, "unitId": UNIT_ID},
        ]
    }
    aggregator = ReportAggregator(MetricRegistry())
    result = aggregator.aggregate(plan, records)
    assert result.totals["avgDeliveryTime"] == 15.0


def test_aggregator_groups_by_granularity() -> None:
    plan = _dynamic_plan(granularity=Granularity.DAY)
    records = {
        "get-delivery-statistics": [
            {"deliverySales": 100, "unitId": UNIT_ID, "date": "2026-05-01"},
            {"deliverySales": 200, "unitId": UNIT_ID, "date": "2026-05-01"},
            {"deliverySales": 300, "unitId": UNIT_ID, "date": "2026-05-02"},
        ]
    }
    aggregator = ReportAggregator(MetricRegistry())
    result = aggregator.aggregate(plan, records)
    assert len(result.rows) == 2
    assert result.totals["deliverySales"] == 600


def test_aggregator_groups_dynamic_rows_by_hour() -> None:
    plan = _dynamic_plan(
        operation_ids=["get-dough-consumption"],
        granularity=Granularity.HOUR,
        dynamic_aggregation=DynamicAggregation(
            value_field="quantity",
            aggregation=AggregationType.SUM,
            collection="consumption",
        ),
    )
    records = {
        "get-dough-consumption": [
            {"fromLocal": "2026-05-01T10:00:00", "quantity": 2.5},
            {"fromLocal": "2026-05-01T10:30:00", "quantity": 1.5},
            {"fromLocal": "2026-05-01T11:00:00", "quantity": 3.0},
        ]
    }
    result = ReportAggregator(MetricRegistry()).aggregate(plan, records)
    assert result.rows == [
        {"hour": "2026-05-01T10:00:00", "quantity": 4.0},
        {"hour": "2026-05-01T11:00:00", "quantity": 3.0},
    ]
    assert result.totals["quantity"] == 7.0


def test_aggregator_summary_mentions_dynamic() -> None:
    plan = _dynamic_plan()
    records = {"get-delivery-statistics": [{"deliverySales": 100, "unitId": UNIT_ID}]}
    aggregator = ReportAggregator(MetricRegistry())
    result = aggregator.aggregate(plan, records)
    assert "sum" in result.summary.lower()
    assert "deliverySales" in result.summary


def test_aggregator_returns_flat_raw_table() -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode=PlanMode.RAW,
        operation_ids=["get-dough-consumption"],
    )
    records = {
        "get-dough-consumption": [
            {
                "unitdId": UNIT_ID,
                "quantity": 12.5,
                "details": {"size": 30},
                "__period_start": "2026-05-01",
            }
        ]
    }
    result = ReportAggregator(MetricRegistry()).aggregate(plan, records)
    assert result.columns == ["unitdId", "quantity", "details.size"]
    assert result.rows[0]["details.size"] == 30
    assert result.totals == {"rows_count": 1}


def test_staff_meals_cost_metric_sums_prices() -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["staff_meals_cost"],
        operation_ids=["get-staff-meals"],
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 10),
    )
    records = {
        "get-staff-meals": [
            {"unitId": UNIT_ID, "price": 140},
            {"unitId": UNIT_ID, "price": 70},
            {"unitId": UNIT_ID, "price": 70},
        ]
    }
    result = ReportAggregator(MetricRegistry()).aggregate(plan, records)
    assert result.totals == {"staff_meals_cost": 280.0}
