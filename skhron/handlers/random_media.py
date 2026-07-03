"""Рандом: выбор категории и выдача случайного медиа из Схрона."""

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
from skhron.keyboards.callbacks import MenuCB, RandomCB
from skhron.keyboards.common import back_to_menu_kb, categories_pick_kb, media_kb
from skhron.services import access
from skhron.utils.media import media_caption, send_media

router = Router(name="random_media")

PICK_TEXT = (
    "🎲 <b>Рандом из Схрона</b>\n\n"
    "Выбирай категорию — или жми «Из всех доступных», "
    "и я вытащу что-нибудь наугад 😏"
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


@router.callback_query(MenuCB.filter(F.action == "random"))
async def random_menu(
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
        categories,
        make_cb=lambda c: RandomCB(category_id=c.id),
        header_buttons=[
            InlineKeyboardButton(
                text="🎲 Из всех доступных",
                callback_data=RandomCB(category_id=0).pack(),
            )
        ],
    )
    await _show_text_screen(callback, bot, PICK_TEXT, keyboard)
    await callback.answer()


@router.callback_query(RandomCB.filter())
async def show_random(
    callback: CallbackQuery,
    callback_data: RandomCB,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    if callback_data.category_id == 0:
        category_ids = await access.viewable_category_ids(session, user, config)
    else:
        if not await access.can_view(
            session, user, config, callback_data.category_id
        ):
            await callback.answer("Нет доступа к этой категории", show_alert=True)
            return
        category_ids = [callback_data.category_id]

    media = await repo.get_random_media(session, category_ids)
    if media is None:
        await callback.answer("Тут пока пусто 🕸", show_alert=True)
        return

    category = await repo.get_category(session, media.category_id)
    deletable = await access.can_delete_media(
        session, user, config, media.uploaded_by
    )
    more_row = [
        InlineKeyboardButton(
            text="🎲 Ещё",
            callback_data=RandomCB(
                category_id=callback_data.category_id
            ).pack(),
        )
    ]
    # Старые сообщения не удаляем — пусть остаются в чате
    await send_media(
        bot,
        _chat_id(callback),
        media,
        caption=media_caption(media, category),
        reply_markup=media_kb(media.id, deletable=deletable, extra_rows=[more_row]),
    )
    await callback.answer()
