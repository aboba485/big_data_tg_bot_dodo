from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.bot.services import BotReportService
from app.documentation import DocumentationLoader, DocumentationRepository
from app.dodo.channels import (
    SALES_CHANNEL_GROUP,
    endpoint_sales_channel_capability,
    sales_channel_intent,
)
from app.planner.schemas import (
    Granularity,
    OperationArgument,
    OutputFormat,
    PlanStatus,
    ReportPlan,
)
from app.reports.service import ReportAggregator
from app.services import build_services
from app.storage.sqlite import SQLiteDatabase
from app.users.models import TelegramRole, TelegramUser
from tests.conftest import UNIT_ID


def _service(settings) -> BotReportService:
    services = build_services(settings, httpx.AsyncClient())
    return BotReportService(
        settings=settings,
        orchestrator=services["orchestrator"],
        retrieval=services["retrieval"],
        planner=services["planner"],
        validator=services["validator"],
        metrics=services["metrics"],
        resolver=services["resolver"],
        files=services["files"],
        user_access=services["user_access"],
    )


def _admin() -> TelegramUser:
    return TelegramUser(
        telegram_id=1,
        role=TelegramRole.ADMIN,
        allowed_report_types=["*"],
        allowed_unit_ids=["*"],
    )


def _repository(settings) -> DocumentationRepository:
    repository = DocumentationRepository(
        SQLiteDatabase(settings.sqlite_path),
        DocumentationLoader(settings.documentation_zip_path),
        allowed_operations=settings.allowed_operations,
        index_all_get=True,
    )
    repository.ensure_index()
    return repository


def test_russian_channel_intent_is_canonical_and_not_a_split() -> None:
    intent = sales_channel_intent("Покажи выручку по каналу доставки")

    assert intent.values == ("Delivery",)
    assert intent.split is False


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Покажи выручку доставки", "Delivery"),
        ("Покажи заказы на самовывоз", "Takeaway"),
        ("Покажи продажи в зале", "Dine-in"),
    ],
)
def test_channel_intent_does_not_require_channel_word(query: str, expected: str) -> None:
    assert sales_channel_intent(query).values == (expected,)


def test_restaurant_name_without_channel_context_is_not_dine_in() -> None:
    intent = sales_channel_intent("Покажи выручку ресторана Москва-1")

    assert intent.values == ()


def test_unit_clarification_is_not_mistaken_for_dine_in() -> None:
    intent = sales_channel_intent(
        "Покажи продажи по каналам за июнь. Уточнение пользователя: Ресторан Москва-1"
    )

    assert intent.values == ()
    assert intent.split is True


def test_specific_channel_wins_over_total_wording() -> None:
    intent = sales_channel_intent("Покажи общую сумму продаж доставки")

    assert intent.values == ("Delivery",)
    assert intent.all_together is False


def test_time_breakdown_wording_does_not_cancel_channel_split() -> None:
    intent = sales_channel_intent("Продажи по каналам без разбивки по дням")

    assert intent.split is True
    assert intent.all_together is False


@pytest.mark.asyncio
async def test_delivery_metric_is_not_mistaken_for_channel_filter(settings) -> None:
    service = _service(settings)

    preparation = await service.prepare(
        _admin(),
        f"Покажи среднее время доставки за июнь 2026 по юниту {UNIT_ID}",
        defer_units=True,
    )

    assert preparation.status == "ready"
    assert preparation.sales_channel_options == []
    assert preparation.sales_channel_selection == ""


def test_channel_breakdown_intent_is_detected() -> None:
    intent = sales_channel_intent("Покажи количество заказов в разрезе каналов")

    assert intent.values == ()
    assert intent.split is True


def test_channel_filter_matches_endpoint_spelling_variants() -> None:
    plan = ReportPlan(
        status=PlanStatus.READY,
        filters=[{"name": "salesChannel", "values": ["Dine-in"]}],
    )

    assert ReportAggregator._matches_filters(plan, {"salesChannel": "DineIn"}) is True


def test_documentation_distinguishes_breakdown_and_filter_only_endpoints(settings) -> None:
    repository = _repository(settings)
    finances = endpoint_sales_channel_capability(repository.get("get-finances-sales-daily-units"))
    workload = endpoint_sales_channel_capability(
        repository.get("get-production-unit-workload-by-orders")
    )

    assert finances is not None
    assert finances.can_group is True
    assert finances.can_filter is True
    assert {"Delivery", "Dine-in", "Takeaway"}.issubset(finances.values)
    assert workload is not None
    assert workload.can_group is False
    assert workload.can_filter is True
    assert workload.parameter is not None
    assert workload.parameter.name == "salesChannels"


@pytest.mark.asyncio
async def test_generic_channel_capable_report_offers_channel_choice(settings) -> None:
    service = _service(settings)

    preparation = await service.prepare(
        _admin(),
        f"Покажи выручку за июнь 2026 по юниту {UNIT_ID}",
        defer_units=True,
    )

    assert preparation.status == "ready"
    assert preparation.sales_channel_options == [
        "Delivery",
        "Dine-in",
        "Takeaway",
        "Staff meal",
        "Tracker",
    ]
    assert preparation.sales_channel_can_split is True
    assert preparation.sales_channel_selection == ""


@pytest.mark.asyncio
async def test_explicit_russian_channel_filters_nested_finance_rows(settings) -> None:
    service = _service(settings)
    query = f"Покажи выручку по каналу доставки за июнь 2026 по юниту {UNIT_ID}"

    preparation = await service.prepare(_admin(), query, defer_units=True)
    result = await service.run(
        _admin(),
        query,
        OutputFormat.TABLE,
        unit_ids=[UNIT_ID],
        granularity=Granularity.TOTAL,
    )

    assert preparation.sales_channel_selection == "Delivery"
    assert result.status == "ready"
    assert result.response["totals"]["sales"] == 180_000
    assert result.response["plan"]["filters"] == [{"name": "salesChannel", "values": ["Delivery"]}]
    assert "Канал продаж: Доставка" in result.text


@pytest.mark.asyncio
async def test_channel_choice_can_split_base_sales_metric(settings) -> None:
    service = _service(settings)
    query = f"Покажи выручку за июнь 2026 по юниту {UNIT_ID}"
    await service.prepare(_admin(), query, defer_units=True)

    result = await service.run(
        _admin(),
        query,
        OutputFormat.TABLE,
        unit_ids=[UNIT_ID],
        granularity=Granularity.TOTAL,
        sales_channel_choice="split",
    )

    assert result.status == "ready"
    assert SALES_CHANNEL_GROUP in result.response["plan"]["group_by"]
    assert {row["salesChannel"] for row in result.response["rows"]} == {
        "Delivery",
        "Dine-in",
    }
    assert result.response["totals"]["sales"] == 300_000


@pytest.mark.asyncio
async def test_filter_only_metric_binds_documented_sales_channels_parameter(settings) -> None:
    service = _service(settings)
    endpoint = service.retrieval.repository.get("get-production-unit-workload-by-orders")
    assert endpoint is not None
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["workload_orders_count"],
        operation_ids=[endpoint.operation_id],
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 2),
        unit_references=[UNIT_ID],
        filters=[{"name": "salesChannel", "values": ["Dine-in"]}],
    )
    params: dict[str, object] = {}

    applied = service.orchestrator.executor._bind_operation_filters(endpoint, params, plan)

    assert params == {"salesChannels": "Dine-in"}
    assert applied == {"salesChannel"}


def test_singular_sales_channel_parameter_is_not_bound_to_multiple_values(settings) -> None:
    service = _service(settings)
    endpoint = service.retrieval.repository.get("get-sales")
    assert endpoint is not None
    plan = ReportPlan(
        status=PlanStatus.READY,
        operation_ids=[endpoint.operation_id],
        filters=[{"name": "salesChannel", "values": ["Delivery", "Takeaway"]}],
    )
    params: dict[str, object] = {}

    applied = service.orchestrator.executor._bind_operation_filters(endpoint, params, plan)

    assert params == {}
    assert applied == set()


def test_all_channels_choice_removes_model_channel_arguments(settings) -> None:
    service = _service(settings)
    plan = ReportPlan(
        status=PlanStatus.READY,
        metric_ids=["sales"],
        operation_ids=["get-finances-sales-daily-units"],
        operation_arguments=[
            OperationArgument(name="salesChannels", value="Delivery", location="query")
        ],
    )

    service.validator.apply_sales_channel_choice(plan, "all")

    assert plan.operation_arguments == []
