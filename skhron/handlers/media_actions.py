"""Кнопки под медиа: избранное, удаление и подпись задним числом
(общие для рандома/ленты/избранного)."""

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
from skhron.utils.fsm import clear_state_keep_pending
from skhron.filters import PrivateCallback

router = Router(name="media_actions")
# кнопки личных экранов, пересланные в группу, там не работают
router.callback_query.filter(PrivateCallback())

# Лимит длины подписи: должен помещаться в caption Telegram (1024) вместе
# со служебной припиской «📁 Категория» при показе медиа
CAPTION_MAX_LEN = 800


class CaptionStates(StatesGroup):
    waiting_caption = State()


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
    # Единый ответ для «нет файла / удалён / нет доступа к категории» —
    # как в toggle_favorite: не раскрываем, существует ли файл, и не даём
    # удалять после полного отзыва доступа
    if (
        media is None
        or media.is_deleted
        or not await access.can_view(session, user, config, media.category_id)
    ):
        await callback.answer("Этот файл уже удалён из Схрона", show_alert=True)
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
    # Единый ответ для «нет файла / удалён / нет доступа к категории» —
    # как в toggle_favorite: не раскрываем, существует ли файл, и не даём
    # удалять после полного отзыва доступа
    if (
        media is None
        or media.is_deleted
        or not await access.can_view(session, user, config, media.category_id)
    ):
        await callback.answer("Этот файл уже удалён из Схрона", show_alert=True)
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


# ---------------------------------------------------------------- подпись ✏️


def _caption_cancel_kb(media_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data=MediaActionCB(
                        action="capx", media_id=media_id
                    ).pack(),
                )
            ]
        ]
    )


@router.callback_query(MediaActionCB.filter(F.action == "cap"))
async def ask_caption(
    callback: CallbackQuery,
    callback_data: MediaActionCB,
    session: AsyncSession,
    user: User,
    config: Config,
    state: FSMContext,
) -> None:
    media = await repo.get_media(session, callback_data.media_id)
    # Единый ответ для «нет файла / удалён / нет доступа к категории» —
    # как в ask_delete: не раскрываем, существует ли файл
    if (
        media is None
        or media.is_deleted
        or not await access.can_view(session, user, config, media.category_id)
    ):
        await callback.answer("Этот файл уже удалён из Схрона", show_alert=True)
        return
    if not await access.can_delete_media(session, user, config, media.uploaded_by):
        await callback.answer(
            "Подпись может менять загрузивший или админ", show_alert=True
        )
        return
    message = callback.message
    if not isinstance(message, Message):
        # сообщение старше 48 часов: некуда отправить подсказку
        await callback.answer("Кнопка устарела 🕰 Найди файл заново", show_alert=True)
        return
    # Сначала отправка, потом состояние: если подсказка не ушла, юзер
    # не должен застрять в «невидимом» режиме редактирования
    try:
        await message.answer(
            f"Пришли текст подписи (до {CAPTION_MAX_LEN} символов). "
            "Отправь «-», чтобы убрать подпись",
            reply_markup=_caption_cancel_kb(media.id),
        )
    except TelegramAPIError:
        await callback.answer(
            "Не получилось начать редактирование — попробуй ещё раз",
            show_alert=True,
        )
        return
    # update_data, а не set_data: не теряем pending/dup_candidates
    await state.update_data(cap_media_id=media.id)
    await state.set_state(CaptionStates.waiting_caption)
    await callback.answer()


@router.callback_query(MediaActionCB.filter(F.action == "capx"))
async def cancel_caption(
    callback: CallbackQuery,
    callback_data: MediaActionCB,
    state: FSMContext,
) -> None:
    # Снимаем только СВОЁ состояние: протухшая кнопка «Отмена» не должна
    # убивать чужой FSM-диалог (админку, переход по номеру и т.п.)
    if await state.get_state() == CaptionStates.waiting_caption.state:
        await clear_state_keep_pending(state)
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text("Ок, не меняем 👌")
        except TelegramAPIError:
            pass
    await callback.answer()


@router.message(
    StateFilter(CaptionStates.waiting_caption), F.chat.type == "private", F.text
)
async def receive_caption(
    message: Message,
    session: AsyncSession,
    user: User,
    config: Config,
    state: FSMContext,
) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    media_id = data.get("cap_media_id")
    if media_id is None:
        # бота перезапускали: состояние осталось, а data — нет
        await clear_state_keep_pending(state)
        await message.answer("Кнопка устарела — найди мем и нажми ✏️ ещё раз")
        return
    if len(text) > CAPTION_MAX_LEN:
        # остаёмся в состоянии — пусть пришлёт покороче
        await message.answer(
            f"Длинновато 🙈 До {CAPTION_MAX_LEN} символов, сейчас {len(text)}"
        )
        return
    # Перепроверяем перед записью: файл могли удалить, права — отозвать
    media = await repo.get_media(session, media_id)
    if (
        media is None
        or media.is_deleted
        or not await access.can_view(session, user, config, media.category_id)
    ):
        await clear_state_keep_pending(state)
        await message.answer(
            "Этот файл уже удалён из Схрона — подпись не поменять"
        )
        return
    if not await access.can_delete_media(session, user, config, media.uploaded_by):
        await clear_state_keep_pending(state)
        await message.answer("Подпись может менять загрузивший или админ")
        return
    if text == "-":
        await repo.set_media_caption(session, media.id, None)
        await message.answer("Подпись убрана 🧹")
    else:
        await repo.set_media_caption(session, media.id, text)
        await message.answer(
            "✏️ Подпись обновлена — теперь мем можно найти через 🔍 Поиск"
        )
    await clear_state_keep_pending(state)


@router.message(StateFilter(CaptionStates.waiting_caption), F.chat.type == "private")
async def caption_not_text(message: Message, state: FSMContext) -> None:
    """Не-текст в режиме подписи (файл, стикер и т.п.): не гадаем, выходим.
    clear_state_keep_pending вернёт choosing_category, если есть pending."""
    await clear_state_keep_pending(state)
    await message.answer(
        "Ок, выхожу из редактирования подписи 👌 "
        "Пришли файл ещё раз, если хотел его сохранить"
    )
