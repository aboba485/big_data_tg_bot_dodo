from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.errors import BotInputError
from app.bot.handlers.common import cancel_command, help_callback, start
from app.bot.handlers.reports import (
    _deliver,
    choose_city_callback,
    choose_format,
    choose_granularity_callback,
    choose_sales_channel_callback,
    clarify_report,
    confirm_report,
    natural_language_report,
    reports_help,
    units_action_callback,
)
from app.bot.keyboards import sales_channel_keyboard
from app.bot.models import BotPreparation, BotReportResult
from app.bot.services import UnitCity
from app.bot.states.reports import ReportForm
from app.planner.schemas import Granularity, OutputFormat
from app.users.models import TelegramUser


@dataclass
class FakeMessage:
    text: str | None = None
    answers: list[tuple[str, object | None]] = field(default_factory=list)
    documents: list[tuple[object, str | None]] = field(default_factory=list)
    reply_markups: list[object | None] = field(default_factory=list)

    async def answer(self, text: str, reply_markup=None) -> None:
        self.answers.append((text, reply_markup))

    async def answer_document(self, document, caption=None) -> None:
        self.documents.append((document, caption))

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.reply_markups.append(reply_markup)


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
    cleared: bool = False
    data: dict[str, object] = field(default_factory=dict)
    state: object | None = None

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


def _user() -> TelegramUser:
    return TelegramUser(
        telegram_id=1,
        allowed_report_types=["sales"],
        allowed_unit_ids=["unit-1"],
    )


def _ready_service(**extra) -> SimpleNamespace:
    city = UnitCity(
        city_id="city-token",
        label="Москва",
        units=(("unit-1", "Москва 4-1"),),
    )
    values = {
        "settings": SimpleNamespace(telegram_fsm_ttl_seconds=1800),
        "available_units": lambda _user: [("unit-1", "Ресторан 1")],
        "available_unit_cities": lambda _user: [city],
        "units_in_city": lambda _user, city_id: list(city.units) if city_id == city.city_id else [],
        "metrics": SimpleNamespace(
            has=lambda metric_id: metric_id == "sales",
            get=lambda _metric_id: SimpleNamespace(granularities=["total", "day", "week", "month"]),
        ),
        "prepare": AsyncMock(
            return_value=BotPreparation(
                status="ready",
                report_types=["sales"],
                unit_ids=["unit-1"],
                date_from=None,
                date_to=None,
            )
        ),
        "run": AsyncMock(return_value=BotReportResult(status="ready", text="Готово")),
        "sheets_available": lambda _user: False,
    }
    values.update(extra)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_start_and_cancel_send_main_menu() -> None:
    message = FakeMessage()
    state = FakeState()

    await start(message, state, _user())  # type: ignore[arg-type]
    await cancel_command(message, state)  # type: ignore[arg-type]

    assert state.cleared is True
    assert all(reply_markup is not None for _text, reply_markup in message.answers)


@pytest.mark.asyncio
async def test_help_callback_shows_help_text() -> None:
    callback = FakeCallback(data="help:show")
    await help_callback(callback)  # type: ignore[arg-type]
    assert callback.answered is True
    text = callback.message.answers[-1][0].casefold()
    assert "текстовым запросом" in text
    assert "заведения" in text


def test_main_menu_has_help_instead_of_create_report() -> None:
    from app.bot.keyboards import main_menu

    markup = main_menu()
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "help:show" in callbacks
    assert "report:new" not in callbacks
    assert "weekly:list" in callbacks


@pytest.mark.asyncio
async def test_reports_command_shows_help_instead_of_guided_flow() -> None:
    message = FakeMessage()
    await reports_help(message)  # type: ignore[arg-type]
    assert message.answers[-1][1] is not None
    assert "/schedules" in message.answers[-1][0]


@pytest.mark.asyncio
async def test_natural_request_asks_for_city_then_units_after_prepare() -> None:
    state = FakeState()
    service = _ready_service()

    query = FakeMessage(text="Покажи выручку за июнь по ресторану")
    await natural_language_report(query, state, _user(), service)  # type: ignore[arg-type]

    assert query.answers[0] == ("⏳ Разбираю запрос…", None)
    assert state.state == ReportForm.choosing_city
    assert "выберите город" in query.answers[-1][0].casefold()
    assert query.answers[-1][1] is not None
    assert state.data["unit_ids"] == ["unit-1"]

    callback = FakeCallback(data="report:city:city-token", message=query)
    await choose_city_callback(callback, state, _user(), service)  # type: ignore[arg-type]

    assert state.state == ReportForm.choosing_unit
    markup = query.answers[-1][1]
    assert markup.inline_keyboard[0][0].callback_data == "report:toggle:unit-1"
    assert any(
        button.callback_data == "report:cities" for row in markup.inline_keyboard for button in row
    )


@pytest.mark.asyncio
async def test_report_city_rejects_unknown_token() -> None:
    state = FakeState(state=ReportForm.choosing_city)
    service = _ready_service()

    with pytest.raises(BotInputError, match="город недоступен"):
        await choose_city_callback(  # type: ignore[arg-type]
            FakeCallback(data="report:city:forged"), state, _user(), service
        )

    assert state.state == ReportForm.choosing_city
    assert "city_id" not in state.data


@pytest.mark.asyncio
async def test_clearing_city_preserves_units_selected_in_other_city() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "city_id": "city-a",
            "unit_ids": ["unit-a", "unit-b"],
            "unit_labels": {"unit-a": "Москва 1", "unit-b": "Смоленск 1"},
        },
        state=ReportForm.choosing_unit,
    )
    service = _ready_service(units_in_city=lambda _user, _city_id: [("unit-a", "Москва 1")])

    await units_action_callback(  # type: ignore[arg-type]
        FakeCallback(data="report:units:clear"), state, _user(), service
    )

    assert state.data["unit_ids"] == ["unit-b"]
    assert state.data["unit_labels"] == {"unit-b": "Смоленск 1"}


@pytest.mark.asyncio
async def test_natural_request_clarification_moves_to_unit_selection() -> None:
    state = FakeState()
    service = _ready_service(
        prepare=AsyncMock(
            side_effect=[
                BotPreparation(status="needs_clarification", question="Уточните период"),
                BotPreparation(
                    status="ready",
                    report_types=["sales"],
                    unit_ids=["unit-1"],
                ),
            ]
        )
    )

    first = FakeMessage(text="Покажи выручку")
    await natural_language_report(first, state, _user(), service)  # type: ignore[arg-type]
    assert state.state == ReportForm.clarification
    assert first.answers[-1] == ("Уточните период", None)

    clarification = FakeMessage(text="за июнь 2026")
    await clarify_report(clarification, state, _user(), service)  # type: ignore[arg-type]
    assert state.state == ReportForm.choosing_city
    assert "за июнь 2026" in str(state.data["source_query"])
    assert "выберите город" in clarification.answers[-1][0].casefold()


@pytest.mark.asyncio
async def test_unit_selection_done_asks_for_granularity() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "unit_ids": ["unit-1"],
            "unit_labels": {"unit-1": "Ресторан 1"},
            "preparation": {"report_types": ["sales"]},
            "city_id": "city-token",
        },
        state=ReportForm.choosing_unit,
    )
    service = _ready_service()
    callback = FakeCallback(data="report:units:done")

    await units_action_callback(callback, state, _user(), service)  # type: ignore[arg-type]

    assert state.state == ReportForm.choosing_granularity
    assert "разбить данные по времени" in callback.message.answers[-1][0]
    assert callback.message.answers[-1][1] is not None


@pytest.mark.asyncio
async def test_granularity_moves_to_format() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "preparation": {"report_types": ["sales"]},
        },
        state=ReportForm.choosing_granularity,
    )
    service = _ready_service()
    callback = FakeCallback(data="report:granularity:day")

    await choose_granularity_callback(callback, state, _user(), service)  # type: ignore[arg-type]

    assert state.state == ReportForm.choosing_format
    assert state.data["granularity"] == "day"
    assert state.data["granularity_label"] == "По дням"
    assert "формат" in callback.message.answers[-1][0].casefold()


@pytest.mark.asyncio
async def test_granularity_offers_channel_choice_for_capable_report() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "preparation": {
                "report_types": ["sales"],
                "sales_channel_options": ["Delivery", "Dine-in", "Takeaway"],
                "sales_channel_can_split": True,
            },
        },
        state=ReportForm.choosing_granularity,
    )
    service = _ready_service()
    callback = FakeCallback(data="report:granularity:day")

    await choose_granularity_callback(callback, state, _user(), service)  # type: ignore[arg-type]

    assert state.state == ReportForm.choosing_channel
    assert "каналы продаж" in callback.message.answers[-1][0].casefold()
    callbacks = [
        button.callback_data
        for row in callback.message.answers[-1][1].inline_keyboard
        for button in row
    ]
    assert "report:channel:all" in callbacks
    assert "report:channel:split" in callbacks
    assert "report:channel:Delivery" in callbacks


@pytest.mark.parametrize(("prefix", "can_split"), [("report", True), ("weekly", False)])
def test_sales_channel_keyboard_has_flat_button_rows(prefix: str, can_split: bool) -> None:
    markup = sales_channel_keyboard(
        ["Delivery", "Dine-in", "Takeaway"],
        can_split=can_split,
        prefix=prefix,
    )

    callbacks = [row[0].callback_data for row in markup.inline_keyboard]
    expected = [f"{prefix}:channel:all"]
    if can_split:
        expected.append(f"{prefix}:channel:split")
    expected.extend(
        [
            f"{prefix}:channel:Delivery",
            f"{prefix}:channel:Dine-in",
            f"{prefix}:channel:Takeaway",
            f"{prefix}:cancel",
        ]
    )
    assert callbacks == expected


@pytest.mark.asyncio
async def test_channel_choice_moves_to_format() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "preparation": {
                "report_types": ["sales"],
                "sales_channel_options": ["Delivery", "Dine-in", "Takeaway"],
                "sales_channel_can_split": True,
            },
        },
        state=ReportForm.choosing_channel,
    )
    service = _ready_service()
    callback = FakeCallback(data="report:channel:Delivery")

    await choose_sales_channel_callback(callback, state, _user(), service)  # type: ignore[arg-type]

    assert state.state == ReportForm.choosing_format
    assert state.data["sales_channel_choice"] == "Delivery"
    assert state.data["sales_channel_label"] == "Доставка"
    assert "формат" in callback.message.answers[-1][0].casefold()


@pytest.mark.asyncio
async def test_rejects_unsupported_granularity() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "preparation": {"report_types": ["sales"]},
        },
        state=ReportForm.choosing_granularity,
    )
    service = _ready_service(
        metrics=SimpleNamespace(
            has=lambda metric_id: metric_id == "sales",
            get=lambda _metric_id: SimpleNamespace(granularities=["total", "day"]),
        )
    )
    callback = FakeCallback(data="report:granularity:month")

    with pytest.raises(BotInputError, match="детализация"):
        await choose_granularity_callback(  # type: ignore[arg-type]
            callback, state, _user(), service
        )


@pytest.mark.asyncio
async def test_text_only_flow_reaches_confirmation_and_delivers_report() -> None:
    state = FakeState(
        data={
            "started_at": 10**20,
            "source_query": "Покажи выручку за июнь по ресторану",
            "unit_ids": ["unit-1"],
            "unit_labels": {"unit-1": "Ресторан 1"},
            "granularity": "week",
            "granularity_label": "По неделям",
            "preparation": {
                "report_types": ["sales"],
                "date_from": "2026-06-01",
                "date_to": "2026-06-30",
            },
            "mode": "natural",
        },
        state=ReportForm.choosing_format,
    )
    service = _ready_service()

    format_message = FakeMessage(text="CSV")
    await choose_format(format_message, state, _user(), service)  # type: ignore[arg-type]
    assert state.state == ReportForm.confirming
    assert "Детализация: По неделям" in format_message.answers[-1][0]
    assert "Напишите «да»" in format_message.answers[-1][0]

    confirmation = FakeMessage(text="да")
    await confirm_report(confirmation, state, _user(), service)  # type: ignore[arg-type]

    service.run.assert_awaited_once_with(
        _user(),
        "Покажи выручку за июнь по ресторану",
        OutputFormat.CSV,
        unit_ids=["unit-1"],
        granularity=Granularity.WEEK,
        sales_channel_choice=None,
    )
    # Report delivered, then asked if user wants to make it repeating
    assert "Готово" in confirmation.answers[-2][0]
    assert "регулярно" in confirmation.answers[-1][0].lower()
    assert state.state == ReportForm.ask_make_repeating


@pytest.mark.asyncio
async def test_invalid_text_choices_keep_current_state() -> None:
    service = SimpleNamespace(
        settings=SimpleNamespace(telegram_fsm_ttl_seconds=1800),
        sheets_available=lambda _user: False,
    )
    state = FakeState(
        data={"source_query": "query", "started_at": 10**20},
        state=ReportForm.choosing_format,
    )
    format_message = FakeMessage(text="PDF")

    await choose_format(format_message, state, _user(), service)  # type: ignore[arg-type]

    assert state.state == ReportForm.choosing_format
    assert "текст, CSV или XLSX" in format_message.answers[-1][0]


@pytest.mark.asyncio
async def test_file_is_removed_after_telegram_send(tmp_path) -> None:
    path = tmp_path / "report.xlsx"
    path.write_bytes(b"report")
    cleaned: list[str] = []

    class FakeService:
        def cleanup_file(self, report_id: str) -> None:
            cleaned.append(report_id)
            path.unlink(missing_ok=True)

    message = FakeMessage()
    result = BotReportResult(
        status="ready",
        text="Готово",
        report_id="a" * 32,
        file_path=path,
        file_name="report.xlsx",
    )

    await _deliver(message, FakeService(), result)  # type: ignore[arg-type]

    assert len(message.documents) == 1
    assert cleaned == ["a" * 32]
    assert not path.exists()


@pytest.mark.asyncio
async def test_file_is_removed_when_telegram_send_fails(tmp_path) -> None:
    path = tmp_path / "report.csv"
    path.write_bytes(b"report")
    cleaned: list[str] = []

    class FailingMessage(FakeMessage):
        async def answer_document(self, document, caption=None) -> None:
            raise RuntimeError("Telegram unavailable")

    class FakeService:
        def cleanup_file(self, report_id: str) -> None:
            cleaned.append(report_id)
            path.unlink(missing_ok=True)

    result = BotReportResult(
        status="ready",
        text="Готово",
        report_id="b" * 32,
        file_path=path,
        file_name="report.csv",
    )

    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        await _deliver(FailingMessage(), FakeService(), result)  # type: ignore[arg-type]
    assert cleaned == ["b" * 32]
    assert not path.exists()
