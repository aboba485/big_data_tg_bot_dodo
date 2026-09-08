from __future__ import annotations

import time
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

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
from app.bot.messages.texts import DRIVE_NOT_LINKED
from app.bot.services import BotReportService
from app.bot.states.reports import ScheduledReportForm
from app.dodo.channels import sales_channel_label
from app.planner.schemas import Granularity
from app.storage.weekly_reports import (
    ScheduledReportLimitError,
    WeeklyReportRepository,
    encode_subscription_unit_ids,
    parse_subscription_unit_ids,
)
from app.users.models import TelegramUser

# Alias for backward compatibility
WeeklyReportForm = ScheduledReportForm

router = Router(name="weekly_reports")
WEEKDAYS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
FREQUENCY_LABELS = {
    "weekly": "Раз в неделю",
    "monthly": "Раз в месяц",
}
SCHEDULED_REPORT_HOURS = tuple(range(6, 23))
# Alias for backward compatibility
WEEKLY_REPORT_HOURS = SCHEDULED_REPORT_HOURS
DEFAULT_GRANULARITIES = ["total", "day", "week", "month"]


@router.message(Command("schedules"))
async def schedules_command(
    message: Message,
    state: FSMContext,
    telegram_user: TelegramUser,
    weekly_reports: WeeklyReportRepository,
    bot_report_service: BotReportService,
) -> None:
    await state.clear()
    await _show_list(message, telegram_user, weekly_reports, bot_report_service)


@router.callback_query(F.data == "weekly:list")
async def schedules_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    weekly_reports: WeeklyReportRepository,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    await state.clear()
    if callback.message:
        await _show_list(callback.message, telegram_user, weekly_reports, bot_report_service)


@router.callback_query(F.data == "weekly:new")
async def weekly_new(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    units = bot_report_service.available_units(telegram_user)
    if not units:
        raise BotInputError("Для вашего профиля не настроены доступные заведения.")
    await state.clear()
    await state.set_state(WeeklyReportForm.waiting_query)
    await state.update_data(started_at=time.time())
    if callback.message:
        await callback.message.answer(
            "Напишите, какой отчёт нужно присылать регулярно.\n"
            "Например: «Покажи выручку» или «Продажи по каналам».",
            reply_markup=keyboard([[("Отмена", "weekly:cancel")]]),
        )


@router.message(WeeklyReportForm.waiting_query, F.text)
async def weekly_query(
    message: Message,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await _ensure_active(state, bot_report_service)
    query = message.text or ""
    await message.answer("⏳ Разбираю запрос…")
    preparation = await bot_report_service.prepare(telegram_user, query, defer_units=True)
    if preparation.status == "needs_clarification":
        await message.answer(preparation.question or "Уточните параметры отчёта.")
        return
    if preparation.status == "unsupported":
        await state.clear()
        await message.answer(
            preparation.reason or "Этот отчёт не поддерживается.",
            reply_markup=main_menu(),
        )
        return

    available = dict(bot_report_service.available_reports(telegram_user))
    metric_id = next(
        (item for item in preparation.report_types if item in available),
        None,
    )
    if metric_id is None:
        raise BotInputError(
            "Не удалось определить тип отчёта. Опишите метрику, например: «Покажи выручку»."
        )

    units = bot_report_service.available_units(telegram_user)
    available_units = dict(units)
    preselected = [unit_id for unit_id in preparation.unit_ids if unit_id in available_units]
    labels = {unit_id: available_units[unit_id] for unit_id in preselected}
    await state.set_state(WeeklyReportForm.choosing_city)
    await state.update_data(
        source_query=query,
        preparation=preparation.model_dump(mode="json"),
        metric_id=metric_id,
        metric_label=available[metric_id],
        unit_ids=preselected,
        unit_labels=labels,
        units_page=0,
        started_at=time.time(),
    )
    await _show_cities(message, state, telegram_user, bot_report_service)


@router.callback_query(WeeklyReportForm.choosing_city, F.data.startswith("weekly:city:"))
async def weekly_city(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    city_id = (callback.data or "").removeprefix("weekly:city:")
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
    await state.set_state(WeeklyReportForm.choosing_unit)
    if callback.message:
        await callback.message.answer(
            f"🏙 {city.label}\nВыберите одно или несколько заведений:",
            reply_markup=units_multiselect_keyboard(
                list(city.units), set(_unit_ids(data)), 0, "weekly", city_back=True
            ),
        )


@router.callback_query(WeeklyReportForm.choosing_unit, F.data == "weekly:cities")
async def weekly_cities(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    if callback.message:
        await _show_cities(callback.message, state, telegram_user, bot_report_service)


@router.callback_query(WeeklyReportForm.choosing_unit, F.data.startswith("weekly:toggle:"))
async def weekly_toggle_unit(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    unit_id = (callback.data or "").removeprefix("weekly:toggle:")
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
                "weekly",
                city_back=True,
            )
        )


@router.callback_query(WeeklyReportForm.choosing_unit, F.data.startswith("weekly:units:"))
async def weekly_units_action(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    action = (callback.data or "").removeprefix("weekly:units:")
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
                    units, selected, page, "weekly", city_back=True
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
                    units, selected, page, "weekly", city_back=True
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
        await state.set_state(WeeklyReportForm.choosing_granularity)
        if callback.message:
            await callback.message.answer(
                "Как разбить данные по времени?",
                reply_markup=granularity_keyboard(
                    "weekly", _allowed_granularities(data, bot_report_service)
                ),
            )
        return

    await callback.answer()
    page = _callback_int(callback.data, "weekly:units:", "страницы")
    selected = set(_unit_ids(data))
    await state.update_data(units_page=page)
    if callback.message:
        await callback.message.edit_reply_markup(
            reply_markup=units_multiselect_keyboard(units, selected, page, "weekly", city_back=True)
        )


@router.callback_query(
    ScheduledReportForm.choosing_granularity, F.data.startswith("weekly:granularity:")
)
async def weekly_granularity(
    callback: CallbackQuery, state: FSMContext, bot_report_service: BotReportService
) -> None:
    await callback.answer()
    value = (callback.data or "").removeprefix("weekly:granularity:")
    data = await state.get_data()
    allowed = _allowed_granularities(data, bot_report_service)
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
    await _ask_channel_or_frequency(callback.message, state)


@router.callback_query(ScheduledReportForm.choosing_channel, F.data.startswith("weekly:channel:"))
async def weekly_sales_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    choice = (callback.data or "").removeprefix("weekly:channel:")
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
    await _ask_frequency(callback.message, state)


@router.callback_query(ScheduledReportForm.choosing_frequency, F.data.startswith("weekly:freq:"))
async def schedule_frequency(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    frequency = (callback.data or "").removeprefix("weekly:freq:")
    if frequency not in ("weekly", "monthly"):
        raise BotInputError("Неизвестная периодичность.")
    await state.update_data(
        frequency=frequency,
        frequency_label=FREQUENCY_LABELS.get(frequency, frequency),
    )
    if frequency == "monthly":
        await state.set_state(ScheduledReportForm.choosing_day_of_month)
        if callback.message:
            await callback.message.answer(
                "В какой день месяца присылать отчёт?",
                reply_markup=_day_of_month_keyboard(),
            )
    else:
        await state.set_state(ScheduledReportForm.choosing_weekday)
        if callback.message:
            await callback.message.answer(
                "В какой день недели присылать отчёт?",
                reply_markup=keyboard(
                    [[(label, f"weekly:day:{day}")] for day, label in enumerate(WEEKDAYS)]
                    + [[("Отмена", "weekly:cancel")]]
                ),
            )


@router.callback_query(ScheduledReportForm.choosing_day_of_month, F.data.startswith("weekly:dom:"))
async def schedule_day_of_month(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    day = _callback_int(callback.data, "weekly:dom:", "дня месяца")
    if day not in range(1, 29):
        raise BotInputError("День месяца должен быть от 1 до 28.")
    await state.update_data(day_of_month=day, weekday=0)
    await state.set_state(ScheduledReportForm.choosing_time)
    if callback.message:
        await callback.message.answer(
            "Во сколько присылать отчёт?",
            reply_markup=_time_keyboard(),
        )


@router.callback_query(ScheduledReportForm.choosing_weekday, F.data.startswith("weekly:day:"))
async def weekly_day(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    weekday = _callback_int(callback.data, "weekly:day:", "дня недели")
    if weekday not in range(7):
        raise BotInputError("Неизвестный день недели.")
    await state.update_data(weekday=weekday, day_of_month=None)
    await state.set_state(ScheduledReportForm.choosing_time)
    if callback.message:
        await callback.message.answer(
            "Во сколько присылать отчёт?",
            reply_markup=_time_keyboard(),
        )


@router.callback_query(ScheduledReportForm.choosing_time, F.data.startswith("weekly:time:"))
async def weekly_time(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    hour = _callback_int(callback.data, "weekly:time:", "времени")
    if hour not in SCHEDULED_REPORT_HOURS:
        raise BotInputError("Недоступное время отправки.")
    await state.update_data(local_hour=hour)
    await state.set_state(ScheduledReportForm.choosing_format)
    if callback.message:
        await callback.message.answer(
            "Выберите формат:",
            reply_markup=format_keyboard(
                "weekly", include_sheets=bot_report_service.sheets_available(telegram_user)
            ),
        )


@router.callback_query(ScheduledReportForm.choosing_format, F.data.startswith("weekly:format:"))
async def weekly_format(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    output_format = (callback.data or "").removeprefix("weekly:format:")
    allowed = allowed_formats(include_sheets=bot_report_service.sheets_available(telegram_user))
    if output_format not in allowed:
        if output_format == "sheets":
            raise BotInputError(DRIVE_NOT_LINKED)
        raise BotInputError("Неизвестный формат.")
    await state.update_data(output_format=output_format)
    await state.set_state(ScheduledReportForm.confirming)
    data = await state.get_data()
    if callback.message:
        await callback.message.answer(
            _confirmation(data),
            reply_markup=keyboard(
                [[("Сохранить", "weekly:confirm")], [("Отмена", "weekly:cancel")]]
            ),
        )


@router.callback_query(ScheduledReportForm.confirming, F.data == "weekly:confirm")
async def weekly_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    weekly_reports: WeeklyReportRepository,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    await _ensure_active(state, bot_report_service)
    data = await state.get_data()
    metric_id = str(data["metric_id"])
    unit_ids = _unit_ids(data)
    available_units = dict(bot_report_service.available_units(telegram_user))
    if metric_id not in dict(bot_report_service.available_reports(telegram_user)):
        raise BotInputError("Этот отчёт больше недоступен.")
    if not unit_ids or any(unit_id not in available_units for unit_id in unit_ids):
        raise BotInputError("Выбранные заведения больше недоступны.")
    output_format = str(data["output_format"])
    if output_format == "sheets" and not bot_report_service.sheets_available(telegram_user):
        raise BotInputError(DRIVE_NOT_LINKED)
    if not callback.message:
        return
    frequency = str(data.get("frequency") or "weekly")
    day_of_month = data.get("day_of_month")
    try:
        weekly_reports.create(
            telegram_id=telegram_user.telegram_id,
            chat_id=callback.message.chat.id,
            metric_id=metric_id,
            unit_id=encode_subscription_unit_ids(unit_ids),
            output_format=output_format,
            granularity=str(data.get("granularity") or Granularity.TOTAL.value),
            sales_channel_choice=str(
                data.get("sales_channel_choice")
                or (data.get("preparation") or {}).get("sales_channel_selection")
                or ""
            ),
            frequency=frequency,
            weekday=int(data.get("weekday") or 0),
            day_of_month=int(day_of_month) if day_of_month is not None else None,
            local_hour=int(data["local_hour"]),
            timezone_name=bot_report_service.settings.app_timezone,
            max_per_user=bot_report_service.settings.telegram_max_weekly_reports_per_user,
        )
    except ScheduledReportLimitError as exc:
        raise BotInputError(str(exc)) from exc
    await state.clear()
    freq_label = "ежемесячный" if frequency == "monthly" else "еженедельный"
    await callback.message.answer(
        f"Повторяющийся ({freq_label}) отчёт сохранён.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data.startswith("weekly:delete:"))
async def weekly_delete(
    callback: CallbackQuery,
    telegram_user: TelegramUser,
    weekly_reports: WeeklyReportRepository,
) -> None:
    await callback.answer()
    try:
        subscription_id = int((callback.data or "").removeprefix("weekly:delete:"))
    except ValueError as exc:
        raise BotInputError("Некорректный идентификатор подписки.") from exc
    deleted = weekly_reports.delete_for_user(subscription_id, telegram_user.telegram_id)
    if callback.message:
        await callback.message.answer(
            "Подписка удалена." if deleted else "Подписка не найдена.",
            reply_markup=main_menu(),
        )


@router.callback_query(F.data == "weekly:cancel")
async def weekly_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    await state.clear()
    if callback.message:
        await callback.message.answer("Действие отменено.", reply_markup=main_menu())


async def _show_list(
    message: Message,
    user: TelegramUser,
    repository: WeeklyReportRepository,
    service: BotReportService,
) -> None:
    items = repository.list_for_user(user.telegram_id)
    names = dict(service.available_units(user))
    lines = []
    for item in items:
        unit_ids = parse_subscription_unit_ids(item.unit_id)
        unit_label = ", ".join(names.get(unit_id, unit_id[:8]) for unit_id in unit_ids)
        granularity_label = GRANULARITY_LABELS.get(item.granularity, item.granularity)
        if item.frequency == "monthly" and item.day_of_month is not None:
            schedule_label = f"{item.day_of_month}-е число, {item.local_hour:02d}:00"
            freq_icon = "📆"
        else:
            schedule_label = f"{WEEKDAYS[item.weekday]} {item.local_hour:02d}:00"
            freq_icon = "📅"
        channel_label = ""
        if item.sales_channel_choice:
            channel_label = (
                "разбивка по каналам"
                if item.sales_channel_choice == "split"
                else "все каналы вместе"
                if item.sales_channel_choice == "all"
                else sales_channel_label(item.sales_channel_choice)
            )
        lines.append(
            f"#{item.id} {freq_icon}: {item.metric_id}, {unit_label}, {granularity_label}, "
            f"{channel_label + ', ' if channel_label else ''}"
            f"{schedule_label}, "
            f"{FORMAT_LABELS.get(item.output_format, item.output_format)}"
        )
    rows = [[("➕ Добавить", "weekly:new")]]
    rows.extend([[(f"Удалить #{item.id}", f"weekly:delete:{item.id}")] for item in items])
    rows.append([("Назад", "report:cancel")])
    await message.answer(
        "Запланированные отчёты:\n" + ("\n".join(lines) if lines else "Пока нет подписок."),
        reply_markup=keyboard(rows),
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
    await state.set_state(WeeklyReportForm.choosing_city)
    await message.answer(
        "Сначала выберите город:",
        reply_markup=unit_cities_keyboard(
            [(item.city_id, item.label, len(item.units)) for item in cities], "weekly"
        ),
    )


async def _ask_channel_or_frequency(
    message: Message | None,
    state: FSMContext,
) -> None:
    if message is None:
        return
    data = await state.get_data()
    preparation = data.get("preparation") or {}
    options = [str(value) for value in preparation.get("sales_channel_options") or []]
    selection = str(preparation.get("sales_channel_selection") or "")
    if options and not selection:
        await state.set_state(ScheduledReportForm.choosing_channel)
        await message.answer(
            "Как учитывать каналы продаж?",
            reply_markup=sales_channel_keyboard(
                options,
                can_split=bool(preparation.get("sales_channel_can_split")),
                prefix="weekly",
            ),
        )
        return
    await _ask_frequency(message, state)


async def _ask_frequency(message: Message | None, state: FSMContext) -> None:
    if message is None:
        return
    await state.set_state(ScheduledReportForm.choosing_frequency)
    await message.answer(
        "Как часто присылать отчёт?",
        reply_markup=keyboard(
            [
                [("📅 Раз в неделю", "weekly:freq:weekly")],
                [("📆 Раз в месяц", "weekly:freq:monthly")],
                [("Отмена", "weekly:cancel")],
            ]
        ),
    )


def _confirmation(data: dict[str, object]) -> str:
    granularity = str(data.get("granularity") or "")
    granularity_label = data.get("granularity_label") or GRANULARITY_LABELS.get(
        granularity, granularity
    )
    frequency = str(data.get("frequency") or "weekly")
    frequency_label = FREQUENCY_LABELS.get(frequency, frequency)
    if frequency == "monthly":
        day_of_month = data.get("day_of_month")
        schedule_label = f"{day_of_month}-е число, {int(data['local_hour']):02d}:00"
        period_info = "Период каждого отчёта: предыдущий завершённый месяц."
    else:
        weekday_idx = int(data.get("weekday") or 0)
        schedule_label = f"{WEEKDAYS[weekday_idx]}, {int(data['local_hour']):02d}:00"
        period_info = "Период каждого отчёта: предыдущая завершённая неделя (пн–вс)."
    preparation = data.get("preparation") or {}
    channel_choice = str(
        data.get("sales_channel_choice") or preparation.get("sales_channel_selection") or ""
    )
    channel_line = ""
    if channel_choice:
        channel_label = (
            "Разбивка по каналам"
            if channel_choice == "split"
            else "Все каналы вместе"
            if channel_choice == "all"
            else sales_channel_label(channel_choice)
        )
        channel_line = f"Каналы продаж: {channel_label}\n"
    return (
        f"Заведения: {data['unit_label']}\n"
        f"Отчёт: {data['metric_label']}\n"
        f"Детализация: {granularity_label}\n"
        f"{channel_line}"
        f"Периодичность: {frequency_label}\n"
        f"Расписание: {schedule_label}\n"
        f"Формат: {FORMAT_LABELS.get(str(data['output_format']), str(data['output_format']))}\n"
        f"{period_info}"
    )


def _allowed_granularities(data: dict[str, Any], service: BotReportService) -> list[str]:
    metric_id = str(data.get("metric_id") or "")
    if service.metrics.has(metric_id):
        return list(service.metrics.get(metric_id).granularities)
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


def _unit_ids(data: dict[str, Any]) -> list[str]:
    values = data.get("unit_ids") or []
    return [str(item) for item in values]


def _unit_labels(data: dict[str, Any]) -> dict[str, str]:
    values = data.get("unit_labels") or {}
    return {str(key): str(value) for key, value in values.items()}


def _time_keyboard():
    columns = 4
    rows = [
        [
            (f"{hour:02d}:00", f"weekly:time:{hour}")
            for hour in SCHEDULED_REPORT_HOURS[start : start + columns]
        ]
        for start in range(0, len(SCHEDULED_REPORT_HOURS), columns)
    ]
    rows.append([("Отмена", "weekly:cancel")])
    return keyboard(rows)


def _day_of_month_keyboard():
    columns = 7
    days = list(range(1, 29))
    rows = [
        [(str(day), f"weekly:dom:{day}") for day in days[start : start + columns]]
        for start in range(0, len(days), columns)
    ]
    rows.append([("Отмена", "weekly:cancel")])
    return keyboard(rows)


def _callback_int(data: str | None, prefix: str, label: str) -> int:
    try:
        return int((data or "").removeprefix(prefix))
    except ValueError as exc:
        raise BotInputError(f"Некорректное значение {label}.") from exc


async def _ensure_active(state: FSMContext, service: BotReportService) -> None:
    data = await state.get_data()
    started_at = float(data.get("started_at", 0))
    if not started_at or time.time() - started_at > service.settings.telegram_fsm_ttl_seconds:
        await state.clear()
        raise BotInputError("Настройка расписания устарела. Начните заново.")
