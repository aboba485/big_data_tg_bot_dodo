from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.errors import BotInputError
from app.bot.keyboards import drive_menu, main_menu
from app.bot.messages.texts import (
    DRIVE_LINK_PROMPT,
    DRIVE_NOT_CONFIGURED,
    DRIVE_STATUS_LINKED,
    DRIVE_STATUS_UNLINKED,
    DRIVE_UNLINK_MISSING,
    DRIVE_UNLINKED,
)
from app.errors import ApplicationError
from app.google_drive.service import GoogleDriveService
from app.users.models import TelegramUser

router = Router(name="google_drive")


@router.message(Command("drive"))
async def drive_command(
    message: Message,
    state: FSMContext,
    telegram_user: TelegramUser,
    google_drive: GoogleDriveService,
) -> None:
    await state.clear()
    await _show_status(message, telegram_user, google_drive)


@router.callback_query(F.data == "drive:show")
async def drive_show_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_user: TelegramUser,
    google_drive: GoogleDriveService,
) -> None:
    await callback.answer()
    await state.clear()
    if callback.message:
        await _show_status(callback.message, telegram_user, google_drive)


@router.callback_query(F.data == "drive:link")
async def drive_link_callback(
    callback: CallbackQuery,
    telegram_user: TelegramUser,
    google_drive: GoogleDriveService,
) -> None:
    await callback.answer()
    if not callback.message:
        return
    if not google_drive.enabled:
        await callback.message.answer(DRIVE_NOT_CONFIGURED, reply_markup=main_menu())
        return
    try:
        url = google_drive.create_auth_url(telegram_user.telegram_id)
    except ApplicationError as exc:
        raise BotInputError(exc.message) from exc
    minutes = max(google_drive.settings.google_oauth_state_ttl_seconds // 60, 1)
    await callback.message.answer(
        DRIVE_LINK_PROMPT.format(minutes=minutes),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Разрешить доступ в Google", url=url)],
                [InlineKeyboardButton(text="Отмена", callback_data="report:cancel")],
            ]
        ),
    )


@router.callback_query(F.data == "drive:unlink")
async def drive_unlink_callback(
    callback: CallbackQuery,
    telegram_user: TelegramUser,
    google_drive: GoogleDriveService,
) -> None:
    await callback.answer()
    removed = google_drive.unlink(telegram_user.telegram_id)
    if callback.message:
        await callback.message.answer(
            DRIVE_UNLINKED if removed else DRIVE_UNLINK_MISSING,
            reply_markup=main_menu(),
        )


async def _show_status(
    message: Message, user: TelegramUser, google_drive: GoogleDriveService
) -> None:
    if not google_drive.enabled:
        await message.answer(DRIVE_NOT_CONFIGURED, reply_markup=main_menu())
        return
    email = google_drive.linked_email(user.telegram_id)
    if email:
        await message.answer(DRIVE_STATUS_LINKED.format(email=email), reply_markup=drive_menu(True))
        return
    await message.answer(DRIVE_STATUS_UNLINKED, reply_markup=drive_menu(False))
