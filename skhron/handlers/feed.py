"""Лента категории: листаем медиа по одному, свежие сверху."""

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
from skhron.keyboards.callbacks import FeedCB, FeedPickCB, MenuCB
from skhron.keyboards.common import back_to_menu_kb, categories_pick_kb, media_kb
from skhron.services import access
from skhron.utils.media import media_caption, send_media

router = Router(name="feed")

PICK_TEXT = (
    "📼 <b>Лента Схрона</b>\n\n"
    "Выбирай категорию — покажу всё подряд, самое свежее сверху!"
)
NO_ACCESS_TEXT = (
    "Пока у тебя нет доступа ни к одной категории 😕\n\n"
    "Попроси у друга инвайт-ссылку на Схрон — и тут сразу станет веселее!"
)


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


def _nav_row(category_id: int, offset: int, total: int) -> list[InlineKeyboardButton]:
    """[◀️] [позиция/всего] [▶️]; неактивные края и счётчик — noop (offset=-1)."""
    noop = FeedCB(category_id=category_id, offset=-1).pack()
    prev_cb = (
        FeedCB(category_id=category_id, offset=offset - 1).pack()
        if offset > 0
        else noop
    )
    next_cb = (
        FeedCB(category_id=category_id, offset=offset + 1).pack()
        if offset + 1 < total
        else noop
    )
    return [
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=f"{offset + 1}/{total}", callback_data=noop),
        InlineKeyboardButton(text="▶️", callback_data=next_cb),
    ]


async def _show_feed_item(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
    category_id: int,
    offset: int,
) -> None:
    if not await access.can_view(session, user, config, category_id):
        await callback.answer("Нет доступа к этой категории", show_alert=True)
        return

    media, total = await repo.get_feed_item(session, category_id, offset)
    if total == 0:
        await callback.answer("В категории пока пусто 🕸", show_alert=True)
        return
    if media is None or offset >= total:
        # Лента сократилась (файлы удалили) — прыгаем на последний реальный элемент
        offset = min(offset, total - 1)
        media, total = await repo.get_feed_item(session, category_id, offset)
        if media is None:
            await callback.answer("В категории пока пусто 🕸", show_alert=True)
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
            extra_rows=[_nav_row(category_id, offset, total)],
        ),
    )
    # Удаляем предыдущий экран — эффект перелистывания
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == "feed"))
async def feed_menu(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    categories = await access.viewable_categories(session, user, config)
    if not categories:
        await _show_text_screen(callback, bot, NO_ACCESS_TEXT, back_to_menu_kb())
        await callback.answer()
        return
    keyboard = categories_pick_kb(
        categories, make_cb=lambda c: FeedPickCB(category_id=c.id)
    )
    await _show_text_screen(callback, bot, PICK_TEXT, keyboard)
    await callback.answer()


@router.callback_query(FeedPickCB.filter())
async def feed_pick(
    callback: CallbackQuery,
    callback_data: FeedPickCB,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    await _show_feed_item(
        callback, session, user, config, bot, callback_data.category_id, 0
    )


@router.callback_query(FeedCB.filter())
async def feed_page(
    callback: CallbackQuery,
    callback_data: FeedCB,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    if callback_data.offset == -1:
        # noop-кнопка (счётчик позиции)
        await callback.answer()
        return
    await _show_feed_item(
        callback,
        session,
        user,
        config,
        bot,
        callback_data.category_id,
        callback_data.offset,
    )
