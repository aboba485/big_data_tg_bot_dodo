from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.dodo.channels import sales_channel_label


def keyboard(rows: Iterable[Iterable[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_menu() -> InlineKeyboardMarkup:
    return keyboard(
        [
            [("❓ Помощь", "help:show")],
            [("📊 Доступные метрики", "metrics:show")],
            [("🔄 Повторяющиеся отчёты", "weekly:list")],
            [("☁️ Google Drive", "drive:show")],
        ]
    )


def unit_cities_keyboard(
    cities: Iterable[tuple[str, str, int]], prefix: str
) -> InlineKeyboardMarkup:
    return keyboard(
        [[(f"🏙 {label} · {count}", f"{prefix}:city:{city_id}")] for city_id, label, count in cities]
        + [[("Отмена", f"{prefix}:cancel")]]
    )


FORMAT_LABELS = {
    "table": "Текст",
    "csv": "CSV",
    "xlsx": "XLSX",
    "sheets": "Google Sheets",
}


def format_keyboard(prefix: str, *, include_sheets: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [
        [
            ("Текст", f"{prefix}:format:table"),
            ("CSV", f"{prefix}:format:csv"),
            ("XLSX", f"{prefix}:format:xlsx"),
        ]
    ]
    if include_sheets:
        rows.append([("☁️ Google Sheets", f"{prefix}:format:sheets")])
    rows.append([("Отмена", f"{prefix}:cancel")])
    return keyboard(rows)


def allowed_formats(*, include_sheets: bool = False) -> set[str]:
    values = {"table", "csv", "xlsx"}
    if include_sheets:
        values.add("sheets")
    return values


def drive_menu(linked: bool) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = []
    if linked:
        rows.append([("🔌 Отключить Google Drive", "drive:unlink")])
    else:
        rows.append([("🔗 Подключить Google Drive", "drive:link")])
    rows.append([("Назад", "report:cancel")])
    return keyboard(rows)


GRANULARITY_LABELS = {
    "total": "За весь период",
    "day": "По дням",
    "week": "По неделям",
    "month": "По месяцам",
}

VAT_MODE_LABELS = {
    "with_vat": "С НДС",
    "without_vat": "Без НДС",
}


def granularity_keyboard(
    prefix: str,
    granularities: Iterable[str] | None = None,
) -> InlineKeyboardMarkup:
    allowed = set(granularities) if granularities is not None else set(GRANULARITY_LABELS)
    rows = [
        [(label, f"{prefix}:granularity:{value}")]
        for value, label in GRANULARITY_LABELS.items()
        if value in allowed
    ]
    rows.append([("Отмена", f"{prefix}:cancel")])
    return keyboard(rows)


def vat_mode_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = [[(label, f"{prefix}:vat_mode:{value}")] for value, label in VAT_MODE_LABELS.items()]
    rows.append([("Отмена", f"{prefix}:cancel")])
    return keyboard(rows)


def sales_channel_keyboard(
    channels: Iterable[str],
    *,
    can_split: bool,
    prefix: str = "report",
) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [[("Все каналы вместе", f"{prefix}:channel:all")]]
    if can_split:
        rows.append([("Разбить по каналам", f"{prefix}:channel:split")])
    rows.extend([(sales_channel_label(value), f"{prefix}:channel:{value}")] for value in channels)
    rows.append([("Отмена", f"{prefix}:cancel")])
    return keyboard(rows)


def units_multiselect_keyboard(
    units: list[tuple[str, str]],
    selected: set[str],
    page: int,
    prefix: str,
    page_size: int = 8,
    *,
    city_back: bool = False,
) -> InlineKeyboardMarkup:
    last_page = max(0, (len(units) - 1) // page_size) if units else 0
    page = min(max(page, 0), last_page)
    start = page * page_size
    rows: list[list[tuple[str, str]]] = []
    for unit_id, label in units[start : start + page_size]:
        mark = "✓" if unit_id in selected else " "
        rows.append([(f"[{mark}] {label}", f"{prefix}:toggle:{unit_id}")])

    navigation: list[tuple[str, str]] = []
    if page > 0:
        navigation.append(("←", f"{prefix}:units:{page - 1}"))
    if page < last_page:
        navigation.append(("→", f"{prefix}:units:{page + 1}"))
    if navigation:
        rows.append(navigation)

    select_label = "Выбрать город" if city_back else "Выбрать все"
    clear_label = "Очистить город" if city_back else "Очистить"
    rows.append(
        [
            (select_label, f"{prefix}:units:all"),
            (clear_label, f"{prefix}:units:clear"),
        ]
    )
    count = len(selected)
    rows.append([(f"Готово ({count} выбрано)", f"{prefix}:units:done")])
    if city_back:
        rows.append([("← К городам", f"{prefix}:cities")])
    rows.append([("Отмена", f"{prefix}:cancel")])
    return keyboard(rows)
