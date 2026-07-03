"""Главное меню и экран «Мои доступы»."""

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import User
from skhron.keyboards.callbacks import MenuCB
from skhron.keyboards.common import back_to_menu_kb, main_menu_kb
from skhron.services import access

router = Router(name="menu")

MENU_TEXT = (
    "🗄 <b>«Схрон» — главное меню</b>\n\n"
    "Мемы сами себя не пересмотрят. Что будем делать?"
)


async def _show_text_screen(
    callback: CallbackQuery, bot: Bot, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Показывает текстовый экран поверх сообщения callback'а.

    Медиа-сообщение нельзя edit_text — тогда удаляем и шлём новое.
    """
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except TelegramAPIError as e:
            if "message is not modified" in str(e).lower():
                return
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            await message.answer(text, reply_markup=kb)
            return
    await bot.send_message(callback.from_user.id, text, reply_markup=kb)


@router.message(Command("menu"), F.chat.type == "private")
async def cmd_menu(message: Message, state: FSMContext, is_admin: bool) -> None:
    await state.clear()
    await message.answer(MENU_TEXT, reply_markup=main_menu_kb(is_admin))


@router.callback_query(MenuCB.filter(F.action == "home"))
async def cb_home(
    callback: CallbackQuery, state: FSMContext, is_admin: bool, bot: Bot
) -> None:
    await state.clear()
    await _show_text_screen(callback, bot, MENU_TEXT, main_menu_kb(is_admin))
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "access"))
async def cb_access(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    is_admin: bool,
    bot: Bot,
) -> None:
    viewable = await access.viewable_categories(session, user, config)
    uploadable_ids = {
        c.id for c in await access.uploadable_categories(session, user, config)
    }

    lines: list[str] = ["🔐 <b>Мои доступы</b>", ""]
    if is_admin:
        lines += ["👑 Ты админ — тебе доступно всё.", ""]

    if viewable:
        for category in viewable:
            count = await repo.count_media(session, category.id)
            upload_mark = " 📤" if category.id in uploadable_ids else ""
            lines.append(
                f"📁 {html.escape(category.title)} — {count} шт.{upload_mark}"
            )
        lines += ["", "<i>📤 — в эту категорию можно загружать</i>"]
    elif is_admin:
        lines.append(
            "Правда, категорий в Схроне пока нет — создай первую через /admin 😉"
        )
    else:
        lines.append("У тебя пока нет доступов. Попроси у админа инвайт-ссылку 🎟")

    await _show_text_screen(callback, bot, "\n".join(lines), back_to_menu_kb())
    await callback.answer()
