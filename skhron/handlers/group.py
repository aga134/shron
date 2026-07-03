"""Работа Схрона в группах: /random и /categories по правам самой группы.

Права выдаются админом на группу целиком (в его личной админке),
личные доступы участников здесь не участвуют — контент видят все.
Загрузка, лента и меню в группах не работают.
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
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.db import repo
from skhron.keyboards.callbacks import GroupRandomCB
from skhron.services import access
from skhron.utils.media import media_caption, send_media

router = Router(name="group")

GROUP_TYPES = {"group", "supergroup"}

GREETING_TEXT = (
    "Привет! Я Схрон 📦 — приватный архив мемов. "
    "Команды: /random — случайный мем, /categories — что открыто этой группе. "
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
