"""Избранное: листаем сохранённые мемы из доступных категорий."""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import User
from skhron.keyboards.callbacks import FavPageCB, MenuCB
from skhron.keyboards.common import back_to_menu_kb, media_kb
from skhron.services import access
from skhron.utils.media import media_caption, send_media

router = Router(name="favorites")

EMPTY_TEXT = "В избранном пусто. Жми ⭐️ под любым мемом!"


def _chat_id(callback: CallbackQuery) -> int:
    if callback.message is not None:
        return callback.message.chat.id
    return callback.from_user.id


async def _show_text_screen(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """Правило: сообщение с медиа нельзя edit_text — тогда удаляем и шлём новое."""
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=reply_markup)
            return
        except TelegramAPIError as e:
            if "message is not modified" in str(e).lower():
                return
            try:
                await message.delete()
            except TelegramAPIError:
                pass
    await bot.send_message(_chat_id(callback), text, reply_markup=reply_markup)


def _nav_row(offset: int, total: int) -> list[InlineKeyboardButton]:
    """[◀️] [позиция/всего] [▶️]; неактивные края и счётчик — noop (offset=-1)."""
    noop = FavPageCB(offset=-1).pack()
    prev_cb = FavPageCB(offset=offset - 1).pack() if offset > 0 else noop
    next_cb = FavPageCB(offset=offset + 1).pack() if offset + 1 < total else noop
    return [
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=f"{offset + 1}/{total}", callback_data=noop),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ]


async def _show_favorite_item(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
    offset: int,
) -> None:
    category_ids = await access.viewable_category_ids(session, user, config)
    media, total = await repo.get_favorite_item(
        session, user.id, category_ids, offset
    )
    if total == 0:
        await _show_text_screen(callback, bot, EMPTY_TEXT, back_to_menu_kb())
        await callback.answer()
        return
    if media is None or offset >= total:
        # Избранное сократилось (файлы удалили) — прыгаем на последний элемент
        offset = min(offset, total - 1)
        media, total = await repo.get_favorite_item(
            session, user.id, category_ids, offset
        )
        if media is None:
            await _show_text_screen(callback, bot, EMPTY_TEXT, back_to_menu_kb())
            await callback.answer()
            return

    category = await repo.get_category(session, media.category_id)
    deletable = await access.can_delete_media(
        session, user, config, media.uploaded_by
    )
    await send_media(
        bot,
        _chat_id(callback),
        media,
        caption=media_caption(media, category),
        reply_markup=media_kb(
            media.id,
            deletable=deletable,
            extra_rows=[_nav_row(offset, total)],
        ),
    )
    # Удаляем предыдущий экран — эффект перелистывания
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "favorites"))
async def favorites_menu(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    await _show_favorite_item(callback, session, user, config, bot, offset=0)


@router.callback_query(FavPageCB.filter())
async def favorites_page(
    callback: CallbackQuery,
    callback_data: FavPageCB,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    if callback_data.offset == -1:
        # noop-кнопка (счётчик позиции)
        await callback.answer()
        return
    await _show_favorite_item(
        callback, session, user, config, bot, callback_data.offset
    )
