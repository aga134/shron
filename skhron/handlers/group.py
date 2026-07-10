"""Работа Схрона в группах: /random, /feed и /categories по правам самой
группы, /save реплаем — сохранение мема прямо из чата, ⭐️ под
медиа-постами бота — в личное избранное нажавшего и /top — топ мемов
группы по звёздочкам.

Права на просмотр выдаются админом на группу целиком (в его личной админке),
личные доступы участников здесь не участвуют — контент видят все.
Исключение — /save: сохраняет конкретный участник, поэтому нужна пара условий
«категория открыта группе» + «у автора команды есть личное право загрузки».
Меню в группах не работает.
"""

import asyncio
import html
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import Category, User
from skhron.keyboards.callbacks import (
    GroupFavCB,
    GroupFeedCB,
    GroupFeedPickCB,
    GroupRandomCB,
    GroupSaveCB,
    LegacyGroupLikeCB,
)
from skhron.services import access
from skhron.services.archive import archive_copy
from skhron.services.dedup import PHASH_MAX_DISTANCE, compute_phash_from_message
from skhron.utils.media import extract_media, media_caption, send_media

router = Router(name="group")

GROUP_TYPES = {"group", "supergroup"}

GREETING_TEXT = (
    "Привет! Я Схрон 📦 — приватный архив мемов. "
    "Команды: /random — случайный мем, /feed — лента категории, "
    "/categories — что открыто этой группе, "
    "/save (ответом на мем) — сохранить его в Схрон. "
    "⭐️ под постами — в личное избранное. "
    "Какие категории доступны группе — решает мой админ."
)
NO_ACCESS_TEXT = (
    "Этой группе пока не открыли ни одной категории 🙈 "
    "Попроси админа Схрона — это делается в его админ-панели"
)


def _fav_button(media_id: int, text: str = "⭐️") -> InlineKeyboardButton:
    """Кнопка «⭐️» — тоггл личного избранного нажавшего (без счётчиков)."""
    return InlineKeyboardButton(
        text=text,
        callback_data=GroupFavCB(media_id=media_id).pack(),
    )


def _more_kb(category_id: int, media_id: int) -> InlineKeyboardMarkup:
    """Один ряд «🎲 Ещё» + «⭐️» — без 🗑/меню: в группе им не место."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎲 Ещё",
                    callback_data=GroupRandomCB(category_id=category_id).pack(),
                ),
                _fav_button(media_id),
            ]
        ]
    )


def _feed_nav_kb(
    category_id: int, offset: int, total: int, media_id: int
) -> InlineKeyboardMarkup:
    """[◀️] [позиция/всего] [▶️] + отдельный ряд «⭐️ В избранное»; на краях
    стрелку не показываем, счётчик всегда есть — клик по нему открывает ввод
    номера (offset=-1)."""
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
    return InlineKeyboardMarkup(
        inline_keyboard=[row, [_fav_button(media_id, "⭐️ В избранное")]]
    )


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
            reply_markup=_feed_nav_kb(category_id, offset, total, media.id),
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


@router.message(Command("top"), F.chat.type.in_(GROUP_TYPES))
async def group_top(message: Message, session: AsyncSession, bot: Bot) -> None:
    """Топ-5 мемов группы по звёздочкам — из открытых ей категорий."""
    # кулдаун на группу: каждая выдача — до 6 сообщений, спам командой
    # быстро упирается во флуд-лимиты Telegram
    now = time.monotonic()
    last = _top_cooldowns.get(message.chat.id)
    if last is not None and now - last < _TOP_COOLDOWN_SECONDS:
        await message.reply("Топ уже показывал недавно 🙂 Попробуй через минуту")
        return
    # отметку ставим ДО отправки: параллельный второй /top отсечётся сразу
    _top_cooldowns[message.chat.id] = now

    # бот мог попасть в группу до внедрения фичи — регистрируем лениво
    await repo.upsert_chat(
        session, message.chat.id, message.chat.title or "", message.chat.type
    )
    ids = await access.group_viewable_category_ids(session, message.chat.id)
    if not ids:
        await message.reply(NO_ACCESS_TEXT)
        return
    top = await repo.top_favorited(session, ids, limit=5)
    if not top:
        await message.reply(
            "В этих категориях пока ни одной звёздочки — жми ⭐️ под мемами!"
        )
        return
    thread_id = (
        message.message_thread_id
        if getattr(message, "is_topic_message", False)
        else None
    )
    try:
        # честная формулировка: звёзды считаются от всех пользователей Схрона
        # по категориям группы, а не только от участников этого чата
        await message.reply("🏆 Самые звёздные мемы из категорий группы:")
    except TelegramAPIError:
        # чат уже под флуд-лимитом — слать пять медиа бессмысленно
        return
    for place, (media, count) in enumerate(top, start=1):
        if place > 1:
            # пауза между постами, чтобы не влететь во флуд-лимит
            await asyncio.sleep(0.3)
        category = await repo.get_category(session, media.category_id)
        caption = f"#{place} · ⭐️ {count}\n\n" + media_caption(media, category)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[_fav_button(media.id, "⭐️ В избранное")]]
        )
        try:
            await send_media(
                bot,
                message.chat.id,
                media,
                caption=caption,
                reply_markup=kb,
                message_thread_id=thread_id,
            )
        except TelegramRetryAfter as e:
            # флуд-лимит: ждём, сколько попросили, и пробуем ровно ещё раз
            await asyncio.sleep(e.retry_after)
            try:
                await send_media(
                    bot,
                    message.chat.id,
                    media,
                    caption=caption,
                    reply_markup=kb,
                    message_thread_id=thread_id,
                )
            except TelegramAPIError:
                continue
        except TelegramAPIError:
            # битый элемент пропускаем — остальной топ важнее
            continue


# ---------------------------------------------------------------- /save реплаем


async def _group_save(
    bot: Bot,
    session: AsyncSession,
    config: Config,
    user: User,
    category: Category,
    item: dict,
) -> str:
    """Сохранение мема из группы по item-словарю. Возвращает готовый
    HTML-safe текст-ответ (title уже экранирован).

    Порядок как в личном _save_one, но вместо вопроса «сохранить всё равно?»
    похожий файл просто НЕ сохраняем: в группе не раскрываем контент категории
    и не разводим диалоги. Точный дубль (по file_unique_id) проверяется ДО
    поиска похожих: тот же самый файл всегда идёт прежним путём add_media
    (честное «уже есть» + восстановление мягко удалённых).
    """
    # скаляры снимаем ДО add_media: rollback при гонке IntegrityError
    # протухает ORM-объекты, и доступ к их полям после него упадёт
    category_id = category.id
    raw_title = category.title
    title = html.escape(raw_title)
    user_id = user.id
    uploader_name = user.full_name

    exact = await repo.get_media_by_unique_id(
        session, category_id, item["file_unique_id"]
    )
    if exact is not None and not exact.is_deleted:
        return f"⚠️ Уже есть в «{title}»"
    if item.get("phash") and exact is None:
        similar = await repo.find_similar_media(
            session,
            category_id,
            item["phash"],
            PHASH_MAX_DISTANCE,
            exclude_file_unique_id=item["file_unique_id"],
        )
        if similar is not None:
            return (
                f"🤔 Очень похоже на то, что уже лежит в «{title}» — "
                "не стал сохранять. Если это всё же другой мем, "
                "сохрани его через личку бота"
            )
    media, created = await repo.add_media(
        session,
        category_id,
        item["file_id"],
        item["file_unique_id"],
        item["media_type"],
        item.get("caption"),
        user_id,
        phash=item.get("phash"),
    )
    if not created:
        return f"⚠️ Уже есть в «{title}»"
    if media.archive_message_id is None:
        archive_chat_id, archive_message_id = await archive_copy(
            bot,
            config,
            item["src_chat_id"],
            item["src_message_id"],
            item["media_type"],
            raw_title,
            uploader_name,
        )
        if archive_message_id is not None:
            media.archive_chat_id = archive_chat_id
            media.archive_message_id = archive_message_id
            await session.commit()
    return f"✅ Сохранил в «{title}» 📦"


@router.message(Command("save"), F.chat.type.in_(GROUP_TYPES))
async def group_save(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    config: Config,
    fsm_storage: BaseStorage,
    user: User | None = None,
) -> None:
    """Сохранение мема из группового чата без похода в личку:
    /save ответом на сообщение с медиа. Privacy mode не мешает — команды
    бот видит всегда, а reply_to_message приезжает внутри самой команды."""
    if message.reply_to_message is None:
        await message.reply("Ответь командой /save на сообщение с мемом 🙂")
        return
    # бот мог попасть в группу до внедрения фичи — регистрируем лениво
    await repo.upsert_chat(
        session, message.chat.id, message.chat.title or "", message.chat.type
    )
    extracted = extract_media(message.reply_to_message)
    if extracted is None:
        await message.reply(
            "В этом сообщении нет медиа, которое я умею хранить "
            "(фото/видео/гифка/кружок/войс/аудио)"
        )
        return
    if user is None:
        # анонимный админ или отправитель «от имени канала»: middleware
        # не кладёт user для ботов-масок, а без личности право загрузки
        # не проверить и загрузку не атрибутировать
        await message.reply(
            "Не вижу, кто сохраняет 🙈 Анонимному админу /save недоступен — "
            "сними анонимность или сохрани через личку бота"
        )
        return

    cats = [
        c
        for c in await access.group_viewable_categories(session, message.chat.id)
        if await access.can_upload(session, user, config, c.id)
    ]
    if not cats:
        await message.reply(
            "Сохранять могут те, у кого есть право загрузки в категории "
            "этой группы — попроси у админа Схрона 🙂"
        )
        return

    # phash считаем сразу: у GroupSaveCB-колбэка объекта Message с медиа
    # уже не будет, поэтому в реестр кладём готовые извлечённые поля
    reply = message.reply_to_message
    media_type, file_id, file_unique_id = extracted
    item = {
        "media_type": media_type,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "caption": reply.caption,
        "src_chat_id": reply.chat.id,
        "src_message_id": reply.message_id,
        "phash": await compute_phash_from_message(bot, reply),
    }

    if len(cats) == 1:
        try:
            result = await _group_save(bot, session, config, user, cats[0], item)
        except (TelegramAPIError, SQLAlchemyError):
            # rollback оставляет сессию рабочей для ответа-реплая
            await session.rollback()
            result = "Не получилось сохранить 😕 Попробуй ещё раз"
        try:
            await message.reply(result)
        except TelegramAPIError:
            pass
        return

    builder = InlineKeyboardBuilder()
    for category in cats:
        builder.row(
            InlineKeyboardButton(
                text=category.title,
                callback_data=GroupSaveCB(category_id=category.id).pack(),
            )
        )
    # вопрос — РЕПЛАЕМ на сообщение с мемом: так видно, о чём речь,
    # и не важно, удалят ли потом саму команду /save
    try:
        question = await reply.reply(
            "Куда сохранить? 📁", reply_markup=builder.as_markup()
        )
    except TelegramAPIError:
        try:
            await message.reply("Не получилось задать вопрос 😕")
        except TelegramAPIError:
            pass
        return
    # реестр ожидающих сохранений — в чат-скоупном контексте (как gjumps):
    # запись фиксируем только ПОСЛЕ успешной отправки вопроса
    ctx = _chat_jump_ctx(bot, fsm_storage, message.chat.id)
    data = await ctx.get_data()
    pending: dict = data.get("gsaves", {})
    pending[str(question.message_id)] = {
        "sender_id": user.id,
        "category_ids": [c.id for c in cats],
        "item": item,
    }
    if len(pending) > 20:
        # брошенные вопросы не копим бесконечно: message_id растут монотонно,
        # так что выкидываем самые старые ключи
        for stale in sorted(pending, key=int)[: len(pending) - 20]:
            del pending[stale]
    await ctx.update_data(gsaves=pending)


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
            reply_markup=_more_kb(callback_data.category_id, media.id),
            message_thread_id=thread_id,
        )
    except TelegramAPIError:
        await callback.answer("Не получилось отправить мем 😕", show_alert=True)
        return
    await callback.answer()


# Кулдаун /top per группа: monotonic-время последней выдачи
_TOP_COOLDOWN_SECONDS = 60
_top_cooldowns: dict[int, float] = {}


@router.callback_query(GroupFavCB.filter())
@router.callback_query(LegacyGroupLikeCB.filter())
async def group_fav(
    callback: CallbackQuery,
    callback_data: GroupFavCB | LegacyGroupLikeCB,
    session: AsyncSession,
    user: User | None = None,
) -> None:
    """Тоггл ⭐️ под групповым медиа-постом бота — личное избранное нажавшего.

    Обслуживает и старые кнопки «❤️ N» эпохи лайков (лайки смигрированы
    в избранное, так что нажатие на старом посте работает как ⭐️).

    Избранное привязано к записи media, а не к сообщению: одна и та же
    картинка добавляется юзером один раз, из какого бы поста её ни показали.
    Публичных счётчиков нет, разметку сообщения не трогаем — локи не нужны.
    """
    message = callback.message
    if message is None:
        await callback.answer()
        return
    # .chat есть и у InaccessibleMessage — этого достаточно
    chat = message.chat
    if chat.type not in GROUP_TYPES:
        await callback.answer("Кнопка работает только в группе", show_alert=True)
        return
    if user is None:
        # анонимный админ или отправитель «от имени канала»: middleware
        # не кладёт user для ботов-масок, а избранное без личности не привязать
        await callback.answer(
            "Не вижу, кто нажал 🙈 Анонимному админу избранное недоступно",
            show_alert=True,
        )
        return
    media = await repo.get_media(session, callback_data.media_id)
    if media is None or media.is_deleted:
        await callback.answer("Этого мема уже нет в Схроне", show_alert=True)
        return
    # категория должна быть открыта ИМЕННО этой группе — защита от
    # пересланных сообщений с кнопкой и поддельных callback_data
    if not await access.group_can_view(session, chat.id, media.category_id):
        await callback.answer("Этот мем не из этой группы 🙈", show_alert=True)
        return

    added = await repo.toggle_favorite(session, user.id, media.id)
    await callback.answer(
        "⭐️ В избранном! Смотри в личке бота" if added else "Убрано из избранного"
    )


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


@router.callback_query(GroupSaveCB.filter())
async def group_save_pick(
    callback: CallbackQuery,
    callback_data: GroupSaveCB,
    session: AsyncSession,
    bot: Bot,
    config: Config,
    user: User,
    fsm_storage: BaseStorage,
) -> None:
    """Выбор категории для /save. Объекта сообщения с медиа тут уже нет —
    работаем по извлечённым полям из реестра gsaves."""
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

    ctx = _chat_jump_ctx(bot, fsm_storage, chat.id)
    data = await ctx.get_data()
    pending: dict = data.get("gsaves", {})
    key = str(message.message_id)
    entry = pending.get(key)
    if entry is None:
        # бота перезапускали или запись вытеснило капом
        if isinstance(message, Message):
            try:
                await message.edit_text("⌛️ Кнопка устарела")
            except TelegramAPIError:
                pass
        await callback.answer("Кнопка устарела 🕰", show_alert=True)
        return
    if callback.from_user.id != entry.get("sender_id"):
        await callback.answer(
            "Эта кнопка для того, кто сохранял 🙂", show_alert=True
        )
        return

    # честные проверки: кнопка могла пережить смену прав/категорий
    if callback_data.category_id not in entry.get("category_ids", []):
        await callback.answer(
            "Эта категория тут не предлагалась 🤔", show_alert=True
        )
        return
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория куда-то делась 🤔", show_alert=True)
        return
    if not await access.group_can_view(session, chat.id, category.id):
        await callback.answer(
            "Эту категорию группе не открывали 🙈", show_alert=True
        )
        return
    if not await access.can_upload(session, user, config, category.id):
        await callback.answer(
            "Сюда загружать нельзя — попроси доступ у админа 🔒", show_alert=True
        )
        return

    try:
        result = await _group_save(
            bot, session, config, user, category, entry["item"]
        )
    except (TelegramAPIError, SQLAlchemyError):
        # rollback оставляет сессию рабочей; запись НЕ удаляем —
        # пусть попробует нажать ещё раз
        await session.rollback()
        await callback.answer(
            "Не получилось сохранить 😕 Попробуй ещё раз", show_alert=True
        )
        return

    pending.pop(key, None)
    await ctx.update_data(gsaves=pending)
    # вопрос — всегда текстовое сообщение, edit_text безопасен
    if isinstance(message, Message):
        try:
            await message.edit_text(result)
        except TelegramAPIError:
            pass
    # тост — plain text (HTML-entities убираем); лимит answerCallbackQuery —
    # 200 UTF-16 code units (астральные эмодзи считаются за два)
    toast = html.unescape(result)
    if len(toast.encode("utf-16-le")) // 2 > 200:
        toast = toast[:99] + "…"
    await callback.answer(toast)


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
            reply_markup=_feed_nav_kb(category_id, num - 1, total, media.id),
            message_thread_id=thread_id,
        )
    except TelegramAPIError:
        await message.reply("Не получилось отправить мем 😕")
        return
    await _drop_pending()
