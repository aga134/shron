"""Избранное: листаем сохранённые мемы из доступных категорий."""

from aiogram import Bot, F, Router
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
from skhron.db.models import Media, User
from skhron.keyboards.callbacks import FavPageCB, MenuCB
from skhron.keyboards.common import back_to_menu_kb, media_kb
from skhron.services import access
from skhron.utils.fsm import clear_state_keep_pending
from skhron.utils.media import media_caption, send_media

router = Router(name="favorites")

EMPTY_TEXT = "В избранном пусто. Жми ⭐️ под любым мемом!"
JUMP_PROMPT = "Напиши номер поста (1–{total}) — перейду к нему 🔢"
JUMP_CANCELLED_TEXT = "Ок, не переходим 👌"
NOT_A_NUMBER_TEXT = "Нужен просто номер, например 12"
OUT_OF_RANGE_TEXT = "В ленте всего {total} постов, а ты просишь {num} 🙃"


class FavJumpStates(StatesGroup):
    waiting_number = State()


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
    """[◀️] [позиция/всего] [▶️]; на краях стрелку не показываем (как в группе),
    счётчик всегда есть — клик по нему открывает ввод номера (offset=-1)."""
    row: list[InlineKeyboardButton] = []
    if offset > 0:
        row.append(
            InlineKeyboardButton(
                text="◀️", callback_data=FavPageCB(offset=offset - 1).pack()
            )
        )
    row.append(
        InlineKeyboardButton(
            text=f"{offset + 1}/{total}",
            callback_data=FavPageCB(offset=-1).pack(),
        )
    )
    if offset + 1 < total:
        row.append(
            InlineKeyboardButton(
                text="▶️", callback_data=FavPageCB(offset=offset + 1).pack()
            )
        )
    return row


async def _send_favorite_media(
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
    chat_id: int,
    media: Media,
    offset: int,
    total: int,
) -> bool:
    """Пост избранного уходит НОВЫМ сообщением; старые не удаляем — как в рандоме.

    Возвращает False, если отправить не получилось (как в group.py).
    """
    category = await repo.get_category(session, media.category_id)
    deletable = await access.can_delete_media(
        session, user, config, media.uploaded_by
    )
    try:
        await send_media(
            bot,
            chat_id,
            media,
            caption=media_caption(media, category),
            reply_markup=media_kb(
                media.id,
                deletable=deletable,
                extra_rows=[_nav_row(offset, total)],
            ),
        )
    except TelegramAPIError:
        return False
    return True


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

    sent = await _send_favorite_media(
        session, user, config, bot, _chat_id(callback), media, offset, total
    )
    if not sent:
        await callback.answer("Не получилось отправить мем 😕", show_alert=True)
        return
    await callback.answer()


async def _start_jump(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Клик по счётчику «N/M» — включаем режим «жду номер поста»."""
    category_ids = await access.viewable_category_ids(session, user, config)
    _, total = await repo.get_favorite_item(session, user.id, category_ids, 0)
    if total == 0:
        await callback.answer("В избранном пусто 🕸", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data=FavPageCB(offset=-2).pack(),
                )
            ]
        ]
    )
    # Сначала шлём вопрос и только при успехе ставим состояние (как в group.py):
    # иначе после неудачной отправки юзер застрянет в режиме ввода
    # без видимого промпта и кнопки «Отмена»
    try:
        await bot.send_message(
            _chat_id(callback), JUMP_PROMPT.format(total=total), reply_markup=kb
        )
    except TelegramAPIError:
        await callback.answer("Не получилось отправить вопрос 😕", show_alert=True)
        return
    # update_data (не set_data) — не затираем dup_candidates и данные других
    # потоков; состояние collecting перезаписываем осознанно: юзер переключился
    # на листание, его загрузка потом завершится как «протухшая»
    await state.update_data(favjump_total=total)
    await state.set_state(FavJumpStates.waiting_number)
    await callback.answer()


async def _cancel_jump(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «↩️ Отмена» (offset=-2): выходим из режима ввода номера.
    Состояние снимаем только если это действительно наш режим — протухшая
    кнопка не должна ломать чужой FSM-диалог."""
    if await state.get_state() == FavJumpStates.waiting_number.state:
        await clear_state_keep_pending(state)
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(JUMP_CANCELLED_TEXT)
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
    state: FSMContext,
) -> None:
    """Спец-коды offset: -1 — клик по счётчику «N/M»: открыть ввод номера
    поста; -2 — «↩️ Отмена» под приглашением ввода номера: выйти из режима
    ввода. Остальное — обычная навигация."""
    if callback_data.offset == -1:
        await _start_jump(callback, session, user, config, bot, state)
        return
    if callback_data.offset == -2:
        await _cancel_jump(callback, state)
        return
    # Обычная навигация выводит из режима ввода номера: иначе состояние
    # висело бы дальше, и следующий присланный файл ушёл бы в abort-хендлер
    if await state.get_state() == FavJumpStates.waiting_number.state:
        await clear_state_keep_pending(state)
    await _show_favorite_item(
        callback, session, user, config, bot, callback_data.offset
    )


@router.message(
    StateFilter(FavJumpStates.waiting_number), F.chat.type == "private", F.text
)
async def favorites_jump_number(
    message: Message,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
    state: FSMContext,
) -> None:
    """Юзер прислал номер поста — показываем этот пост новым сообщением."""
    text = (message.text or "").strip()
    if not text.isdecimal():
        # isdecimal, а не isdigit: тот пропускает «²»/«①», на которых int()
        # падает. Остаёмся в состоянии — пусть попробует ещё раз
        await message.answer(NOT_A_NUMBER_TEXT)
        return
    num = int(text)

    # Свежий total: избранное (и доступы) могли измениться после клика
    # по счётчику. Проверяем диапазон ДО запроса с offset=num-1 — заодно
    # отсекаем отрицательные и гигантские числа
    category_ids = await access.viewable_category_ids(session, user, config)
    _, total = await repo.get_favorite_item(session, user.id, category_ids, 0)
    if total == 0:
        # Избранное опустело (или отозвали последний доступ — get_favorite_item
        # вернёт (None, 0)) — выходим, а не зацикливаем «всего 0 постов»
        await clear_state_keep_pending(state)
        await message.answer("В избранном пусто 🕸")
        return
    if num < 1 or num > total:
        await message.answer(OUT_OF_RANGE_TEXT.format(total=total, num=num))
        return
    media, total = await repo.get_favorite_item(
        session, user.id, category_ids, num - 1
    )
    if media is None:
        # гонка: избранное сократилось между двумя запросами
        await message.answer(OUT_OF_RANGE_TEXT.format(total=total, num=num))
        return

    await clear_state_keep_pending(state)
    sent = await _send_favorite_media(
        session, user, config, bot, message.chat.id, media, num - 1, total
    )
    if not sent:
        await message.answer("Не получилось отправить мем 😕")


@router.message(StateFilter(FavJumpStates.waiting_number), F.chat.type == "private")
async def favorites_jump_media_abort(message: Message, state: FSMContext) -> None:
    """Не-текст в режиме ввода номера (мем, стикер, войс): выходим из режима.
    Иначе файл молча пропадёт — upload-хендлеры фильтруют по другим состояниям,
    а случайный клик по счётчику превращается в тупик."""
    await clear_state_keep_pending(state)
    await message.answer(
        "Ок, выхожу из перехода по номеру 👌 "
        "Пришли файл ещё раз — предложу, куда сохранить 📤"
    )
