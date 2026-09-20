from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import main_menu
from app.bot.messages.texts import HELP_TEXT, METRICS_HEADER, NO_METRICS_AVAILABLE, START_TEXT
from app.bot.services import BotReportService
from app.users.models import TelegramUser

router = Router(name="common")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, telegram_user: TelegramUser) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=main_menu())


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@router.callback_query(F.data == "help:show")
async def help_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(HELP_TEXT, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu())


@router.message(Command("metrics"))
async def metrics_command(
    message: Message, telegram_user: TelegramUser, bot_report_service: BotReportService
) -> None:
    await _show_metrics(message, telegram_user, bot_report_service)


@router.callback_query(F.data == "metrics:show")
async def metrics_callback(
    callback: CallbackQuery,
    telegram_user: TelegramUser,
    bot_report_service: BotReportService,
) -> None:
    await callback.answer()
    if callback.message:
        await _show_metrics(callback.message, telegram_user, bot_report_service)


async def _show_metrics(
    message: Message, telegram_user: TelegramUser, bot_report_service: BotReportService
) -> None:
    categories = bot_report_service.available_categories(telegram_user)
    if not categories:
        await message.answer(NO_METRICS_AVAILABLE, reply_markup=main_menu())
        return

    text_parts = [METRICS_HEADER]
    for category in categories:
        reports = bot_report_service.catalog.reports_for(category.category_id, telegram_user)
        if reports:
            text_parts.append(f"<b>{escape(category.label)}</b>\n")
            for _, label in reports:
                text_parts.append(f"  • {escape(label)}\n")
            text_parts.append("\n")

    text = "".join(text_parts).rstrip()
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())
