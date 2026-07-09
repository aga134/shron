"""Админка категорий: список, создание, карточка, переименование,
архив, удаление, просмотр и выдача доступов."""

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.db import repo
from skhron.db.models import Category, User
from skhron.handlers.admin.panel import show_text_screen
from skhron.keyboards.callbacks import AdminCB, CatAdminCB, UserAdminCB
from skhron.utils.dates import fmt_date
from skhron.utils.fsm import clear_state_keep_pending

router = Router(name="admin_categories")


class CatStates(StatesGroup):
    waiting_title = State()
    waiting_rename = State()
    waiting_user = State()


# ---------------------------------------------------------------- рендеры


def _user_label(user: User) -> str:
    return user.full_name or (f"@{user.username}" if user.username else str(user.id))


async def _render_list(
    session: AsyncSession,
) -> tuple[str, InlineKeyboardMarkup]:
    categories = await repo.list_categories(session, include_archived=True)
    active = sum(1 for c in categories if not c.is_archived)
    archived = len(categories) - active
    if categories:
        text = (
            "📁 <b>Категории Схрона</b>\n\n"
            f"Всего: <b>{len(categories)}</b> "
            f"(активных: {active}, в архиве 📦: {archived})"
        )
    else:
        text = (
            "📁 <b>Категории Схрона</b>\n\n"
            "Пока пусто. Создай первую категорию — и понеслась! 🚀"
        )
    rows: list[list[InlineKeyboardButton]] = []
    for category in categories:
        title = ("📦 " if category.is_archived else "") + category.title
        rows.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=CatAdminCB(
                        action="open", category_id=category.id
                    ).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Создать категорию",
                callback_data=CatAdminCB(action="new").pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ В админку", callback_data=AdminCB(section="home").pack()
            )
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_card(
    session: AsyncSession, category: Category
) -> tuple[str, InlineKeyboardMarkup]:
    files_count = await repo.count_media(session, category.id)
    # считаем только реальные доступы: записи с выключенным просмотром
    # (can_view=False) категорию не открывают
    users_count = sum(
        1
        for perm, _member in await repo.list_category_users(session, category.id)
        if perm.can_view
    )
    lines = [
        f"📁 <b>{html.escape(category.title)}</b>",
        "",
        f"Статус: {'📦 в архиве' if category.is_archived else '✅ активна'}",
        f"Файлов: {files_count}",
        f"Доступов: {users_count}",
        f"Создана: {fmt_date(category.created_at)}",
    ]
    if category.is_archived:
        lines += [
            "",
            "<i>Архивная категория скрыта у всех, файлы целы — "
            "можно вернуть в любой момент.</i>",
        ]
    text = "\n".join(lines)

    arch_text = "♻️ Вернуть из архива" if category.is_archived else "🗄 В архив"
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Переименовать",
                callback_data=CatAdminCB(action="ren", category_id=category.id).pack(),
            ),
            InlineKeyboardButton(
                text=arch_text,
                callback_data=CatAdminCB(
                    action="arch", category_id=category.id
                ).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=CatAdminCB(action="del", category_id=category.id).pack(),
            )
        ],
        [
            InlineKeyboardButton(
                text="👥 Доступы",
                callback_data=CatAdminCB(
                    action="users", category_id=category.id
                ).pack(),
            ),
            InlineKeyboardButton(
                text="➕ Выдать доступ",
                callback_data=CatAdminCB(
                    action="adduser", category_id=category.id
                ).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ К списку", callback_data=CatAdminCB(action="list").pack()
            )
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_kb(text: str, cb: CatAdminCB) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=cb.pack())]]
    )


# ---------------------------------------------------------------- список


@router.callback_query(AdminCB.filter(F.section == "cats"))
@router.callback_query(CatAdminCB.filter(F.action == "list"))
async def show_categories(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    # Сюда ведут кнопки-отмены — сбрасываем диалог, сохраняя данные
    # живых кнопок (pending/dup_candidates из загрузки)
    await clear_state_keep_pending(state)
    text, kb = await _render_list(session)
    await show_text_screen(callback, text, kb)
    await callback.answer()


# ---------------------------------------------------------------- создание


@router.callback_query(CatAdminCB.filter(F.action == "new"))
async def ask_new_title(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CatStates.waiting_title)
    await show_text_screen(
        callback,
        "✨ Название новой категории? (до 128 символов)",
        _cancel_kb("↩️ Отмена", CatAdminCB(action="list")),
    )
    await callback.answer()


@router.message(CatStates.waiting_title, F.chat.type == "private")
async def create_category(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        await message.answer(
            "Хм, нужен текст от 1 до 128 символов. Попробуй ещё раз 🙂",
            reply_markup=_cancel_kb("↩️ Отмена", CatAdminCB(action="list")),
        )
        return
    category = await repo.create_category(session, title, user.id)
    if category is None:
        await message.answer(
            "Такая уже есть, придумай другое 🙃",
            reply_markup=_cancel_kb("↩️ Отмена", CatAdminCB(action="list")),
        )
        return
    await clear_state_keep_pending(state)
    text, kb = await _render_card(session, category)
    await message.answer(text, reply_markup=kb)


# ---------------------------------------------------------------- карточка


@router.callback_query(CatAdminCB.filter(F.action == "open"))
async def open_category(
    callback: CallbackQuery,
    callback_data: CatAdminCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    # Сюда тоже ведут кнопки-отмены (ren/adduser) — сбрасываем диалог,
    # сохраняя данные живых кнопок (pending/dup_candidates)
    await clear_state_keep_pending(state)
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer(
            "Категория не найдена — возможно, её уже удалили", show_alert=True
        )
        return
    text, kb = await _render_card(session, category)
    await show_text_screen(callback, text, kb)
    await callback.answer()


# ---------------------------------------------------------------- переименование


@router.callback_query(CatAdminCB.filter(F.action == "ren"))
async def ask_rename(
    callback: CallbackQuery,
    callback_data: CatAdminCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await state.set_state(CatStates.waiting_rename)
    await state.update_data(category_id=category.id)
    await show_text_screen(
        callback,
        f"✏️ Новое название для «{html.escape(category.title)}»? (до 128 символов)",
        _cancel_kb("↩️ Отмена", CatAdminCB(action="open", category_id=category.id)),
    )
    await callback.answer()


@router.message(CatStates.waiting_rename, F.chat.type == "private")
async def do_rename(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    category = await repo.get_category(session, data.get("category_id", 0))
    if category is None:
        await clear_state_keep_pending(state)
        await message.answer(
            "Упс, категория куда-то делась 🙈 Начни заново из админки: /admin"
        )
        return
    cancel = _cancel_kb(
        "↩️ Отмена", CatAdminCB(action="open", category_id=category.id)
    )
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        await message.answer(
            "Нужен текст от 1 до 128 символов. Попробуй ещё раз 🙂",
            reply_markup=cancel,
        )
        return
    if not await repo.rename_category(session, category.id, title):
        await message.answer("Название занято, другое? 🤔", reply_markup=cancel)
        return
    await clear_state_keep_pending(state)
    text, kb = await _render_card(session, category)
    await message.answer(text, reply_markup=kb)


# ---------------------------------------------------------------- архив


@router.callback_query(CatAdminCB.filter(F.action == "arch"))
async def toggle_archive(
    callback: CallbackQuery, callback_data: CatAdminCB, session: AsyncSession
) -> None:
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    archived = not category.is_archived
    await repo.set_category_archived(session, category.id, archived)
    text, kb = await _render_card(session, category)
    await show_text_screen(callback, text, kb)
    await callback.answer("📦 В архиве" if archived else "Снова активна")


# ---------------------------------------------------------------- удаление


@router.callback_query(CatAdminCB.filter(F.action == "del"))
async def ask_delete(
    callback: CallbackQuery, callback_data: CatAdminCB, session: AsyncSession
) -> None:
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    files_count = await repo.count_media(session, category.id)
    text = (
        f"Удалить «{html.escape(category.title)}»? 😱\n\n"
        f"Из базы уйдут записи о <b>{files_count}</b> файлах и все доступы. "
        "Сами файлы останутся в Telegram (и в канале-архиве)."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Да, удалить",
                    callback_data=CatAdminCB(
                        action="delc", category_id=category.id
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data=CatAdminCB(
                        action="open", category_id=category.id
                    ).pack(),
                ),
            ]
        ]
    )
    await show_text_screen(callback, text, kb)
    await callback.answer()


@router.callback_query(CatAdminCB.filter(F.action == "delc"))
async def confirm_delete(
    callback: CallbackQuery, callback_data: CatAdminCB, session: AsyncSession
) -> None:
    await repo.delete_category(session, callback_data.category_id)
    text, kb = await _render_list(session)
    await show_text_screen(callback, text, kb)
    await callback.answer("Категория удалена")


# ---------------------------------------------------------------- доступы


@router.callback_query(CatAdminCB.filter(F.action == "users"))
async def category_users(
    callback: CallbackQuery, callback_data: CatAdminCB, session: AsyncSession
) -> None:
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    pairs = await repo.list_category_users(session, category.id)
    lines = [f"👥 <b>Доступы к «{html.escape(category.title)}»</b>", ""]
    if pairs:
        for perm, member in pairs:
            icons = ("👁" if perm.can_view else "🚫") + (
                "📤" if perm.can_upload else ""
            )
            line = f"{icons} {html.escape(member.full_name or '—')}"
            if member.username:
                line += f" (@{html.escape(member.username)})"
            lines.append(line)
        lines += [
            "",
            "<i>👁 — просмотр, 📤 — ещё и загрузка, 🚫 — просмотр выключен</i>",
        ]
    else:
        lines.append("Пока никому не выдан доступ. Админы видят её и так 😎")
    rows: list[list[InlineKeyboardButton]] = []
    for _perm, member in pairs:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_user_label(member),
                    callback_data=UserAdminCB(
                        action="open", user_id=member.id
                    ).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Выдать доступ",
                callback_data=CatAdminCB(
                    action="adduser", category_id=category.id
                ).pack(),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К категории",
                callback_data=CatAdminCB(
                    action="open", category_id=category.id
                ).pack(),
            )
        ]
    )
    await show_text_screen(
        callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await callback.answer()


@router.callback_query(CatAdminCB.filter(F.action == "adduser"))
async def ask_user(
    callback: CallbackQuery,
    callback_data: CatAdminCB,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await state.set_state(CatStates.waiting_user)
    await state.update_data(category_id=category.id)
    text = (
        f"➕ Кому открыть «{html.escape(category.title)}»?\n\n"
        "Пришли числовой ID, @username или перешли сообщение от человека. "
        "Если он ещё не запускал бота — проще создать инвайт 🎟"
    )
    await show_text_screen(
        callback,
        text,
        _cancel_kb("↩️ Отмена", CatAdminCB(action="open", category_id=category.id)),
    )
    await callback.answer()


@router.message(CatStates.waiting_user, F.chat.type == "private")
async def grant_user(
    message: Message, state: FSMContext, session: AsyncSession, user: User
) -> None:
    data = await state.get_data()
    category = await repo.get_category(session, data.get("category_id", 0))
    if category is None:
        await clear_state_keep_pending(state)
        await message.answer(
            "Упс, категория куда-то делась 🙈 Начни заново из админки: /admin"
        )
        return
    cancel = _cancel_kb(
        "↩️ Отмена", CatAdminCB(action="open", category_id=category.id)
    )

    target: User | None = None
    if message.forward_origin is not None:
        # Пересланное сообщение: forward_from доступен только если человек
        # не скрыл аккаунт в настройках приватности
        if isinstance(message.forward_origin, MessageOriginUser):
            target = await repo.get_user(
                session, message.forward_origin.sender_user.id
            )
        elif isinstance(message.forward_origin, MessageOriginHiddenUser):
            await message.answer(
                "У этого человека в настройках приватности скрыт аккаунт при "
                "пересылке 🕵️ Попроси его прислать свой ID (например, через "
                "@userinfobot) или пришли мне его @username.",
                reply_markup=cancel,
            )
            return
        else:
            await message.answer(
                "Это переслано из канала или чата, а мне нужен человек 🙂 "
                "Пришли ID, @username или пересланное сообщение от него.",
                reply_markup=cancel,
            )
            return
    else:
        text = (message.text or "").strip()
        if text.startswith("@") and len(text) > 1:
            target = await repo.get_user_by_username(session, text)
        elif text.isdigit():
            target = await repo.get_user(session, int(text))
        else:
            await message.answer(
                "Не понял 🤔 Пришли числовой ID, @username или перешли "
                "сообщение от человека.",
                reply_markup=cancel,
            )
            return

    if target is None:
        await message.answer(
            "Этот человек ещё не запускал бота — сделай ему инвайт 🎟",
            reply_markup=cancel,
        )
        return

    granted = await repo.set_permission(
        session, target.id, category.id, can_view=True, granted_by=user.id
    )
    if granted is None:
        # кросс-админская гонка: категорию/юзера успели удалить параллельно
        await message.answer(
            "Права уже изменили параллельно 🤝 Попробуй ещё раз.",
            reply_markup=cancel,
        )
        return
    await clear_state_keep_pending(state)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Разрешить и загрузку",
                    callback_data=UserAdminCB(
                        action="pupload",
                        user_id=target.id,
                        category_id=category.id,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К категории",
                    callback_data=CatAdminCB(
                        action="open", category_id=category.id
                    ).pack(),
                )
            ],
        ]
    )
    await message.answer(
        f"✅ {html.escape(_user_label(target))} теперь видит "
        f"«{html.escape(category.title)}»",
        reply_markup=kb,
    )
