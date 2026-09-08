from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.errors import BotInputError
from app.bot.handlers.weekly import (
    WEEKLY_REPORT_HOURS,
    _time_keyboard,
    weekly_city,
    weekly_granularity,
    weekly_query,
    weekly_sales_channel,
    weekly_time,
    weekly_units_action,
)
from app.bot.models import BotPreparation, BotReportResult
from app.bot.scheduler import WeeklyReportScheduler
from app.bot.services import UnitCity
from app.bot.states.reports import WeeklyReportForm
from app.planner.schemas import Granularity
from app.storage.sqlite import SQLiteDatabase
from app.storage.weekly_reports import (
    WeeklyReportLimitError,
    WeeklyReportRepository,
    WeeklyReportSubscription,
    encode_subscription_unit_ids,
    next_weekly_run,
    parse_subscription_unit_ids,
)


@dataclass
class FakeMessage:
    text: str | None = None
    answers: list[tuple[str, object | None]] = field(default_factory=list)

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.answers.append(("", reply_markup))


@dataclass
class FakeCallback:
    data: str | None
    message: FakeMessage = field(default_factory=FakeMessage)
    answered: bool = False
    alert: str | None = None

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answered = True
        if show_alert:
            self.alert = text


@dataclass
class FakeState:
    data: dict[str, object] = field(default_factory=dict)
    state: object | None = None
    cleared: bool = False

    async def clear(self) -> None:
        self.cleared = True
        self.data.clear()
        self.state = None

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def get_data(self) -> dict[str, object]:
        return self.data


def _user() -> SimpleNamespace:
    return SimpleNamespace(telegram_id=1)


def _format_service(*, sheets: bool = False) -> SimpleNamespace:
    return SimpleNamespace(sheets_available=lambda _user: sheets)


def test_subscription_unit_ids_encode_and_parse() -> None:
    single = "000d3a240c719a8711e68aba13f7f862"
    multi = [
        "000d3a240c719a8711e68aba13f7f862",
        "000d3a240c719a8711e68aba13f7fc8a",
    ]

    assert parse_subscription_unit_ids(single) == [single]
    encoded = encode_subscription_unit_ids(multi)
    assert parse_subscription_unit_ids(encoded) == multi
    assert encode_subscription_unit_ids(list(reversed(multi))) == encoded
    assert encode_subscription_unit_ids([single]) == single


def test_next_weekly_run_uses_configured_timezone() -> None:
    now = datetime(2026, 8, 3, 7, 30, tzinfo=UTC)

    result = next_weekly_run(now, weekday=0, local_hour=9, timezone_name="Europe/Moscow")

    assert result == datetime(2026, 8, 10, 6, 0, tzinfo=UTC)


def test_subscriptions_are_scoped_to_owner_and_claimed_once(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "weekly.db")
    database.initialize()
    repository = WeeklyReportRepository(database)
    now = datetime(2026, 8, 3, 5, 0, tzinfo=UTC)
    subscription = repository.create(
        telegram_id=10,
        chat_id=10,
        metric_id="sales",
        unit_id="unit-1",
        output_format="table",
        granularity="day",
        weekday=0,
        local_hour=9,
        timezone_name="Europe/Moscow",
        now_utc=now,
    )

    assert subscription.granularity == "day"
    assert repository.list_for_user(10) == [subscription]
    assert repository.list_for_user(11) == []
    assert repository.delete_for_user(subscription.id, 11) is False
    assert repository.claim(subscription.id, "2026-08-02", now) is True
    assert repository.claim(subscription.id, "2026-08-02", now) is False


def test_subscription_creation_is_idempotent_and_limited(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "weekly-limit.db")
    database.initialize()
    repository = WeeklyReportRepository(database)
    common = {
        "telegram_id": 10,
        "chat_id": 10,
        "metric_id": "sales",
        "unit_id": "unit-1",
        "output_format": "table",
        "granularity": "total",
        "weekday": 0,
        "local_hour": 9,
        "timezone_name": "Europe/Moscow",
        "max_per_user": 1,
    }

    first = repository.create(**common)
    duplicate = repository.create(**common)

    assert duplicate.id == first.id
    with pytest.raises(WeeklyReportLimitError, match="не более 1"):
        repository.create(**{**common, "metric_id": "orders_count"})


def test_subscription_granularity_affects_uniqueness(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "weekly-granularity.db")
    database.initialize()
    repository = WeeklyReportRepository(database)
    common = {
        "telegram_id": 10,
        "chat_id": 10,
        "metric_id": "sales",
        "unit_id": "unit-1",
        "output_format": "table",
        "weekday": 0,
        "local_hour": 9,
        "timezone_name": "Europe/Moscow",
        "max_per_user": 5,
    }

    first = repository.create(**common, granularity="total")
    second = repository.create(**common, granularity="day")

    assert first.id != second.id
    assert {item.granularity for item in repository.list_for_user(10)} == {"total", "day"}


def test_subscription_sales_channel_affects_uniqueness(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "weekly-channel.db")
    database.initialize()
    repository = WeeklyReportRepository(database)
    common = {
        "telegram_id": 10,
        "chat_id": 10,
        "metric_id": "sales",
        "unit_id": "unit-1",
        "output_format": "table",
        "granularity": "total",
        "weekday": 0,
        "local_hour": 9,
        "timezone_name": "Europe/Moscow",
        "max_per_user": 5,
    }

    combined = repository.create(**common)
    delivery = repository.create(**common, sales_channel_choice="Delivery")

    assert combined.id != delivery.id
    stored = repository.get(delivery.id)
    assert stored is not None
    assert stored.sales_channel_choice == "Delivery"


def test_weekly_time_keyboard_contains_every_hour_from_6_to_22() -> None:
    markup = _time_keyboard()
    time_rows = markup.inline_keyboard[:-1]
    callbacks = [button.callback_data for row in time_rows for button in row]

    assert tuple(range(6, 23)) == WEEKLY_REPORT_HOURS
    assert callbacks == [f"weekly:time:{hour}" for hour in range(6, 23)]
    assert max(map(len, time_rows)) == 4
    assert markup.inline_keyboard[-1][0].callback_data == "weekly:cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize("hour", [6, 22])
async def test_weekly_time_accepts_range_boundaries(hour) -> None:
    callback = FakeCallback(data=f"weekly:time:{hour}")
    state = FakeState()

    await weekly_time(callback, state, _user(), _format_service())  # type: ignore[arg-type]

    assert state.data["local_hour"] == hour
    assert state.state == WeeklyReportForm.choosing_format


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["5", "23", "abc"])
async def test_weekly_time_rejects_values_outside_allowed_range(value) -> None:
    callback = FakeCallback(data=f"weekly:time:{value}")
    state = FakeState()

    with pytest.raises(BotInputError):
        await weekly_time(callback, state, _user(), _format_service())  # type: ignore[arg-type]

    assert "local_hour" not in state.data
    assert state.state is None


@pytest.mark.asyncio
async def test_weekly_query_moves_to_unit_selection() -> None:
    state = FakeState(data={"started_at": 10**20}, state=WeeklyReportForm.waiting_query)
    user = SimpleNamespace(telegram_id=1)
    city = UnitCity(
        city_id="city-token",
        label="Москва",
        units=(("unit-1", "Москва 4-1"),),
    )
    service = SimpleNamespace(
        settings=SimpleNamespace(telegram_fsm_ttl_seconds=1800),
        prepare=AsyncMock(
            return_value=BotPreparation(
                status="ready",
                report_types=["sales"],
                unit_ids=["unit-1"],
            )
        ),
        available_reports=lambda _user: [("sales", "Выручка")],
        available_units=lambda _user: [("unit-1", "Ресторан 1")],
        available_unit_cities=lambda _user: [city],
        units_in_city=lambda _user, city_id: list(city.units) if city_id == city.city_id else [],
        metrics=SimpleNamespace(has=lambda metric_id: metric_id == "sales"),
    )
    message = FakeMessage(text="Покажи выручку")

    await weekly_query(message, state, user, service)  # type: ignore[arg-type]

    assert message.answers[0] == ("⏳ Разбираю запрос…", None)
    assert state.state == WeeklyReportForm.choosing_city
    assert state.data["metric_id"] == "sales"
    assert state.data["unit_ids"] == ["unit-1"]
    assert "выберите город" in message.answers[-1][0].casefold()

    callback = FakeCallback(data="weekly:city:city-token", message=message)
    await weekly_city(callback, state, user, service)  # type: ignore[arg-type]

    assert state.state == WeeklyReportForm.choosing_unit
    markup = message.answers[-1][1]
    assert markup.inline_keyboard[0][0].callback_data == "weekly:toggle:unit-1"
    assert any(
        button.callback_data == "weekly:cities" for row in markup.inline_keyboard for button in row
    )


@pytest.mark.asyncio
async def test_weekly_city_rejects_unknown_token() -> None:
    state = FakeState(state=WeeklyReportForm.choosing_city)
    service = SimpleNamespace(available_unit_cities=lambda _user: [])

    with pytest.raises(BotInputError, match="город недоступен"):
        await weekly_city(  # type: ignore[arg-type]
            FakeCallback(data="weekly:city:forged"), state, _user(), service
        )

    assert state.state == WeeklyReportForm.choosing_city
    assert "city_id" not in state.data


@pytest.mark.asyncio
async def test_weekly_units_done_asks_for_granularity() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "unit_ids": ["unit-1"],
            "unit_labels": {"unit-1": "Ресторан 1"},
            "metric_id": "sales",
            "preparation": {"report_types": ["sales"]},
            "city_id": "city-token",
        },
        state=WeeklyReportForm.choosing_unit,
    )
    user = SimpleNamespace(telegram_id=1)
    service = SimpleNamespace(
        units_in_city=lambda _user, _city_id: [("unit-1", "Ресторан 1")],
        metrics=SimpleNamespace(
            has=lambda metric_id: metric_id == "sales",
            get=lambda _metric_id: SimpleNamespace(granularities=["total", "day", "week", "month"]),
        ),
    )
    callback = FakeCallback(data="weekly:units:done")

    await weekly_units_action(callback, state, user, service)  # type: ignore[arg-type]

    assert state.state == WeeklyReportForm.choosing_granularity
    assert "разбить данные по времени" in callback.message.answers[-1][0]


@pytest.mark.asyncio
async def test_weekly_granularity_moves_to_frequency() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "metric_id": "sales",
            "preparation": {"report_types": ["sales"]},
        },
        state=WeeklyReportForm.choosing_granularity,
    )
    service = SimpleNamespace(
        metrics=SimpleNamespace(
            has=lambda metric_id: metric_id == "sales",
            get=lambda _metric_id: SimpleNamespace(granularities=["total", "day", "week", "month"]),
        )
    )
    callback = FakeCallback(data="weekly:granularity:week")

    await weekly_granularity(callback, state, service)  # type: ignore[arg-type]

    assert state.state == WeeklyReportForm.choosing_frequency
    assert state.data["granularity"] == "week"
    assert state.data["granularity_label"] == "По неделям"
    assert "часто" in callback.message.answers[-1][0].lower()


@pytest.mark.asyncio
async def test_weekly_flow_offers_and_saves_channel_choice() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "metric_id": "sales",
            "preparation": {
                "report_types": ["sales"],
                "sales_channel_options": ["Delivery", "Dine-in", "Takeaway"],
                "sales_channel_can_split": True,
            },
        },
        state=WeeklyReportForm.choosing_granularity,
    )
    service = SimpleNamespace(
        metrics=SimpleNamespace(
            has=lambda metric_id: metric_id == "sales",
            get=lambda _metric_id: SimpleNamespace(granularities=["total", "day", "week", "month"]),
        )
    )
    granularity_callback = FakeCallback(data="weekly:granularity:total")

    await weekly_granularity(  # type: ignore[arg-type]
        granularity_callback, state, service
    )

    assert state.state == WeeklyReportForm.choosing_channel
    callbacks = [
        button.callback_data
        for row in granularity_callback.message.answers[-1][1].inline_keyboard
        for button in row
    ]
    assert "weekly:channel:split" in callbacks

    channel_callback = FakeCallback(data="weekly:channel:Delivery")
    await weekly_sales_channel(channel_callback, state)  # type: ignore[arg-type]

    assert state.data["sales_channel_choice"] == "Delivery"
    assert state.state == WeeklyReportForm.choosing_frequency


@pytest.mark.asyncio
async def test_scheduler_sends_previous_completed_week() -> None:
    now = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    subscription = WeeklyReportSubscription(
        id=1,
        telegram_id=10,
        chat_id=10,
        metric_id="sales",
        unit_id="000d3a240c719a8711e68aba13f7f862",
        output_format="table",
        weekday=2,
        local_hour=12,
        timezone="Europe/Moscow",
        next_run_at=now,
        granularity="day",
        sales_channel_choice="Delivery",
    )
    repository = SimpleNamespace(
        claim=Mock(return_value=True),
        get=Mock(return_value=subscription),
        mark_sent=Mock(),
        mark_failed=Mock(),
    )
    user = SimpleNamespace(telegram_id=10)
    report_service = SimpleNamespace(
        user_access=SimpleNamespace(authorized=Mock(return_value=user)),
        run_selected=AsyncMock(return_value=BotReportResult(status="ready", text="Готово")),
    )
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    scheduler = WeeklyReportScheduler(bot, repository, report_service, poll_seconds=30)

    await scheduler._deliver(subscription, now)

    call = report_service.run_selected.await_args.kwargs
    assert call["date_from"].isoformat() == "2026-07-27"
    assert call["date_to"].isoformat() == "2026-08-02"
    assert call["unit_ids"] == ["000d3a240c719a8711e68aba13f7f862"]
    assert call["granularity"] == Granularity.DAY
    assert call["sales_channel_choice"] == "Delivery"
    bot.send_message.assert_awaited_once_with(10, "Готово")
    repository.mark_sent.assert_called_once_with(subscription, "2026-08-02", now)
    repository.mark_failed.assert_not_called()


@pytest.mark.asyncio
async def test_scheduler_continues_after_one_subscription_fails() -> None:
    now = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    subscriptions = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    scheduler = WeeklyReportScheduler(
        SimpleNamespace(),
        SimpleNamespace(due=Mock(return_value=subscriptions)),
        SimpleNamespace(),
        poll_seconds=30,
    )
    scheduler._deliver = AsyncMock(  # type: ignore[method-assign]
        side_effect=[RuntimeError("broken subscription"), None]
    )

    await scheduler.run_once(now)

    assert scheduler._deliver.await_count == 2
