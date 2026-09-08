from __future__ import annotations

import time
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.bot.errors import BotInputError
from app.bot.keyboards import (
    FORMAT_LABELS,
    GRANULARITY_LABELS,
    allowed_formats,
    format_keyboard,
    granularity_keyboard,
    keyboard,
    main_menu,
    sales_channel_keyboard,
    unit_cities_keyboard,
    units_multiselect_keyboard,
)
from app.bot.messages.texts import DRIVE_NOT_LINKED, HELP_TEXT
from app.bot.models import BotPreparation
from app.bot.services import BotReportService
from app.bot.states.reports import ReportForm, ScheduledReportForm
from app.dodo.channels import sales_channel_label
from app.planner.schemas import Granularity, OutputFormat
from app.users.models import TelegramUser

router = Router(name="reports")

FORMAT_ALIASES = {
    "текст": OutputFormat.TABLE,
    "таблица": OutputFormat.TABLE,
    "table": OutputFormat.TABLE,
    "csv": OutputFormat.CSV,
    "xlsx": OutputFormat.XLSX,
    "excel": OutputFormat.XLSX,
    "sheets": OutputFormat.SHEETS,
    "google sheets": OutputFormat.SHEETS,
    "гугл таблицы": OutputFormat.SHEETS,
    "google": OutputFormat.SHEETS,
}
CONFIRM_WORDS = {"да", "сформировать", "готово"}
MODIFY_WORDS = {"изменить", "изменить параметры"}
CANCEL_WORDS = {"отмена", "отменить"}
DEFAULT_GRANULARITIES = ["total", "day", "week", "month"]


@router.message(Command("reports"))
async def reports_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.callback_query(ReportForm.choosing_city, F.data.startswith("report:city:"))
async def choose_city_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    city_id = (callback.data or "").removeprefix("report:city:")
    city = next(
        (
            item
            for item in bot_report_service.available_unit_cities(telegram_user)
            if item.city_id == city_id
        ),
        None,
    )
    if city is None:
        raise BotInputError("Этот город недоступен.")
    data = await state.get_data()
    await state.update_data(city_id=city.city_id, city_label=city.label, units_page=0)
    await state.set_state(ReportForm.choosing_unit)
    if callback.message:
        await callback.message.answer(
            f"🏙 {city.label}\nВыберите одно или несколько заведений:",
            reply_markup=units_multiselect_keyboard(
                list(city.units), set(_unit_ids(data)), 0, "report", city_back=True
            ),
        )


@router.callback_query(ReportForm.choosing_unit, F.data == "report:cities")
async def report_cities_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    if callback.message:
        await _show_cities(callback.message, state, telegram_user, bot_report_service)


@router.callback_query(ReportForm.choosing_unit, F.data.startswith("report:toggle:"))
async def toggle_unit_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    unit_id = (callback.data or "").removeprefix("report:toggle:")
    data = await state.get_data()
    units = dict(bot_report_service.units_in_city(telegram_user, str(data.get("city_id", ""))))
    if unit_id not in units:
        raise BotInputError("Это заведение недоступно.")
    selected = set(_unit_ids(data))
    labels = dict(_unit_labels(data))
    if unit_id in selected:
        selected.remove(unit_id)
        labels.pop(unit_id, None)
    else:
        selected.add(unit_id)
        labels[unit_id] = units[unit_id]
    page = int(data.get("units_page", 0))
    await state.update_data(unit_ids=sorted(selected), unit_labels=labels, units_page=page)
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=units_multiselect_keyboard(
                list(units.items()),
                selected,
                page,
                "report",
                city_back=True,
            )
        )


@router.callback_query(ReportForm.choosing_unit, F.data.startswith("report:units:"))
async def units_action_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    action = (callback.data or "").removeprefix("report:units:")
    data = await state.get_data()
    units = bot_report_service.units_in_city(telegram_user, str(data.get("city_id", "")))
    if not units:
        raise BotInputError("Этот город больше недоступен.")
    page = int(data.get("units_page", 0))

    if action == "all":
        await callback.answer()
        selected = set(_unit_ids(data)) | {unit_id for unit_id, _ in units}
        labels = dict(_unit_labels(data))
        labels.update(dict(units))
        await state.update_data(unit_ids=sorted(selected), unit_labels=labels)
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=units_multiselect_keyboard(
                    units, selected, page, "report", city_back=True
                )
            )
        return

    if action == "clear":
        await callback.answer()
        city_unit_ids = {unit_id for unit_id, _ in units}
        selected = set(_unit_ids(data)) - city_unit_ids
        labels = {
            unit_id: label
            for unit_id, label in _unit_labels(data).items()
            if unit_id not in city_unit_ids
        }
        await state.update_data(unit_ids=sorted(selected), unit_labels=labels)
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=units_multiselect_keyboard(
                    units, selected, page, "report", city_back=True
                )
            )
        return

    if action == "done":
        selected = _unit_ids(data)
        if not selected:
            await callback.answer("Выберите хотя бы одно заведение.", show_alert=True)
            return
        await callback.answer()
        labels = _unit_labels(data)
        await state.update_data(
            unit_ids=selected,
            unit_labels=labels,
            unit_label=", ".join(labels.get(unit_id, unit_id) for unit_id in selected),
        )
        await _advance_natural_flow(callback.message, state, telegram_user, bot_report_service)
        return

    await callback.answer()
    page = _callback_int(callback.data, "report:units:", "страницы")
    selected = set(_unit_ids(data))
    await state.update_data(units_page=page)
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=units_multiselect_keyboard(units, selected, page, "report", city_back=True)
        )


@router.callback_query(ReportForm.choosing_granularity, F.data.startswith("report:granularity:"))
async def choose_granularity_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    value = (callback.data or "").removeprefix("report:granularity:")
    allowed = _allowed_granularities(await state.get_data(), bot_report_service)
    if value not in allowed:
        raise BotInputError("Эта детализация недоступна для выбранного отчёта.")
    try:
        granularity = Granularity(value)
    except ValueError as exc:
        raise BotInputError("Неизвестная детализация.") from exc
    await state.update_data(
        granularity=granularity.value,
        granularity_label=GRANULARITY_LABELS.get(granularity.value, granularity.value),
    )
    await _advance_natural_flow(callback.message, state, telegram_user, bot_report_service)


@router.callback_query(ReportForm.choosing_channel, F.data.startswith("report:channel:"))
async def choose_sales_channel_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    await _ensure_active(state, bot_report_service)
    choice = (callback.data or "").removeprefix("report:channel:")
    data = await state.get_data()
    preparation = data.get("preparation") or {}
    options = [str(value) for value in preparation.get("sales_channel_options") or []]
    can_split = bool(preparation.get("sales_channel_can_split"))
    if choice not in {"all", *options} and not (choice == "split" and can_split):
        raise BotInputError("Этот канал продаж недоступен.")
    label = (
        "Все каналы вместе"
        if choice == "all"
        else "Разбивка по каналам"
        if choice == "split"
        else sales_channel_label(choice)
    )
    await state.update_data(sales_channel_choice=choice, sales_channel_label=label)
    await _advance_natural_flow(callback.message, state, telegram_user, bot_report_service)


@router.callback_query(ReportForm.choosing_format, F.data.startswith("report:format:"))
async def choose_format_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    await _ensure_active(state, bot_report_service)
    value = (callback.data or "").removeprefix("report:format:")
    output_format = _parse_format(value, telegram_user, bot_report_service)
    await state.update_data(output_format=output_format.value)
    await state.set_state(ReportForm.confirming)
    data = await state.get_data()
    if callback.message:
        await callback.message.answer(
            _confirmation_text(data),
            reply_markup=keyboard(
                [
                    [("Сформировать", "report:natural-confirm")],
                    [("Отмена", "report:cancel")],
                ]
            ),
        )


@router.callback_query(ReportForm.confirming, F.data == "report:natural-confirm")
async def confirm_natural_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    await _ensure_active(state, bot_report_service)
    data = await state.get_data()
    if not callback.message:
        return
    await callback.message.answer("Отчёт формируется…")
    result = await bot_report_service.run(
        telegram_user,
        str(data["source_query"]),
        OutputFormat(str(data["output_format"])),
        unit_ids=_unit_ids(data),
        granularity=Granularity(str(data.get("granularity") or Granularity.TOTAL.value)),
        sales_channel_choice=str(data.get("sales_channel_choice") or "") or None,
    )
    if result.status == "needs_clarification":
        await state.set_state(ReportForm.clarification)
        await callback.message.answer(result.question or "Уточните параметры.")
        return
    if result.status == "unsupported":
        await state.clear()
        await callback.message.answer(
            result.reason or "Отчёт не поддерживается.",
            reply_markup=main_menu(),
        )
        return
    await _deliver(callback.message, bot_report_service, result)
    await _ask_make_repeating(callback.message, state)


@router.callback_query(F.data == "report:cancel")
async def cancel_report_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await state.clear()
    if callback.message:
        await callback.message.answer("Действие отменено.", reply_markup=main_menu())


@router.callback_query(ReportForm.ask_make_repeating, F.data == "report:make_repeating")
async def make_repeating_callback(
    callback: CallbackQuery,
    state: FSMContext,
    bot_report_service: BotReportService,
) -> None:
    """Convert current report settings to a repeating report."""
    await callback.answer()
    data = await state.get_data()
    if not callback.message:
        await state.clear()
        return

    preparation = data.get("preparation") or {}
    report_types = list(preparation.get("report_types") or [])
    metric_id = next(
        (rt for rt in report_types if bot_report_service.metrics.has(rt)),
        report_types[0] if report_types else None,
    )
    if not metric_id:
        await state.clear()
        await callback.message.answer(
            "Не удалось определить тип отчёта для повторения.",
            reply_markup=main_menu(),
        )
        return

    unit_ids = _unit_ids(data)
    unit_labels = _unit_labels(data)
    granularity = str(data.get("granularity") or Granularity.TOTAL.value)
    output_format = str(data.get("output_format") or "table")

    await state.clear()
    await state.set_state(ScheduledReportForm.choosing_frequency)
    await state.update_data(
        metric_id=metric_id,
        metric_label=bot_report_service.metrics.aliases().get(metric_id, [metric_id])[0],
        unit_ids=unit_ids,
        unit_labels=unit_labels,
        unit_label=", ".join(unit_labels.get(uid, uid) for uid in unit_ids),
        granularity=granularity,
        granularity_label=GRANULARITY_LABELS.get(granularity, granularity),
        output_format=output_format,
        sales_channel_choice=str(
            data.get("sales_channel_choice")
            or (data.get("preparation") or {}).get("sales_channel_selection")
            or ""
        ),
        started_at=_now_timestamp(),
        from_one_time_report=True,
    )

    await callback.message.answer(
        "Как часто присылать отчёт?",
        reply_markup=keyboard(
            [
                [("📅 Раз в неделю", "weekly:freq:weekly")],
                [("📆 Раз в месяц", "weekly:freq:monthly")],
                [("Отмена", "weekly:cancel")],
            ]
        ),
    )


@router.message(ReportForm.clarification, F.text)
async def clarify_report(
    message: Message,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await _ensure_active(state, bot_report_service)
    data = await state.get_data()
    query = f"{data['source_query']}. Уточнение пользователя: {message.text}"
    preparation = await bot_report_service.prepare(telegram_user, query, defer_units=True)
    if preparation.status == "needs_clarification":
        await state.update_data(source_query=query)
        await message.answer(preparation.question or "Уточните параметры.")
        return
    if preparation.status == "unsupported":
        await state.clear()
        await message.answer(
            preparation.reason or "Отчёт не поддерживается.",
            reply_markup=main_menu(),
        )
        return
    await _ask_units(message, state, telegram_user, bot_report_service, query, preparation)


@router.message(ReportForm.choosing_format, F.text)
async def choose_format(
    message: Message,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await _ensure_active(state, bot_report_service)
    output_format = FORMAT_ALIASES.get((message.text or "").strip().casefold())
    if output_format is None:
        await message.answer("Неизвестный формат. Напишите: текст, CSV или XLSX.")
        return
    output_format = _parse_format(output_format.value, telegram_user, bot_report_service)
    await state.update_data(output_format=output_format.value)
    await state.set_state(ReportForm.confirming)
    data = await state.get_data()
    await message.answer(
        f"{_confirmation_text(data)}\n\n"
        "Напишите «да», чтобы сформировать отчёт, «изменить» для нового запроса "
        "или «отмена»."
    )


@router.message(ReportForm.confirming, F.text)
async def confirm_report(
    message: Message,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await _ensure_active(state, bot_report_service)
    answer = (message.text or "").strip().casefold()
    if answer in CANCEL_WORDS:
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=main_menu())
        return
    if answer in MODIFY_WORDS:
        await state.clear()
        await message.answer(
            "Отправьте новый запрос со всеми нужными параметрами.",
            reply_markup=main_menu(),
        )
        return
    if answer not in CONFIRM_WORDS:
        await message.answer("Напишите: да, изменить или отмена.")
        return

    data = await state.get_data()
    output_format = OutputFormat(str(data["output_format"]))
    await message.answer("Отчёт формируется…")
    query = str(data["source_query"])
    result = await bot_report_service.run(
        telegram_user,
        query,
        output_format,
        unit_ids=_unit_ids(data),
        granularity=Granularity(str(data.get("granularity") or Granularity.TOTAL.value)),
        sales_channel_choice=str(data.get("sales_channel_choice") or "") or None,
    )
    if result.status == "needs_clarification":
        await state.update_data(source_query=query)
        await state.set_state(ReportForm.clarification)
        await message.answer(result.question or "Уточните параметры.")
        return
    if result.status == "unsupported":
        await state.clear()
        await message.answer(
            result.reason or "Отчёт не поддерживается.",
            reply_markup=main_menu(),
        )
        return
    await _deliver(message, bot_report_service, result)
    await _ask_make_repeating(message, state)


@router.message(StateFilter(None), F.text)
async def natural_language_report(
    message: Message,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    query = message.text or ""
    await message.answer("⏳ Разбираю запрос…")
    preparation = await bot_report_service.prepare(telegram_user, query, defer_units=True)
    if preparation.status == "needs_clarification":
        await state.set_state(ReportForm.clarification)
        await state.update_data(source_query=query, started_at=_now_timestamp())
        await message.answer(preparation.question or "Уточните параметры.")
        return
    if preparation.status == "unsupported":
        await message.answer(
            preparation.reason or "Отчёт не поддерживается.",
            reply_markup=main_menu(),
        )
        return
    await _ask_units(message, state, telegram_user, bot_report_service, query, preparation)


async def _ask_units(
    message: Message,
    state: FSMContext,
    user: TelegramUser,
    service: BotReportService,
    query: str,
    preparation: BotPreparation,
) -> None:
    units = service.available_units(user)
    if not units:
        raise BotInputError("Для вашего профиля не настроены доступные заведения.")
    available = dict(units)
    if any(unit_id not in available for unit_id in preparation.unit_ids):
        raise BotInputError("Одно из указанных заведений недоступно.")
    preselected = list(dict.fromkeys(preparation.unit_ids)) if preparation.units_specified else []
    labels = {unit_id: available[unit_id] for unit_id in preselected}
    await state.update_data(
        source_query=query,
        preparation=preparation.model_dump(mode="json"),
        started_at=_now_timestamp(),
        mode="natural",
        unit_ids=preselected,
        unit_labels=labels,
        units_page=0,
        date_from=preparation.date_from.isoformat() if preparation.date_from else None,
        date_to=preparation.date_to.isoformat() if preparation.date_to else None,
        metric_label=", ".join(preparation.report_types),
    )
    await _advance_natural_flow(message, state, user, service)


async def _advance_natural_flow(
    message: Message | None,
    state: FSMContext,
    user: TelegramUser,
    service: BotReportService,
) -> None:
    if message is None:
        return
    data = await state.get_data()
    if not _unit_ids(data):
        await _show_cities(message, state, user, service)
        return

    preparation = data.get("preparation") or {}
    if not data.get("granularity"):
        prepared_granularity = str(preparation.get("granularity") or "")
        if preparation.get("granularity_specified") and prepared_granularity in (
            _allowed_granularities(data, service)
        ):
            await state.update_data(
                granularity=prepared_granularity,
                granularity_label=GRANULARITY_LABELS.get(
                    prepared_granularity, prepared_granularity
                ),
            )
            data = await state.get_data()
        else:
            await _ask_granularity(message, state, service)
            return

    data = await state.get_data()
    options = [str(value) for value in preparation.get("sales_channel_options") or []]
    selected_channel = str(data.get("sales_channel_choice") or "")
    if preparation.get("sales_channel_specified"):
        selected_channel = selected_channel or str(preparation.get("sales_channel_selection") or "")
    if options and not selected_channel:
        await state.set_state(ReportForm.choosing_channel)
        await message.answer(
            "Выберите каналы продаж:",
            reply_markup=sales_channel_keyboard(
                options,
                can_split=bool(preparation.get("sales_channel_can_split")),
                prefix="report",
            ),
        )
        return
    if selected_channel and not data.get("sales_channel_choice"):
        label = (
            "Все каналы вместе"
            if selected_channel == "all"
            else "Разбивка по каналам"
            if selected_channel == "split"
            else sales_channel_label(selected_channel)
        )
        await state.update_data(
            sales_channel_choice=selected_channel,
            sales_channel_label=label,
        )

    if not data.get("output_format"):
        prepared_format = str(preparation.get("output_format") or "")
        if preparation.get("output_format_specified") and prepared_format:
            output_format = _parse_format(prepared_format, user, service)
            await state.update_data(output_format=output_format.value)
        else:
            await state.set_state(ReportForm.choosing_format)
            await message.answer(
                "Выберите формат:",
                reply_markup=format_keyboard(
                    "report", include_sheets=service.sheets_available(user)
                ),
            )
            return

    await state.set_state(ReportForm.confirming)
    data = await state.get_data()
    await message.answer(
        _confirmation_text(data),
        reply_markup=keyboard(
            [
                [("Сформировать", "report:natural-confirm")],
                [("Отмена", "report:cancel")],
            ]
        ),
    )


async def _show_cities(
    message: Message,
    state: FSMContext,
    user: TelegramUser,
    service: BotReportService,
) -> None:
    cities = service.available_unit_cities(user)
    if not cities:
        raise BotInputError("Для вашего профиля не настроены доступные заведения.")
    await state.set_state(ReportForm.choosing_city)
    await message.answer(
        "Сначала выберите город:",
        reply_markup=unit_cities_keyboard(
            [(item.city_id, item.label, len(item.units)) for item in cities], "report"
        ),
    )


async def _ask_granularity(
    message: Message | None,
    state: FSMContext,
    service: BotReportService,
) -> None:
    if message is None:
        return
    data = await state.get_data()
    await state.set_state(ReportForm.choosing_granularity)
    await message.answer(
        "Как разбить данные по времени?",
        reply_markup=granularity_keyboard("report", _allowed_granularities(data, service)),
    )


async def _ask_channel_or_format(
    message: Message | None,
    state: FSMContext,
    user: TelegramUser,
    service: BotReportService,
) -> None:
    if message is None:
        return
    data = await state.get_data()
    preparation = data.get("preparation") or {}
    options = [str(value) for value in preparation.get("sales_channel_options") or []]
    selection = str(preparation.get("sales_channel_selection") or "")
    if options and not selection:
        await state.set_state(ReportForm.choosing_channel)
        await message.answer(
            "Как учитывать каналы продаж?",
            reply_markup=sales_channel_keyboard(
                options,
                can_split=bool(preparation.get("sales_channel_can_split")),
            ),
        )
        return
    await _ask_format(message, state, user, service)


async def _ask_format(
    message: Message | None,
    state: FSMContext,
    user: TelegramUser,
    service: BotReportService,
) -> None:
    if message is None:
        return
    await state.set_state(ReportForm.choosing_format)
    await message.answer(
        "Выберите формат:",
        reply_markup=format_keyboard("report", include_sheets=service.sheets_available(user)),
    )


def _allowed_granularities(data: dict[str, Any], service: BotReportService) -> list[str]:
    preparation = data.get("preparation") or {}
    report_types = list(preparation.get("report_types") or [])
    allowed: list[str] | None = None
    for report_type in report_types:
        if not service.metrics.has(report_type):
            continue
        granularities = list(service.metrics.get(report_type).granularities)
        if allowed is None:
            allowed = granularities
        else:
            allowed = [item for item in allowed if item in granularities]
    return allowed or list(DEFAULT_GRANULARITIES)


async def _deliver(message: Message, service: BotReportService, result: Any) -> None:
    if result.sheet_url:
        await message.answer(result.text or f"Google Sheets: {result.sheet_url}")
        return
    if result.file_path and result.report_id:
        try:
            await message.answer_document(
                FSInputFile(result.file_path, filename=result.file_name),
                caption=result.text[:1000],
            )
        finally:
            service.cleanup_file(result.report_id)
    else:
        await message.answer(result.text or "Отчёт сформирован.")


async def _ask_make_repeating(message: Message, state: FSMContext) -> None:
    """Ask user if they want to make this a repeating report."""
    await state.set_state(ReportForm.ask_make_repeating)
    await message.answer(
        "Хотите получать этот отчёт регулярно?",
        reply_markup=keyboard(
            [
                [("🔄 Сделать повторяющимся", "report:make_repeating")],
                [("Нет, спасибо", "report:cancel")],
            ]
        ),
    )


def _confirmation_text(data: dict[str, Any]) -> str:
    preparation = data.get("preparation") or {}
    metric = data.get("metric_label") or ", ".join(preparation.get("report_types", []))
    date_from = data.get("date_from") or preparation.get("date_from") or "не требуется"
    date_to = data.get("date_to") or preparation.get("date_to") or "не требуется"
    labels = _unit_labels(data)
    unit_ids = _unit_ids(data) or list(preparation.get("unit_ids") or [])
    if labels and unit_ids:
        unit = ", ".join(labels.get(unit_id, unit_id) for unit_id in unit_ids)
    else:
        unit = data.get("unit_label") or (", ".join(unit_ids) if unit_ids else "не требуется")
    granularity = str(data.get("granularity") or "")
    granularity_label = data.get("granularity_label") or GRANULARITY_LABELS.get(
        granularity, granularity
    )
    lines = [
        f"Тип отчёта: {metric}",
        f"Период: {date_from} — {date_to}",
        f"Подразделение: {unit}",
    ]
    if granularity_label:
        lines.append(f"Детализация: {granularity_label}")
    channel_choice = str(
        data.get("sales_channel_choice") or preparation.get("sales_channel_selection") or ""
    )
    if channel_choice:
        channel_label = data.get("sales_channel_label") or (
            "Разбивка по каналам"
            if channel_choice == "split"
            else "Все каналы вместе"
            if channel_choice == "all"
            else sales_channel_label(channel_choice)
        )
        lines.append(f"Каналы продаж: {channel_label}")
    output_format = str(data.get("output_format") or "")
    lines.append(f"Формат: {FORMAT_LABELS.get(output_format, output_format)}")
    return "\n".join(lines)


async def _ensure_active(state: FSMContext, service: BotReportService) -> None:
    data = await state.get_data()
    started_at = float(data.get("started_at", 0))
    if not started_at or _now_timestamp() - started_at > service.settings.telegram_fsm_ttl_seconds:
        await state.clear()
        raise BotInputError("Диалог устарел. Начните создание отчёта заново.")


def _now_timestamp() -> float:
    return time.time()


def _unit_ids(data: dict[str, Any]) -> list[str]:
    values = data.get("unit_ids") or []
    return [str(item) for item in values]


def _unit_labels(data: dict[str, Any]) -> dict[str, str]:
    values = data.get("unit_labels") or {}
    return {str(key): str(value) for key, value in values.items()}


def _parse_format(value: str, user: TelegramUser, service: BotReportService) -> OutputFormat:
    allowed = allowed_formats(include_sheets=service.sheets_available(user))
    if value not in allowed:
        if value == OutputFormat.SHEETS.value:
            raise BotInputError(DRIVE_NOT_LINKED)
        raise BotInputError("Неизвестный формат.")
    return OutputFormat(value)


def _callback_int(data: str | None, prefix: str, label: str) -> int:
    try:
        return int((data or "").removeprefix(prefix))
    except ValueError as exc:
        raise BotInputError(f"Некорректное значение {label}.") from exc
