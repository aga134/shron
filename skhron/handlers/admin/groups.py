"""Админка «Схрона»: группы — список, карточка, разрешённые категории.

В группе контент видят все участники сразу, поэтому категории открываются
самой группе, а не отдельным людям. Родительский роутер уже отфильтровал
события AdminFilter'ом.
"""

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.db import repo
from skhron.db.models import Chat, User
from skhron.keyboards.callbacks import AdminCB, ChatAdminCB, DailyCB

router = Router(name="admin_groups")

# Пресеты «мема дня»: минуты от полуночи в DISPLAY_TZ
_DAILY_PRESETS = (540, 720, 1080, 1260)


# ---------------------------------------------------------------- helpers


def _fmt_minutes(minutes: int) -> str:
    """540 -> «09:00»."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def _show(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    markup: InlineKeyboardMarkup,
) -> None:
    """Пытается отредактировать текущее сообщение; если нельзя (например,
    это сообщение с медиа) — удаляет его и шлёт новое."""
    message = callback.message
    if message is None:
        return
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramAPIError as e:
            if "message is not modified" in str(e):
                return
        try:
            await message.delete()
        except TelegramAPIError:
            pass
    await bot.send_message(message.chat.id, text, reply_markup=markup)


def _chat_label(chat: Chat) -> str:
    label = chat.title or f"Группа {chat.id}"
    if not chat.is_active:
        label = f"🚪 {label}"
    return label


# ---------------------------------------------------------------- renderers


async def _render_groups_list(
    session: AsyncSession,
) -> tuple[str, InlineKeyboardMarkup]:
    chats = await repo.list_chats(session)
    builder = InlineKeyboardBuilder()
    if chats:
        text = (
            "💬 <b>Группы</b>\n\n"
            "Жми на группу, чтобы настроить, какие категории ей открыты:"
        )
        for chat in chats:
            builder.row(
                InlineKeyboardButton(
                    text=_chat_label(chat),
                    callback_data=ChatAdminCB(
                        action="open", chat_id=chat.id
                    ).pack(),
                )
            )
    else:
        text = (
            "💬 <b>Группы</b>\n\n"
            "Групп пока нет. Просто добавь бота в группу с друзьями — "
            "она появится здесь 👌"
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(section="home").pack()
        )
    )
    return text, builder.as_markup()


async def _render_group_card(
    session: AsyncSession, chat: Chat
) -> tuple[str, InlineKeyboardMarkup]:
    categories = await repo.list_categories(session)
    # Открытые группе категории одним запросом; для активных категорий это
    # эквивалентно проверке repo.get_group_permission по каждой
    allowed_ids = {
        category.id
        for category in await repo.list_chat_categories(session, chat.id)
    }

    status = "🤖 бот в группе" if chat.is_active else "🚪 бота удалили из группы"
    if chat.daily_minutes is None:
        daily_line = "🌅 Мем дня: выключен"
    else:
        daily_line = (
            f"🌅 Мем дня: {_fmt_minutes(chat.daily_minutes)} (по серверному поясу)"
        )
    lines = [
        f"💬 <b>{html.escape(chat.title or 'Без названия')}</b>",
        f"ID: <code>{chat.id}</code>",
        status,
        f"📂 Открыто категорий: {len(allowed_ids)}",
        daily_line,
    ]
    if chat.daily_minutes is not None and not allowed_ids:
        lines.append("⚠️ группе не открыто ни одной категории — постить нечего")
    if categories:
        lines.append("")
        lines.append("Жми на категорию, чтобы открыть или закрыть её группе:")
    else:
        lines.append("")
        lines.append("Активных категорий пока нет — сначала создай хотя бы одну 📁")

    builder = InlineKeyboardBuilder()
    for category in categories:
        mark = "✅" if category.id in allowed_ids else "⬜️"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {category.title}",
                callback_data=ChatAdminCB(
                    action="toggle", chat_id=chat.id, category_id=category.id
                ).pack(),
            )
        )
    daily_row = []
    for minutes in _DAILY_PRESETS:
        label = _fmt_minutes(minutes)
        if chat.daily_minutes == minutes:
            btn_text = f"✅ {label}"
        elif minutes == _DAILY_PRESETS[0]:
            btn_text = f"🌅 {label}"
        else:
            btn_text = label
        daily_row.append(
            InlineKeyboardButton(
                text=btn_text,
                callback_data=DailyCB(chat_id=chat.id, minutes=minutes).pack(),
            )
        )
    daily_row.append(
        InlineKeyboardButton(
            text="❌ Выкл",
            callback_data=DailyCB(chat_id=chat.id, minutes=-1).pack(),
        )
    )
    builder.row(*daily_row)
    if not chat.is_active:
        builder.row(
            InlineKeyboardButton(
                text="🗑 Забыть группу",
                callback_data=ChatAdminCB(action="forget", chat_id=chat.id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=ChatAdminCB(action="list").pack()
        )
    )
    return "\n".join(lines), builder.as_markup()


async def _show_missing_chat(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    """Группу уже забыли, а кнопка протухла — алерт и обратно к списку."""
    await callback.answer("Этой группы уже нет в списке", show_alert=True)
    text, markup = await _render_groups_list(session)
    await _show(callback, bot, text, markup)


# ---------------------------------------------------------------- handlers


@router.callback_query(AdminCB.filter(F.section == "groups"))
@router.callback_query(ChatAdminCB.filter(F.action == "list"))
async def groups_list(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    text, markup = await _render_groups_list(session)
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(ChatAdminCB.filter(F.action == "open"))
async def group_card(
    callback: CallbackQuery,
    callback_data: ChatAdminCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    chat = await repo.get_chat(session, callback_data.chat_id)
    if chat is None:
        await _show_missing_chat(callback, session, bot)
        return
    text, markup = await _render_group_card(session, chat)
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(ChatAdminCB.filter(F.action == "toggle"))
async def toggle_group_category(
    callback: CallbackQuery,
    callback_data: ChatAdminCB,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    chat = await repo.get_chat(session, callback_data.chat_id)
    if chat is None:
        await _show_missing_chat(callback, session, bot)
        return

    # Категорию могли удалить или заархивировать, пока карточка висела на
    # экране: не создаём «скрытое» право и не показываем ложный успех.
    category = await repo.get_category(session, callback_data.category_id)
    if category is None or category.is_archived:
        await callback.answer("Этой категории уже нет 🤷", show_alert=True)
        text, markup = await _render_group_card(session, chat)
        await _show(callback, bot, text, markup)
        return

    perm = await repo.get_group_permission(
        session, chat.id, callback_data.category_id
    )
    if perm is not None:
        await repo.revoke_group_permission(
            session, chat.id, callback_data.category_id
        )
        await callback.answer("Закрыто для группы")
    else:
        granted = await repo.set_group_permission(
            session, chat.id, callback_data.category_id, granted_by=user.id
        )
        if granted is None:
            # Категорию (или сам чат) успели жёстко удалить — вставка
            # упала на FK и была откачена внутри repo.
            await callback.answer("Этой категории уже нет 🤷", show_alert=True)
        else:
            await callback.answer("Открыто группе ✅")

    text, markup = await _render_group_card(session, chat)
    await _show(callback, bot, text, markup)


@router.callback_query(DailyCB.filter())
async def set_group_daily(
    callback: CallbackQuery,
    callback_data: DailyCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """Пресеты «мема дня» из карточки группы: время или выключить."""
    chat = await repo.get_chat(session, callback_data.chat_id)
    if chat is None:
        await _show_missing_chat(callback, session, bot)
        return
    new_minutes = None if callback_data.minutes == -1 else callback_data.minutes
    if chat.daily_minutes == new_minutes:
        # повторный тап по активному пресету — ничего не меняем
        await callback.answer("Уже установлено 👌")
        return
    await repo.set_chat_daily(session, chat.id, new_minutes)
    if new_minutes is None:
        await callback.answer("Мем дня выключен")
    else:
        await callback.answer(f"Мем дня в {_fmt_minutes(new_minutes)} ✅")
    text, markup = await _render_group_card(session, chat)
    await _show(callback, bot, text, markup)


@router.callback_query(ChatAdminCB.filter(F.action == "forget"))
async def forget_group(
    callback: CallbackQuery,
    callback_data: ChatAdminCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    chat = await repo.get_chat(session, callback_data.chat_id)
    if chat is None:
        await _show_missing_chat(callback, session, bot)
        return
    if chat.is_active:
        # Кнопка протухла: бота уже вернули в группу, удалять её вместе
        # со всеми правами по старой карточке нельзя.
        await callback.answer(
            "Бот снова в этой группе — забыть можно только покинутую",
            show_alert=True,
        )
        text, markup = await _render_group_card(session, chat)
        await _show(callback, bot, text, markup)
        return
    await repo.delete_chat(session, chat.id)
    await callback.answer("Группа забыта")
    text, markup = await _render_groups_list(session)
    await _show(callback, bot, text, markup)
