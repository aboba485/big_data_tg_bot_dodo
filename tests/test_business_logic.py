from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise

import httpx
import pytest

from app.documentation import DocumentationLoader, DocumentationRepository
from app.documentation.models import EndpointCandidate
from app.dodo.chunker import chunk_dates, chunk_units, report_buckets
from app.dodo.client import DodoApiClient
from app.dodo.paginator import paginate
from app.dodo.units import UnitResolver
from app.errors import (
    DodoApiError,
    DodoForbiddenError,
    DodoUnauthorizedError,
    PlanValidationError,
)
from app.planner.mock import build_mock_plan
from app.planner.schemas import Granularity, PlanStatus, ReportFilter, ReportPlan
from app.planner.service import PlannerService
from app.planner.validator import ReportPlanValidator
from app.reports.aggregations import count_unique, ratio_from_sums, weighted_average
from app.reports.intervals import merge_intervals, stop_duration_hours
from app.reports.metric_registry import MetricRegistry
from app.reports.service import ReportAggregator
from app.retrieval.service import RetrievalService
from app.storage.sqlite import SQLiteDatabase
from tests.conftest import UNIT_ID


def _repository(settings) -> DocumentationRepository:
    repository = DocumentationRepository(
        SQLiteDatabase(settings.sqlite_path),
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
    )
    repository.ensure_index()
    return repository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "operation_id"),
    [
        ("выручка по дням", "get-finances-sales-daily-units"),
        ("выручка с доставки", "get-delivery-statistics"),
        ("среднее время доставки", "get-delivery-statistics"),
        ("новые клиенты", "get-orders-client-statistics"),
        ("стопы ингредиентов", "get-production-stop-sales-statistics-ingredients"),
        ("расход теста", "get-dough-consumption"),
        ("время выдачи заказа", "get-production-orders-handover-time-statistics"),
        ("деньги потрачено на питание команды", "get-staff-meals"),
    ],
)
async def test_retrieval_prioritizes_metric(settings, query: str, operation_id: str) -> None:
    service = RetrievalService(_repository(settings), MetricRegistry())
    result = await service.search_endpoints(query)
    assert result[0].operation_id == operation_id
    assert sum(len(item.compact_summary) for item in result) < 20_000


def test_mock_planner_ready_and_clarification() -> None:
    registry = MetricRegistry()
    ready = build_mock_plan(
        f"Покажи выручку по дням за май 2026 по юниту {UNIT_ID}",
        registry,
        today=date(2026, 7, 23),
        default_units=[],
    ).plan
    assert ready.status == PlanStatus.READY
    assert ready.date_from == date(2026, 5, 1)
    assert ready.granularity == Granularity.DAY
    clarification = build_mock_plan(
        f"Покажи выручку по юниту {UNIT_ID}",
        registry,
        today=date(2026, 7, 23),
        default_units=[],
    ).plan
    assert clarification.status == PlanStatus.NEEDS_CLARIFICATION


def test_metrics_plan_without_dates_asks_for_period(settings) -> None:
    repository = _repository(settings)
    validator = ReportPlanValidator(
        MetricRegistry(),
        repository,
        settings.allowed_operations,
        allow_all_get=True,
    )
    operation_id = "get-finances-sales-daily-units"
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["sales"],
        operation_ids=[operation_id],
        unit_references=[UNIT_ID],
    )
    validated = validator.validate(
        plan,
        [
            EndpointCandidate(
                operation_id=operation_id,
                score=1,
                compact_summary="sales",
            )
        ],
        [UNIT_ID],
    )
    assert validated.status == PlanStatus.NEEDS_CLARIFICATION
    assert validated.clarification_question == "За какой период нужен отчёт?"


def test_metrics_plan_with_only_date_from_asks_for_period(settings) -> None:
    repository = _repository(settings)
    validator = ReportPlanValidator(
        MetricRegistry(),
        repository,
        settings.allowed_operations,
        allow_all_get=True,
    )
    operation_id = "get-finances-sales-daily-units"
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["sales"],
        operation_ids=[operation_id],
        date_from=date(2026, 6, 1),
        unit_references=[UNIT_ID],
    )
    validated = validator.validate(
        plan,
        [
            EndpointCandidate(
                operation_id=operation_id,
                score=1,
                compact_summary="sales",
            )
        ],
        [UNIT_ID],
    )
    assert validated.status == PlanStatus.NEEDS_CLARIFICATION
    assert validated.clarification_question == "За какой период нужен отчёт?"


def test_report_filter_schema_rejects_unit_filter() -> None:
    with pytest.raises(ValueError):
        ReportFilter(name="unitName", values=["Подольск-1"])  # type: ignore[arg-type]


def test_allowed_report_filters_are_applied_before_aggregation() -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["sales"],
        operation_ids=["get-finances-sales-daily-units"],
        filters=[ReportFilter(name="salesChannel", values=["delivery"])],
    )
    result = ReportAggregator(MetricRegistry()).aggregate(
        plan,
        {
            "get-finances-sales-daily-units": [
                {"salesChannel": "Delivery", "sales": 100},
                {"salesChannel": "Dine-in", "sales": 200},
            ]
        },
    )

    assert result.totals["sales"] == 100


def test_filter_must_exist_in_every_selected_operation(settings) -> None:
    repository = _repository(settings)
    validator = ReportPlanValidator(
        MetricRegistry(),
        repository,
        settings.allowed_operations,
        allow_all_get=True,
    )
    operation_id = "get-delivery-statistics"
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["delivery_sales"],
        operation_ids=[operation_id],
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 2),
        unit_references=[UNIT_ID],
        filters=[ReportFilter(name="salesChannel", values=["Delivery"])],
    )

    with pytest.raises(PlanValidationError, match="salesChannel"):
        validator.validate(
            plan,
            [
                EndpointCandidate(
                    operation_id=operation_id,
                    score=1,
                    compact_summary="delivery",
                )
            ],
            [UNIT_ID],
        )


def test_filter_value_must_match_documented_enum(settings) -> None:
    repository = _repository(settings)
    validator = ReportPlanValidator(
        MetricRegistry(),
        repository,
        settings.allowed_operations,
        allow_all_get=True,
    )
    operation_id = "get-finances-sales-daily-units"
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["sales"],
        operation_ids=[operation_id],
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 2),
        unit_references=[UNIT_ID],
        filters=[ReportFilter(name="salesChannel", values=["Unknown channel"])],
    )

    with pytest.raises(PlanValidationError, match="неизвестные значения"):
        validator.validate(
            plan,
            [
                EndpointCandidate(
                    operation_id=operation_id,
                    score=1,
                    compact_summary="sales",
                )
            ],
            [UNIT_ID],
        )


def test_dynamic_plan_rejects_metric_fields(settings) -> None:
    repository = _repository(settings)
    validator = ReportPlanValidator(
        MetricRegistry(),
        repository,
        settings.allowed_operations,
        allow_all_get=True,
    )
    plan = ReportPlan(
        status=PlanStatus.READY,
        mode="dynamic",
        metric_ids=["sales"],
        operation_ids=["get-finances-sales-daily-units"],
        dynamic_aggregation={
            "value_field": "sales",
            "aggregation": "sum",
            "collection": "result",
        },
    )

    with pytest.raises(PlanValidationError, match="другого режима"):
        validator.validate(plan, [], [])


@pytest.mark.asyncio
async def test_openai_planner_reserves_enough_tokens_and_minimal_reasoning(settings) -> None:
    class FakeResponse:
        output_parsed = ReportPlan(
            status=PlanStatus.NEEDS_CLARIFICATION,
            clarification_question="За какой период нужен отчёт?",
        )
        usage = None

    class FakeResponses:
        def __init__(self) -> None:
            self.arguments = {}

        async def parse(self, **kwargs):
            self.arguments = kwargs
            return FakeResponse()

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    settings.openai_max_output_tokens = 1200
    settings.planner_mock_mode = False
    client = FakeClient()
    planner = PlannerService(settings, MetricRegistry(), client=client)
    await planner.create_plan(
        "Покажи выручку",
        [
            EndpointCandidate(
                operation_id="get-finances-sales-daily-units",
                score=1,
                compact_summary="sales",
            )
        ],
    )
    assert client.responses.arguments["max_output_tokens"] == 3000
    assert client.responses.arguments["reasoning"] == {"effort": "minimal"}


@pytest.mark.asyncio
async def test_openai_planner_derives_operation_and_fields_from_metric(settings) -> None:
    class FakeResponse:
        output_parsed = ReportPlan(
            status=PlanStatus.READY,
            metric_ids=["delivery_sales"],
            operation_ids=["get-finances-sales-daily-units"],
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 10),
            unit_references=["Смоленск-2"],
            granularity=Granularity.DAY,
            selected_response_fields=["sales"],
        )
        usage = None

    class FakeResponses:
        async def parse(self, **_kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    settings.planner_mock_mode = False
    planner = PlannerService(settings, MetricRegistry(), client=FakeClient())
    result = await planner.create_plan("Выручка с доставки", [])
    assert result.plan.operation_ids == ["get-delivery-statistics"]
    assert result.plan.selected_response_fields == ["unitId", "deliverySales"]


@pytest.mark.asyncio
async def test_openai_planner_prefers_single_known_metric_over_dynamic_plan(settings) -> None:
    class FakeResponse:
        output_parsed = ReportPlan(
            status=PlanStatus.READY,
            mode="dynamic",
            operation_ids=["get-production-ordershandovertime"],
            dynamic_aggregation={
                "value_field": "ordersHandoverTime[].orderHandoverTime",
                "aggregation": "average",
                "collection": "ordersHandoverTime",
            },
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 10),
            unit_references=[UNIT_ID],
        )
        usage = None

    class FakeResponses:
        async def parse(self, **_kwargs):
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    settings.planner_mock_mode = False
    planner = PlannerService(settings, MetricRegistry(), client=FakeClient())
    result = await planner.create_plan("Покажи время выдачи заказа", [])
    assert result.plan.mode == "metrics"
    assert result.plan.metric_ids == ["average_order_handover_time_seconds"]
    assert result.plan.operation_ids == ["get-production-orders-handover-time-statistics"]


@pytest.mark.asyncio
async def test_openai_planner_converts_unregistered_dynamic_plan_to_raw(settings) -> None:
    class FakeResponse:
        output_parsed = ReportPlan(
            status=PlanStatus.READY,
            mode="dynamic",
            operation_ids=["get-dough-consumption"],
            dynamic_aggregation={
                "value_field": "consumption[].madeUpField",
                "aggregation": "sum",
                "collection": "consumption[]",
            },
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 10),
            unit_references=[UNIT_ID],
            selected_response_fields=["consumption[].madeUpField"],
        )
        usage = None

    class FakeResponses:
        async def parse(self, **_kwargs):
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    settings.planner_mock_mode = False
    planner = PlannerService(settings, MetricRegistry(), client=FakeClient())
    result = await planner.create_plan("Покажи данные по расходованию", [])
    assert result.plan.mode == "raw"
    assert result.plan.dynamic_aggregation is None
    assert result.plan.raw_collection == "consumption"
    assert result.plan.selected_response_fields == []


@pytest.mark.parametrize(
    ("days", "maximum", "expected"),
    [(1, 10, 1), (10, 10, 1), (11, 10, 2), (31, 10, 4), (32, 31, 2)],
)
def test_date_chunking(days: int, maximum: int, expected: int) -> None:
    start = date(2024, 2, 1)
    result = chunk_dates(start, start + timedelta(days=days - 1), maximum)
    assert len(result) == expected
    assert result[0].start == start
    assert result[-1].end == start + timedelta(days=days - 1)
    for left, right in pairwise(result):
        assert left.end + timedelta(days=1) == right.start


def test_unit_and_calendar_buckets() -> None:
    assert [len(item) for item in chunk_units([str(index) for index in range(65)], 30)] == [
        30,
        30,
        5,
    ]
    weeks = report_buckets(date(2026, 5, 1), date(2026, 5, 15), Granularity.WEEK)
    assert weeks[0].end.weekday() == 6
    months = report_buckets(date(2024, 2, 28), date(2024, 3, 2), Granularity.MONTH)
    assert months[0].end == date(2024, 2, 29)


def test_unit_resolver_accepts_model_transliteration(settings) -> None:
    settings.unit_catalog_path.write_text(
        '{"units":[{"id":"' + UNIT_ID + '","name":"Смоленск-2"}]}',
        encoding="utf-8",
    )
    resolver = UnitResolver(
        settings.unit_catalog_path,
        settings.unit_aliases_path,
        default_unit_ids=[],
    )
    assert resolver.resolve(["Smolensk-2"]).unit_ids == [UNIT_ID]
    assert resolver.recognize_in_query("Выручка в Смоленск-2")[0].unit_id == UNIT_ID


def test_unit_names_include_alias_only_catalog(settings) -> None:
    settings.unit_aliases_path.write_text(
        '{"Смоленск-2":"' + UNIT_ID + '","smolensk":"' + UNIT_ID + '"}',
        encoding="utf-8",
    )
    resolver = UnitResolver(
        settings.unit_catalog_path,
        settings.unit_aliases_path,
        default_unit_ids=[],
    )

    assert resolver.names() == {UNIT_ID: "смоленск-2"}


@pytest.mark.asyncio
async def test_planner_does_not_ask_for_uuid_of_known_unit(settings) -> None:
    settings.unit_catalog_path.write_text(
        '{"units":[{"id":"' + UNIT_ID + '","name":"Смоленск-2"}]}',
        encoding="utf-8",
    )
    resolver = UnitResolver(
        settings.unit_catalog_path,
        settings.unit_aliases_path,
        default_unit_ids=[],
    )

    class FakeResponse:
        output_parsed = ReportPlan(
            status=PlanStatus.NEEDS_CLARIFICATION,
            clarification_question="Укажите UUID заведения Смоленск-2",
            metric_ids=["delivery_sales"],
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 10),
            granularity=Granularity.DAY,
        )
        usage = None

    class FakeResponses:
        async def parse(self, **_kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    settings.planner_mock_mode = False
    planner = PlannerService(
        settings,
        MetricRegistry(),
        client=FakeClient(),
        unit_resolver=resolver,
    )
    result = await planner.create_plan(
        "Покажи выручку с доставки в Смоленск-2",
        [],
    )
    assert result.plan.status == PlanStatus.READY
    assert result.plan.unit_references == [UNIT_ID]
    assert result.plan.clarification_question is None


def test_aggregations() -> None:
    rows = [{"sales": 100, "orders": 2}, {"sales": 200, "orders": 8}]
    assert ratio_from_sums(rows, "sales", "orders") == 30
    assert weighted_average(rows, "sales", "orders") == 180
    assert count_unique([{"id": 1}, {"id": 1}, {"id": 2}], "id") == 2
    assert ratio_from_sums(rows, "sales", "missing") is None


def test_stop_intervals_are_clipped_deduplicated_and_merged() -> None:
    start = datetime(2026, 5, 1)
    end = datetime(2026, 5, 2)
    rows = [
        {
            "id": "a",
            "startedAtLocal": "2026-04-30T23:00:00",
            "endedAtLocal": "2026-05-01T02:00:00",
        },
        {
            "id": "a",
            "startedAtLocal": "2026-04-30T23:00:00",
            "endedAtLocal": "2026-05-01T02:00:00",
        },
        {
            "id": "b",
            "startedAtLocal": "2026-05-01T01:00:00",
            "endedAtLocal": "2026-05-01T03:00:00",
        },
    ]
    assert stop_duration_hours(rows, start, end) == 3
    intervals = [
        (start, start + timedelta(hours=2)),
        (start + timedelta(hours=1), start + timedelta(hours=3)),
    ]
    assert len(merge_intervals(intervals)) == 1


@pytest.mark.asyncio
async def test_pagination_and_deduplication() -> None:
    pages = [
        {"items": [{"id": 1}, {"id": 2}], "done": False},
        {"items": [{"id": 2}, {"id": 3}], "done": True},
    ]

    async def fetch(_params):
        return pages.pop(0)

    result = await paginate(
        fetch,
        {"items_path": "items", "end_flag_path": "done", "page_size": 2},
        deduplication_field="id",
    )
    assert [item["id"] for item in result] == [1, 2, 3]


@pytest.mark.asyncio
async def test_pagination_rejects_empty_unfinished_page_by_default() -> None:
    calls = 0

    async def fetch(_params):
        nonlocal calls
        calls += 1
        return {"items": [], "done": False}

    with pytest.raises(DodoApiError, match="пустую страницу"):
        await paginate(fetch, {"items_path": "items", "end_flag_path": "done"})

    assert calls == 1


@pytest.mark.asyncio
async def test_pagination_recovers_when_retry_after_empty_page_has_data() -> None:
    pages = [
        {"items": [], "done": False},
        {"items": [{"id": 1}], "done": True},
    ]

    async def fetch(_params):
        return pages.pop(0)

    result = await paginate(
        fetch,
        {
            "items_path": "items",
            "end_flag_path": "done",
            "empty_page_is_end": True,
        },
    )

    assert result == [{"id": 1}]


@pytest.mark.asyncio
async def test_pagination_rejects_renamed_single_array_by_default() -> None:
    async def fetch(_params):
        return {"renamed": [{"id": 1}], "done": True}

    with pytest.raises(DodoApiError, match="коллекцию пагинации"):
        await paginate(fetch, {"items_path": "items", "end_flag_path": "done"})


@pytest.mark.asyncio
async def test_pagination_accepts_unambiguous_single_array_with_opt_in(caplog) -> None:
    async def fetch(_params):
        return {"renamed": [{"id": 1}], "done": True}

    with caplog.at_level("WARNING", logger="app.dodo.paginator"):
        result = await paginate(
            fetch,
            {
                "items_path": "items",
                "end_flag_path": "done",
                "single_array_collection_fallback": True,
            },
            operation_id="example",
        )

    assert result == [{"id": 1}]
    assert "used configured single-array fallback operation=example" in caplog.text
    assert "renamed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"items": None, "done": True},
        {"items": None, "renamed": [], "done": True},
        {"first": [], "second": [], "done": True},
        {"renamed": [], "metadata": {}, "done": True},
        {"renamed": [], "finished": True},
        {"renamed": [], "done": None},
    ],
)
async def test_single_array_fallback_rejects_ambiguous_or_malformed_payload(payload) -> None:
    async def fetch(_params):
        return payload

    with pytest.raises(DodoApiError, match="пагинации"):
        await paginate(
            fetch,
            {
                "items_path": "items",
                "end_flag_path": "done",
                "single_array_collection_fallback": True,
            },
        )


@pytest.mark.asyncio
async def test_pagination_logs_only_bounded_response_shape(caplog) -> None:
    payload = {
        "data": {"consumption": []},
        "unsafe key": "must-not-be-logged",
        "token": "also-must-not-be-logged",
    }

    async def fetch(_params):
        return payload

    with (
        caplog.at_level("WARNING", logger="app.dodo.paginator"),
        pytest.raises(DodoApiError) as error,
    ):
        await paginate(
            fetch,
            {"items_path": "consumption", "end_flag_path": "done"},
            operation_id="get-dough-consumption",
        )

    shape = error.value.details["response_shape"]
    assert "$.data:object" in shape
    assert "$.data.consumption:array" in shape
    assert "$.<redacted-key>:string" in shape
    assert "get-dough-consumption" in caplog.text
    assert "token" not in caplog.text
    assert "must-not-be-logged" not in caplog.text
    assert "also-must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_pagination_does_not_log_shape_for_valid_payload(caplog) -> None:
    async def fetch(_params):
        return {"items": [], "done": True}

    with caplog.at_level("WARNING", logger="app.dodo.paginator"):
        assert (
            await paginate(
                fetch,
                {"items_path": "items", "end_flag_path": "done"},
                operation_id="example",
            )
            == []
        )

    assert "shape mismatch" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"done": False},
        {"items": None, "done": False},
        {"items": {}, "done": False},
        {"items": [], "done": None},
        {"items": [], "done": 0},
    ],
)
async def test_pagination_rejects_malformed_payload(payload) -> None:
    async def fetch(_params):
        return payload

    with pytest.raises(DodoApiError, match="пагинации"):
        await paginate(
            fetch,
            {
                "items_path": "items",
                "end_flag_path": "done",
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "exception"),
    [(401, DodoUnauthorizedError), (403, DodoForbiddenError)],
)
async def test_dodo_client_maps_auth_errors(settings, status, exception) -> None:
    repository = _repository(settings)
    operation = repository.get("get-delivery-statistics")
    transport = httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DodoApiClient(
            http_client,
            access_token="secret",
            country_id="ru",
            allowed_operations=settings.allowed_operations,
            max_retries=0,
        )
        with pytest.raises(exception):
            await client.request_operation(operation, {"units": UNIT_ID})


@pytest.mark.asyncio
async def test_dodo_client_success_and_invalid_json(settings) -> None:
    operation = _repository(settings).get("get-delivery-statistics")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, json={"ok": True})
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = DodoApiClient(
            http_client,
            access_token="secret",
            country_id="ru",
            allowed_operations=settings.allowed_operations,
        )
        assert await client.request_operation(operation, {}) == {"ok": True}
    bad = httpx.MockTransport(
        lambda request: httpx.Response(200, request=request, content=b"not-json")
    )
    async with httpx.AsyncClient(transport=bad) as http_client:
        client = DodoApiClient(
            http_client,
            access_token="secret",
            country_id="ru",
            allowed_operations=settings.allowed_operations,
        )
        with pytest.raises(DodoApiError, match="JSON"):
            await client.request_operation(operation, {})
