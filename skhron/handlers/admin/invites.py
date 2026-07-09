"""Админка «Схрона»: инвайты — конструктор, список, карточка, деактивация.

Родительский роутер уже отфильтровал события AdminFilter'ом.
"""

import html

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
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.db import repo
from skhron.db.models import User
from skhron.keyboards.callbacks import AdminCB, InviteCB
from skhron.utils.fsm import clear_state_keep_pending

router = Router(name="admin_invites")


class InviteStates(StatesGroup):
    drafting = State()


# ---------------------------------------------------------------- helpers


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


def _uses_label(max_uses: int) -> str:
    return "∞" if max_uses == 0 else str(max_uses)


def _rights_label(can_upload: bool) -> str:
    return "👁+📤 смотреть и загружать" if can_upload else "👁 только смотреть"


async def _invite_link(bot: Bot, code: str) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=inv_{code}"


# ---------------------------------------------------------------- renderers


async def _render_invites_list(
    session: AsyncSession,
) -> tuple[str, InlineKeyboardMarkup]:
    invites = await repo.list_invites(session)
    if invites:
        text = (
            f"🎟 <b>Инвайты</b> (активных: {len(invites)})\n\n"
            "Жми на инвайт, чтобы посмотреть детали:"
        )
    else:
        text = "🎟 <b>Инвайты</b>\n\nАктивных инвайтов нет — самое время создать!"
    builder = InlineKeyboardBuilder()
    for invite in invites:
        builder.row(
            InlineKeyboardButton(
                text=(
                    f"🎟 {invite.code} · "
                    f"{invite.used_count}/{_uses_label(invite.max_uses)}"
                ),
                callback_data=InviteCB(action="open", invite_id=invite.id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="➕ Новый инвайт", callback_data=InviteCB(action="new").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(section="home").pack()
        )
    )
    return text, builder.as_markup()


async def _render_draft(
    session: AsyncSession, data: dict
) -> tuple[str, InlineKeyboardMarkup]:
    cat_ids: list[int] = data.get("cat_ids", [])
    can_upload: bool = data.get("can_upload", False)
    max_uses: int = data.get("max_uses", 1)

    categories = await repo.list_categories(session)
    text = (
        "🎟 <b>Собери инвайт</b>\n\n"
        "Отметь категории, настрой права и лимит — и жми «Создать»!"
    )
    if not categories:
        text += "\n\n⚠️ Активных категорий нет — сначала создай хотя бы одну 📁"

    builder = InlineKeyboardBuilder()
    for category in categories:
        mark = "✅" if category.id in cat_ids else "⬜️"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {category.title}",
                callback_data=InviteCB(action="cat", value=category.id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=f"Права: {_rights_label(can_upload)}",
            callback_data=InviteCB(action="rights").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"Лимит: {_uses_label(max_uses)}",
            callback_data=InviteCB(action="uses").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🎟 Создать", callback_data=InviteCB(action="create").pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="↩️ Отмена", callback_data=InviteCB(action="list").pack()
        )
    )
    return text, builder.as_markup()


# ---------------------------------------------------------------- handlers


@router.callback_query(AdminCB.filter(F.section == "invites"))
async def open_invites_section(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    await clear_state_keep_pending(state)
    text, markup = await _render_invites_list(session)
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(InviteCB.filter(F.action == "list"))
async def invites_list(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    # кнопка «↩️ Отмена» черновика тоже ведёт сюда — сбрасываем диалог,
    # не теряя данные живых кнопок (pending/dup_candidates)
    await clear_state_keep_pending(state)
    text, markup = await _render_invites_list(session)
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(InviteCB.filter(F.action == "new"))
async def new_invite(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    await state.set_state(InviteStates.drafting)
    # set_data затирает все данные — переносим ключи, на которые ещё
    # смотрят живые кнопки: «💾 Сохранить всё равно» (dup_candidates)
    # и «Куда сохранить?» (pending)
    old_data = await state.get_data()
    draft: dict = {"cat_ids": [], "can_upload": False, "max_uses": 1}
    for key in ("pending", "dup_candidates"):
        if old_data.get(key):
            draft[key] = old_data[key]
    await state.set_data(draft)
    text, markup = await _render_draft(session, await state.get_data())
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(
    InviteCB.filter(F.action == "cat"), StateFilter(InviteStates.drafting)
)
async def draft_toggle_category(
    callback: CallbackQuery,
    callback_data: InviteCB,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    cat_ids: list[int] = list(data.get("cat_ids", []))
    if callback_data.value in cat_ids:
        cat_ids.remove(callback_data.value)
    else:
        cat_ids.append(callback_data.value)
    await state.update_data(cat_ids=cat_ids)
    text, markup = await _render_draft(session, await state.get_data())
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(
    InviteCB.filter(F.action == "rights"), StateFilter(InviteStates.drafting)
)
async def draft_toggle_rights(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    await state.update_data(can_upload=not data.get("can_upload", False))
    text, markup = await _render_draft(session, await state.get_data())
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(
    InviteCB.filter(F.action == "uses"), StateFilter(InviteStates.drafting)
)
async def draft_cycle_uses(
    callback: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    current = data.get("max_uses", 1)
    # цикл: 1 → 5 → ∞(0) → 1
    next_value = {1: 5, 5: 0, 0: 1}.get(current, 1)
    await state.update_data(max_uses=next_value)
    text, markup = await _render_draft(session, await state.get_data())
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(
    InviteCB.filter(F.action == "create"), StateFilter(InviteStates.drafting)
)
async def draft_create(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    bot: Bot,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    cat_ids: list[int] = data.get("cat_ids", [])
    if not cat_ids:
        await callback.answer("Выбери хотя бы одну категорию", show_alert=True)
        return
    can_upload: bool = data.get("can_upload", False)
    max_uses: int = data.get("max_uses", 1)

    invite = await repo.create_invite(session, cat_ids, can_upload, max_uses, user.id)
    if invite is None:
        # все выбранные категории успели удалить параллельно —
        # черновик остаётся, конструктор перерисуем со свежим списком
        await callback.answer(
            "Категории уже удалили — собери инвайт заново 🤷", show_alert=True
        )
        text, markup = await _render_draft(session, await state.get_data())
        await _show(callback, bot, text, markup)
        return
    await clear_state_keep_pending(state)

    link = await _invite_link(bot, invite.code)
    titles = []
    for category_id in cat_ids:
        category = await repo.get_category(session, category_id)
        if category is not None:
            titles.append(html.escape(category.title))
    text = (
        "🎉 <b>Инвайт готов!</b>\n\n"
        "Кидай другу ссылку:\n"
        f"<code>{link}</code>\n\n"
        f"📁 Открывает: {', '.join(titles) if titles else '—'}\n"
        f"Права: {_rights_label(can_upload)}\n"
        f"Лимит использований: {_uses_label(max_uses)}"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ К инвайтам",
                    callback_data=InviteCB(action="list").pack(),
                )
            ]
        ]
    )
    await _show(callback, bot, text, markup)
    await callback.answer("🎟 Инвайт создан!")


@router.callback_query(
    InviteCB.filter(F.action.in_({"cat", "rights", "uses", "create"}))
)
async def draft_lost(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, state: FSMContext
) -> None:
    """Кнопки конструктора нажаты вне состояния drafting (например, после
    перезапуска бота) — черновика больше нет, возвращаем к списку."""
    if await state.get_state() is not None:
        # Чужой активный FSM-диалог (создание категории и т.п.) — не трогаем
        await callback.answer("Сначала заверши текущее действие 🙂", show_alert=True)
        return
    # состояние уже None — чистить нечего, просто возвращаем к списку
    await callback.answer("Черновик потерялся 😅 Собери инвайт заново", show_alert=True)
    text, markup = await _render_invites_list(session)
    await _show(callback, bot, text, markup)


@router.callback_query(InviteCB.filter(F.action == "open"))
async def invite_card(
    callback: CallbackQuery,
    callback_data: InviteCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    invite = await repo.get_invite(session, callback_data.invite_id)
    if invite is None:
        await callback.answer("Такого инвайта уже нет 🤷", show_alert=True)
        text, markup = await _render_invites_list(session)
        await _show(callback, bot, text, markup)
        return

    link = await _invite_link(bot, invite.code)
    titles = []
    for category_id in repo.invite_category_ids(invite):
        category = await repo.get_category(session, category_id)
        if category is None:
            continue
        title = html.escape(category.title)
        if category.is_archived:
            title += " (в архиве)"
        titles.append(title)

    text = (
        f"🎟 <b>Инвайт</b> <code>{html.escape(invite.code)}</code>\n\n"
        f"<code>{link}</code>\n\n"
        f"📁 Категории: {', '.join(titles) if titles else '—'}\n"
        f"Права: {_rights_label(invite.can_upload)}\n"
        f"Использовано: {invite.used_count}/{_uses_label(invite.max_uses)}\n"
        f"Статус: {'✅ активен' if invite.is_active else '⛔️ деактивирован'}"
    )
    builder = InlineKeyboardBuilder()
    if invite.is_active:
        builder.row(
            InlineKeyboardButton(
                text="❌ Деактивировать",
                callback_data=InviteCB(action="off", invite_id=invite.id).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=InviteCB(action="list").pack()
        )
    )
    await _show(callback, bot, text, builder.as_markup())
    await callback.answer()


@router.callback_query(InviteCB.filter(F.action == "off"))
async def invite_deactivate(
    callback: CallbackQuery,
    callback_data: InviteCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    await repo.deactivate_invite(session, callback_data.invite_id)
    await callback.answer("Инвайт деактивирован")
    text, markup = await _render_invites_list(session)
    await _show(callback, bot, text, markup)
