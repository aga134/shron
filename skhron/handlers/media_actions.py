"""Кнопки под медиа: избранное и удаление (общие для рандома/ленты/избранного)."""

from aiogram import F, Router
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
from skhron.keyboards.callbacks import MediaActionCB
from skhron.keyboards.common import back_to_menu_kb, confirm_delete_kb, media_kb
from skhron.services import access

router = Router(name="media_actions")


def _swap_row(
    markup: InlineKeyboardMarkup | None,
    match_cb: str,
    new_row: list[InlineKeyboardButton],
    fallback: InlineKeyboardMarkup,
) -> InlineKeyboardMarkup:
    """Заменяет ряд, содержащий кнопку match_cb, сохраняя остальные ряды
    (навигацию ленты, «🎲 Ещё» и т.п.). Если разметки нет — fallback."""
    if markup is None:
        return fallback
    return InlineKeyboardMarkup(
        inline_keyboard=[
            new_row if any(btn.callback_data == match_cb for btn in row) else row
            for row in markup.inline_keyboard
        ]
    )


@router.callback_query(MediaActionCB.filter(F.action == "fav"))
async def toggle_favorite(
    callback: CallbackQuery,
    callback_data: MediaActionCB,
    session: AsyncSession,
    user: User,
    config: Config,
) -> None:
    media = await repo.get_media(session, callback_data.media_id)
    if media is None or media.is_deleted:
        await callback.answer("Этот файл уже удалён из Схрона", show_alert=True)
        return
    if not await access.can_view(session, user, config, media.category_id):
        # то же сообщение, что и для удалённых: не раскрываем, существует ли файл
        await callback.answer("Этот файл уже удалён из Схрона", show_alert=True)
        return
    added = await repo.toggle_favorite(session, user.id, media.id)
    await callback.answer("⭐️ В избранном!" if added else "Убрано из избранного")


@router.callback_query(MediaActionCB.filter(F.action == "del"))
async def ask_delete(
    callback: CallbackQuery,
    callback_data: MediaActionCB,
    session: AsyncSession,
    user: User,
    config: Config,
) -> None:
    media = await repo.get_media(session, callback_data.media_id)
    if media is None or media.is_deleted:
        await callback.answer("Уже удалено", show_alert=True)
        return
    if not await access.can_delete_media(session, user, config, media.uploaded_by):
        await callback.answer(
            "Удалять может только загрузивший или админ", show_alert=True
        )
        return
    message = callback.message
    if not isinstance(message, Message):
        # сообщение старше 48 часов: клавиатуру не поменять
        await callback.answer("Кнопка устарела 🕰 Найди файл заново", show_alert=True)
        return
    confirm = confirm_delete_kb(media.id)
    try:
        await message.edit_reply_markup(
            reply_markup=_swap_row(
                message.reply_markup,
                MediaActionCB(action="del", media_id=media.id).pack(),
                confirm.inline_keyboard[0],
                confirm,
            )
        )
    except TelegramAPIError:
        pass
    await callback.answer()


@router.callback_query(MediaActionCB.filter(F.action == "delc"))
async def confirm_delete(
    callback: CallbackQuery,
    callback_data: MediaActionCB,
    session: AsyncSession,
    user: User,
    config: Config,
) -> None:
    media = await repo.get_media(session, callback_data.media_id)
    if media is None or media.is_deleted:
        await callback.answer("Уже удалено", show_alert=True)
        return
    if not await access.can_delete_media(session, user, config, media.uploaded_by):
        await callback.answer(
            "Удалять может только загрузивший или админ", show_alert=True
        )
        return
    await repo.soft_delete_media(session, media.id)
    message = callback.message
    if isinstance(message, Message):
        deleted = False
        try:
            await message.delete()
            deleted = True
        except TelegramAPIError:
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramAPIError:
                pass
        if deleted:
            # не оставляем юзера без единой кнопки (лента к этому моменту
            # уже удалила предыдущие сообщения «перелистыванием»)
            try:
                await message.answer(
                    "🗑 Удалено из Схрона", reply_markup=back_to_menu_kb()
                )
            except TelegramAPIError:
                pass
    await callback.answer("🗑 Удалено из Схрона")


@router.callback_query(MediaActionCB.filter(F.action == "delx"))
async def cancel_delete(
    callback: CallbackQuery,
    callback_data: MediaActionCB,
) -> None:
    message = callback.message
    if isinstance(message, Message):
        actions = media_kb(callback_data.media_id, deletable=True)
        try:
            await message.edit_reply_markup(
                reply_markup=_swap_row(
                    message.reply_markup,
                    MediaActionCB(
                        action="delc", media_id=callback_data.media_id
                    ).pack(),
                    actions.inline_keyboard[0],
                    actions,
                )
            )
        except TelegramAPIError:
            pass
    await callback.answer()
