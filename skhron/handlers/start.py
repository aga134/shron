"""/start (в т.ч. активация инвайтов по deep-link) и справка."""

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.db import repo
from skhron.db.models import User
from skhron.keyboards.callbacks import MenuCB
from skhron.keyboards.common import back_to_menu_kb, main_menu_kb

router = Router(name="start")


def _greeting_text(user: User) -> str:
    name = html.escape(user.full_name or user.username or "дружище")
    return (
        f"Привет, {name}! 👋\n\n"
        "Это «Схрон» — приватный архив мемов и видосов для своих. "
        "Всё, что жалко потерять в чатах, лежит тут: сохраняем, листаем, "
        "кидаем друг другу.\n\n"
        "Выбирай, что делаем 👇"
    )


def _help_text(is_admin: bool, bot_username: str | None) -> str:
    inline_hint = f"@{bot_username}" if bot_username else "@имя_бота"
    lines = [
        "ℹ️ <b>Что умеет «Схрон»</b>",
        "",
        "🎲 <b>Рандом</b> — случайный мем или видос из доступных категорий",
        "📼 <b>Лента</b> — листай категорию подряд, от свежего к старому",
        "⭐️ <b>Избранное</b> — жми ⭐️ под любым медиа, и оно попадёт "
        "в твою личную подборку",
        "📤 <b>Загрузка</b> — кнопка «Загрузить» или просто пришли мне фото, "
        "видео, гифку, войс, кружок или аудио",
        "🔐 <b>Мои доступы</b> — покажу, какие категории тебе открыты "
        "и куда можно загружать",
        f"🔎 <b>Инлайн-режим</b> — набери {inline_hint} в любом чате "
        "и кидай мемы из Схрона прямо туда",
        "",
        "Если доступов маловато — попроси у админа инвайт-ссылку 🎟",
    ]
    if is_admin:
        lines += ["", "🛠 Ты админ — панель управления тут: /admin"]
    return "\n".join(lines)


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


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    is_admin: bool,
) -> None:
    await state.clear()

    args = command.args or ""
    if args.startswith("inv_"):
        code = args[4:]
        invite = await repo.get_invite_by_code(session, code)
        granted = (
            await repo.redeem_invite(session, invite, user.id)
            if invite is not None
            else []
        )
        if not granted:
            await message.answer(
                "😕 Увы, этот инвайт недействителен или уже истёк.\n"
                "Попроси у того, кто его прислал, свежую ссылку!"
            )
        else:
            lines = "\n".join(f"📁 {html.escape(c.title)}" for c in granted)
            upload_note = (
                "\n\nИ да — в них можно загружать свои находки 📤"
                if invite is not None and invite.can_upload
                else ""
            )
            await message.answer(
                "🎉 Инвайт принят! Теперь тебе открыты категории:\n\n"
                f"{lines}{upload_note}"
            )

    await message.answer(_greeting_text(user), reply_markup=main_menu_kb(is_admin))


@router.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: Message, bot: Bot, is_admin: bool) -> None:
    me = await bot.me()
    await message.answer(
        _help_text(is_admin, me.username), reply_markup=back_to_menu_kb()
    )


@router.callback_query(MenuCB.filter(F.action == "help"))
async def cb_help(callback: CallbackQuery, bot: Bot, is_admin: bool) -> None:
    me = await bot.me()
    await _show_text_screen(
        callback, bot, _help_text(is_admin, me.username), back_to_menu_kb()
    )
    await callback.answer()
