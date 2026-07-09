"""Работа Схрона в группах: /random, /feed и /categories по правам самой группы.

Права выдаются админом на группу целиком (в его личной админке),
личные доступы участников здесь не участвуют — контент видят все.
Загрузка и меню в группах не работают.
"""

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import ChatMemberUpdatedFilter, Command
from aiogram.filters.chat_member_updated import (
    IS_MEMBER,
    IS_NOT_MEMBER,
    JOIN_TRANSITION,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.db import repo
from skhron.keyboards.callbacks import GroupFeedCB, GroupFeedPickCB, GroupRandomCB
from skhron.services import access
from skhron.utils.media import media_caption, send_media

router = Router(name="group")

GROUP_TYPES = {"group", "supergroup"}

GREETING_TEXT = (
    "Привет! Я Схрон 📦 — приватный архив мемов. "
    "Команды: /random — случайный мем, /feed — лента категории, "
    "/categories — что открыто этой группе. "
    "Какие категории доступны группе — решает мой админ."
)
NO_ACCESS_TEXT = (
    "Этой группе пока не открыли ни одной категории 🙈 "
    "Попроси админа Схрона — это делается в его админ-панели"
)


def _more_kb(category_id: int) -> InlineKeyboardMarkup:
    """Один ряд «🎲 Ещё» — без ⭐️/🗑/меню: в группе им не место."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎲 Ещё",
                    callback_data=GroupRandomCB(category_id=category_id).pack(),
                )
            ]
        ]
    )


def _feed_nav_kb(category_id: int, offset: int, total: int) -> InlineKeyboardMarkup:
    """[◀️] [позиция/всего] [▶️]; на краях стрелку не показываем,
    счётчик всегда есть — клик по нему открывает ввод номера (offset=-1)."""
    row: list[InlineKeyboardButton] = []
    if offset > 0:
        row.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=GroupFeedCB(
                    category_id=category_id, offset=offset - 1
                ).pack(),
            )
        )
    row.append(
        InlineKeyboardButton(
            text=f"{offset + 1}/{total}",
            callback_data=GroupFeedCB(category_id=category_id, offset=-1).pack(),
        )
    )
    if offset + 1 < total:
        row.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=GroupFeedCB(
                    category_id=category_id, offset=offset + 1
                ).pack(),
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _chat_jump_ctx(bot: Bot, fsm_storage: BaseStorage, chat_id: int) -> FSMContext:
    """FSM-контекст, скоупнутый на ЧАТ (user_id=chat_id), собранный вручную.

    Per-user контекст здесь не годится: анонимные админы и отправители
    «от имени канала» отвечают как GroupAnonymousBot / канал, и их реплай
    никогда не совпал бы с состоянием, записанным на реальный user id.
    Ожидающие переходы храним в data этого контекста словарём
    gjumps = {str(prompt_message_id): {"category_id": ..., "total": ...}} —
    ключ по id промпта позволяет и параллельные промпты в одном чате.
    """
    return FSMContext(
        storage=fsm_storage,
        key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=chat_id),
    )


async def _send_feed_item(
    session: AsyncSession,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    category_id: int,
    offset: int,
) -> str | None:
    """Шлёт пост ленты НОВЫМ сообщением (старые не удаляем — это группа).

    Возвращает текст ошибки или None при успехе.
    """
    media, total = await repo.get_feed_item(session, category_id, offset)
    if total == 0:
        return "В категории пусто 🕸"
    if media is None or offset >= total:
        # Лента сократилась (файлы удалили) — прыгаем на последний реальный
        offset = min(offset, total - 1)
        media, total = await repo.get_feed_item(session, category_id, offset)
        if media is None:
            return "В категории пусто 🕸"
    category = await repo.get_category(session, media.category_id)
    try:
        await send_media(
            bot,
            chat_id,
            media,
            caption=media_caption(media, category),
            reply_markup=_feed_nav_kb(category_id, offset, total),
            message_thread_id=thread_id,
        )
    except TelegramAPIError:
        return "Не получилось отправить мем 😕"
    return None


async def _show_feed_from_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    category_id: int,
    offset: int,
) -> None:
    """Общие проверки колбэков ленты + показ элемента."""
    message = callback.message
    if message is None:
        await callback.answer()
        return
    # .chat есть и у InaccessibleMessage — этого достаточно
    chat = message.chat
    if chat.type not in GROUP_TYPES:
        await callback.answer(
            "Эта кнопка работает только в группе", show_alert=True
        )
        return
    if not await access.group_can_view(session, chat.id, category_id):
        await callback.answer(
            "Эту категорию группе не открывали 🙈", show_alert=True
        )
        return
    # в форум-группе шлём в тот топик, где нажали кнопку; getattr-guard:
    # у InaccessibleMessage атрибутов нет, а без is_topic_message нельзя —
    # в не-форумных супергруппах message_thread_id означает reply-тред
    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    error = await _send_feed_item(
        session, bot, chat.id, thread_id, category_id, offset
    )
    if error is not None:
        await callback.answer(error, show_alert=True)
        return
    await callback.answer()


# ---------------------------------------------------------------- membership


@router.my_chat_member(
    ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION),
    F.chat.type.in_(GROUP_TYPES),
)
async def bot_joined_group(
    event: ChatMemberUpdated, session: AsyncSession, bot: Bot
) -> None:
    # срабатывает только при реальном входе в группу,
    # а не при смене прав/повышении в админы
    await repo.upsert_chat(
        session, event.chat.id, event.chat.title or "", event.chat.type
    )
    try:
        await bot.send_message(event.chat.id, GREETING_TEXT)
    except TelegramAPIError:
        pass


@router.my_chat_member(
    ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER),
    F.chat.type.in_(GROUP_TYPES),
)
async def bot_left_group(event: ChatMemberUpdated, session: AsyncSession) -> None:
    # бота уже нет в группе — молча помечаем чат неактивным
    await repo.set_chat_active(session, event.chat.id, False)


@router.message(F.migrate_to_chat_id)
async def group_migrated(message: Message, session: AsyncSession) -> None:
    # Telegram превратил группу в супергруппу: сервисное сообщение приходит
    # из старой группы — переносим запись чата и права на новый id
    await repo.migrate_chat(session, message.chat.id, message.migrate_to_chat_id)


# ---------------------------------------------------------------- commands


@router.message(Command("random"), F.chat.type.in_(GROUP_TYPES))
async def group_random(message: Message, session: AsyncSession) -> None:
    # бот мог попасть в группу до внедрения фичи — регистрируем лениво
    await repo.upsert_chat(
        session, message.chat.id, message.chat.title or "", message.chat.type
    )
    allowed = await access.group_viewable_categories(session, message.chat.id)
    if not allowed:
        await message.reply(NO_ACCESS_TEXT)
        return
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🎲 Из всех разрешённых",
            callback_data=GroupRandomCB(category_id=0).pack(),
        )
    )
    for category in allowed:
        builder.row(
            InlineKeyboardButton(
                text=category.title,
                callback_data=GroupRandomCB(category_id=category.id).pack(),
            )
        )
    # кнопки «⬅️ Меню» нет: это группа, меню тут не работает
    await message.reply("Что дёргаем? 🎲", reply_markup=builder.as_markup())


@router.message(Command("categories"), F.chat.type.in_(GROUP_TYPES))
async def group_categories(message: Message, session: AsyncSession) -> None:
    await repo.upsert_chat(
        session, message.chat.id, message.chat.title or "", message.chat.type
    )
    allowed = await access.group_viewable_categories(session, message.chat.id)
    if not allowed:
        await message.reply(NO_ACCESS_TEXT)
        return
    lines = ["📂 <b>Открыто этой группе:</b>", ""]
    for category in allowed:
        count = await repo.count_media(session, category.id)
        lines.append(f"📁 {html.escape(category.title)} — {count} шт.")
    await message.reply("\n".join(lines))


@router.message(Command("feed"), F.chat.type.in_(GROUP_TYPES))
async def group_feed(message: Message, session: AsyncSession, bot: Bot) -> None:
    # бот мог попасть в группу до внедрения фичи — регистрируем лениво
    await repo.upsert_chat(
        session, message.chat.id, message.chat.title or "", message.chat.type
    )
    allowed = await access.group_viewable_categories(session, message.chat.id)
    if not allowed:
        await message.reply(NO_ACCESS_TEXT)
        return
    if len(allowed) == 1:
        # единственная категория — сразу показываем её ленту
        thread_id = (
            message.message_thread_id
            if getattr(message, "is_topic_message", False)
            else None
        )
        error = await _send_feed_item(
            session, bot, message.chat.id, thread_id, allowed[0].id, 0
        )
        if error is not None:
            await message.reply(error)
        return
    builder = InlineKeyboardBuilder()
    for category in allowed:
        builder.row(
            InlineKeyboardButton(
                text=category.title,
                callback_data=GroupFeedPickCB(category_id=category.id).pack(),
            )
        )
    # кнопки «⬅️ Меню» нет: это группа, меню тут не работает
    await message.reply("Какую ленту листаем? 📼", reply_markup=builder.as_markup())


# ---------------------------------------------------------------- callbacks


@router.callback_query(GroupRandomCB.filter())
async def group_random_pick(
    callback: CallbackQuery,
    callback_data: GroupRandomCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    message = callback.message
    if message is None:
        await callback.answer()
        return
    # .chat есть и у InaccessibleMessage — этого достаточно, правок не делаем
    chat = message.chat
    if chat.type not in GROUP_TYPES:
        await callback.answer(
            "Эта кнопка работает только в группе", show_alert=True
        )
        return

    if callback_data.category_id == 0:
        ids = await access.group_viewable_category_ids(session, chat.id)
        if not ids:
            await callback.answer(
                "Группе пока ничего не открыто 🙈", show_alert=True
            )
            return
    else:
        if not await access.group_can_view(
            session, chat.id, callback_data.category_id
        ):
            await callback.answer(
                "Эту категорию группе не открывали 🙈", show_alert=True
            )
            return
        ids = [callback_data.category_id]

    media = await repo.get_random_media(session, ids)
    if media is None:
        await callback.answer("Тут пока пусто 🕸", show_alert=True)
        return

    category = await repo.get_category(session, media.category_id)
    # в форум-группе шлём мем в тот топик, где нажали кнопку;
    # getattr-guard: у InaccessibleMessage этих атрибутов нет,
    # а без is_topic_message нельзя — в не-форумных супергруппах
    # message_thread_id означает reply-тред, а не топик
    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    # старые сообщения не удаляем — пусть остаются в чате группы
    try:
        await send_media(
            bot,
            chat.id,
            media,
            caption=media_caption(media, category),
            reply_markup=_more_kb(callback_data.category_id),
            message_thread_id=thread_id,
        )
    except TelegramAPIError:
        await callback.answer("Не получилось отправить мем 😕", show_alert=True)
        return
    await callback.answer()


@router.callback_query(GroupFeedPickCB.filter())
async def group_feed_pick(
    callback: CallbackQuery,
    callback_data: GroupFeedPickCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    await _show_feed_from_callback(
        callback, session, bot, callback_data.category_id, 0
    )


@router.callback_query(GroupFeedCB.filter())
async def group_feed_page(
    callback: CallbackQuery,
    callback_data: GroupFeedCB,
    session: AsyncSession,
    bot: Bot,
    fsm_storage: BaseStorage,
) -> None:
    if callback_data.offset == -1:
        # клик по счётчику «N/M» — открываем ввод номера поста
        await _start_group_jump(
            callback, callback_data.category_id, session, bot, fsm_storage
        )
        return
    if callback_data.offset < 0:
        # защитная ветка: неизвестные отрицательные offset (например -2 —
        # старая кнопка «Отмена») — просто гасим спиннер, ничего не делаем
        await callback.answer()
        return
    await _show_feed_from_callback(
        callback, session, bot, callback_data.category_id, callback_data.offset
    )


# ---------------------------------------------------------------- jump by number


async def _start_group_jump(
    callback: CallbackQuery,
    category_id: int,
    session: AsyncSession,
    bot: Bot,
    fsm_storage: BaseStorage,
) -> None:
    message = callback.message
    if message is None:
        await callback.answer()
        return
    # .chat есть и у InaccessibleMessage — этого достаточно
    chat = message.chat
    if chat.type not in GROUP_TYPES:
        await callback.answer(
            "Эта кнопка работает только в группе", show_alert=True
        )
        return
    if not await access.group_can_view(session, chat.id, category_id):
        await callback.answer(
            "Эту категорию группе не открывали 🙈", show_alert=True
        )
        return
    _, total = await repo.get_feed_item(session, category_id, 0)
    if total == 0:
        await callback.answer("В категории пусто 🕸", show_alert=True)
        return
    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    # privacy mode: бот видит только реплаи на свои сообщения,
    # поэтому просим именно ОТВЕТ (ForceReply selective — для кликнувшего)
    user = callback.from_user
    mention = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    try:
        prompt = await bot.send_message(
            chat.id,
            f"{mention}, ответь на это сообщение номером поста (1–{total}) 🔢\n"
            "(или ответь «отмена»)",
            reply_markup=ForceReply(selective=True),
            message_thread_id=thread_id,
        )
    except TelegramAPIError:
        await callback.answer("Не получилось отправить вопрос 😕", show_alert=True)
        return
    ctx = _chat_jump_ctx(bot, fsm_storage, chat.id)
    data = await ctx.get_data()
    pending: dict = data.get("gjumps", {})
    pending[str(prompt.message_id)] = {"category_id": category_id, "total": total}
    if len(pending) > 20:
        # брошенные промпты не копим бесконечно: message_id растут монотонно,
        # так что выкидываем самые старые ключи
        for stale in sorted(pending, key=int)[: len(pending) - 20]:
            del pending[stale]
    await ctx.update_data(gjumps=pending)
    await callback.answer()


@router.message(
    F.chat.type.in_(GROUP_TYPES),
    F.reply_to_message,
    F.text,
)
async def group_jump_number(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    fsm_storage: BaseStorage,
) -> None:
    # privacy mode: до нас доходят только реплаи на сообщения самого бота,
    # так что этот хендлер видит в основном ответы на наши промпты
    ctx = _chat_jump_ctx(bot, fsm_storage, message.chat.id)
    data = await ctx.get_data()
    pending: dict = data.get("gjumps", {})
    key = str(message.reply_to_message.message_id)
    entry = pending.get(key)
    if entry is None:
        # реплай не на активный промпт (чужое сообщение бота) — строго молча:
        # отвечать нельзя, иначе съедим обычные реплаи в чате
        return

    async def _drop_pending() -> None:
        pending.pop(key, None)
        await ctx.update_data(gjumps=pending)

    text = (message.text or "").strip()
    if text.casefold() == "отмена":
        await _drop_pending()
        await message.reply("Ок 👌")
        return
    if not text.isdecimal():
        # промежуточная ошибка — промпт остаётся активным
        await message.reply("Просто номер, например 7")
        return

    category_id = entry.get("category_id", 0)
    if not await access.group_can_view(session, message.chat.id, category_id):
        # категорию успели закрыть — выходим из режима
        await _drop_pending()
        await message.reply("Эту категорию группе не открывали 🙈")
        return

    # диапазон проверяем ДО запроса с offset=num-1: «0» и гигантские числа
    # (OverflowError в SQLite) не должны дойти до OFFSET
    num = int(text)
    _, total = await repo.get_feed_item(session, category_id, 0)
    if total == 0:
        await _drop_pending()
        await message.reply("В категории пусто 🕸")
        return
    if num < 1 or num > total:
        # мимо диапазона — промпт остаётся активным, пусть попробует ещё раз
        await message.reply(f"В ленте всего {total} постов 🙃")
        return
    media, total = await repo.get_feed_item(session, category_id, num - 1)
    if media is None:
        # гонка: лента сократилась между двумя запросами
        await message.reply(f"В ленте всего {total} постов 🙃")
        return

    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    category = await repo.get_category(session, media.category_id)
    try:
        await send_media(
            bot,
            message.chat.id,
            media,
            caption=media_caption(media, category),
            reply_markup=_feed_nav_kb(category_id, num - 1, total),
            message_thread_id=thread_id,
        )
    except TelegramAPIError:
        await message.reply("Не получилось отправить мем 😕")
        return
    await _drop_pending()
