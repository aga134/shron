"""Админка «Схрона»: пользователи — список, карточка, права, админка.

Родительский роутер уже отфильтровал события AdminFilter'ом.
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

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import Category, Permission, User
from skhron.keyboards.callbacks import AdminCB, UserAdminCB
from skhron.utils.commands import set_admin_scope
from skhron.utils.dates import fmt_date

router = Router(name="admin_users")

PAGE_SIZE = 10


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


def _user_label(target: User) -> str:
    name = target.full_name or "Без имени"
    if target.username:
        return f"{name} (@{target.username})"
    return name


def _perm_flags(perm: Permission) -> str:
    bits = []
    if perm.can_view:
        bits.append("👁")
    if perm.can_upload:
        bits.append("📤")
    return "+".join(bits) if bits else "🚫"


# ---------------------------------------------------------------- renderers


async def _render_users_list(
    session: AsyncSession, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    users, total = await repo.list_users(
        session, offset=page * PAGE_SIZE, limit=PAGE_SIZE
    )
    text = f"👥 <b>Пользователи</b> (всего {total})\n\nЖми на человека, чтобы открыть карточку:"
    builder = InlineKeyboardBuilder()
    for u in users:
        builder.row(
            InlineKeyboardButton(
                text=_user_label(u),
                callback_data=UserAdminCB(
                    action="open", user_id=u.id, page=page
                ).pack(),
            )
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="◀️",
                callback_data=UserAdminCB(action="list", page=page - 1).pack(),
            )
        )
    if (page + 1) * PAGE_SIZE < total:
        nav.append(
            InlineKeyboardButton(
                text="▶️",
                callback_data=UserAdminCB(action="list", page=page + 1).pack(),
            )
        )
    if nav:
        builder.row(*nav)
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад", callback_data=AdminCB(section="home").pack()
        )
    )
    return text, builder.as_markup()


async def _render_user_card(
    session: AsyncSession, config: Config, target: User, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    perms = await repo.list_user_permissions(session, target.id)

    lines = [f"👤 <b>{html.escape(target.full_name or 'Без имени')}</b>"]
    if target.username:
        lines.append(f"@{html.escape(target.username)}")
    lines.append(f"ID: <code>{target.id}</code>")
    if target.id in config.admin_ids:
        lines.append("⭐️ админ (из конфига)")
    elif target.is_admin:
        lines.append("⭐️ админ")
    lines.append(f"🗓 В Схроне с {fmt_date(target.created_at)}")
    lines.append("")
    if perms:
        lines.append("🔐 Доступы:")
        for perm, category in perms:
            lines.append(f"📁 {html.escape(category.title)}: {_perm_flags(perm)}")
    else:
        lines.append("🔐 Доступов пока нет")

    builder = InlineKeyboardBuilder()
    for _perm, category in perms:
        builder.row(
            InlineKeyboardButton(
                text=f"⚙️ {category.title}",
                callback_data=UserAdminCB(
                    action="perm",
                    user_id=target.id,
                    category_id=category.id,
                    page=page,
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="➕ Выдать доступ",
            callback_data=UserAdminCB(
                action="grant", user_id=target.id, page=page
            ).pack(),
        )
    )
    if target.id not in config.admin_ids:
        builder.row(
            InlineKeyboardButton(
                text="Снять админку" if target.is_admin else "⭐️ Сделать админом",
                callback_data=UserAdminCB(
                    action="tadmin", user_id=target.id, page=page
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=UserAdminCB(action="list", page=page).pack(),
        )
    )
    return "\n".join(lines), builder.as_markup()


async def _render_perm_card(
    session: AsyncSession, target: User, category: Category, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    perm = await repo.get_permission(session, target.id, category.id)
    can_view = perm.can_view if perm is not None else False
    can_upload = perm.can_upload if perm is not None else False

    text = (
        "⚙️ <b>Право доступа</b>\n\n"
        f"👤 {html.escape(target.full_name or 'Без имени')} "
        f"в «{html.escape(category.title)}»\n\n"
        f"👁 Просмотр: {'✅' if can_view else '❌'}\n"
        f"📤 Загрузка: {'✅' if can_upload else '❌'}"
    )
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"👁 Просмотр: {'✅' if can_view else '❌'}",
            callback_data=UserAdminCB(
                action="pview",
                user_id=target.id,
                category_id=category.id,
                page=page,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"📤 Загрузка: {'✅' if can_upload else '❌'}",
            callback_data=UserAdminCB(
                action="pupload",
                user_id=target.id,
                category_id=category.id,
                page=page,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Отозвать доступ",
            callback_data=UserAdminCB(
                action="revoke",
                user_id=target.id,
                category_id=category.id,
                page=page,
            ).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=UserAdminCB(
                action="open", user_id=target.id, page=page
            ).pack(),
        )
    )
    return text, builder.as_markup()


# ---------------------------------------------------------------- handlers


@router.callback_query(AdminCB.filter(F.section == "users"))
async def open_users_section(
    callback: CallbackQuery, session: AsyncSession, bot: Bot
) -> None:
    text, markup = await _render_users_list(session, page=0)
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(UserAdminCB.filter(F.action == "list"))
async def users_list(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    page = max(callback_data.page, 0)
    text, markup = await _render_users_list(session, page=page)
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(UserAdminCB.filter(F.action == "open"))
async def user_card(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    config: Config,
    bot: Bot,
) -> None:
    target = await repo.get_user(session, callback_data.user_id)
    if target is None:
        await callback.answer("Хм, такого юзера уже нет 🤷", show_alert=True)
        text, markup = await _render_users_list(session, page=callback_data.page)
        await _show(callback, bot, text, markup)
        return
    text, markup = await _render_user_card(
        session, config, target, page=callback_data.page
    )
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(UserAdminCB.filter(F.action == "tadmin"))
async def toggle_admin(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    if callback_data.user_id == user.id:
        await callback.answer("Себя понизить нельзя 😏", show_alert=True)
        return
    target = await repo.get_user(session, callback_data.user_id)
    if target is None:
        await callback.answer("Хм, такого юзера уже нет 🤷", show_alert=True)
        return
    if target.id in config.admin_ids:
        await callback.answer(
            "Этот админ прописан в конфиге, его не тронуть", show_alert=True
        )
        return
    new_value = not target.is_admin
    await repo.set_admin(session, target.id, new_value)
    # сразу включаем/выключаем подсказки /admin в личке юзера
    # (ошибки Telegram set_admin_scope глотает сам)
    await set_admin_scope(bot, target.id, new_value)
    text, markup = await _render_user_card(
        session, config, target, page=callback_data.page
    )
    await _show(callback, bot, text, markup)
    await callback.answer(
        "⭐️ Теперь админ!" if new_value else "Админка снята"
    )


@router.callback_query(UserAdminCB.filter(F.action == "perm"))
async def perm_card(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    target = await repo.get_user(session, callback_data.user_id)
    category = await repo.get_category(session, callback_data.category_id)
    if target is None or category is None:
        await callback.answer(
            "Юзер или категория уже не существуют 🤷", show_alert=True
        )
        return
    text, markup = await _render_perm_card(
        session, target, category, page=callback_data.page
    )
    await _show(callback, bot, text, markup)
    await callback.answer()


@router.callback_query(UserAdminCB.filter(F.action.in_({"pview", "pupload"})))
async def toggle_permission_flag(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    target = await repo.get_user(session, callback_data.user_id)
    category = await repo.get_category(session, callback_data.category_id)
    if target is None or category is None:
        await callback.answer(
            "Юзер или категория уже не существуют 🤷", show_alert=True
        )
        return

    perm = await repo.get_permission(session, target.id, category.id)
    granted_by = user.id if perm is None else None
    if callback_data.action == "pview":
        new_value = not (perm.can_view if perm is not None else False)
        updated = await repo.set_permission(
            session, target.id, category.id, can_view=new_value, granted_by=granted_by
        )
    else:
        new_value = not (perm.can_upload if perm is not None else False)
        updated = await repo.set_permission(
            session,
            target.id,
            category.id,
            can_upload=new_value,
            granted_by=granted_by,
        )
    if updated is None:
        # кросс-админская гонка: второй админ успел отозвать доступ или
        # удалить категорию/юзера. Rollback внутри set_permission протухил
        # ORM-объекты — перечитываем их перед рендером
        target = await repo.get_user(session, callback_data.user_id)
        category = await repo.get_category(session, callback_data.category_id)
        if target is None or category is None:
            await callback.answer(
                "Юзер или категория уже не существуют 🤷", show_alert=True
            )
            return
        await callback.answer("Права уже изменили параллельно 🤝", show_alert=True)
        text, markup = await _render_perm_card(
            session, target, category, page=callback_data.page
        )
        await _show(callback, bot, text, markup)
        return

    # Карточка рисуется «с нуля»: сюда можно попасть и из categories.py,
    # где текущее сообщение — совсем другой экран.
    text, markup = await _render_perm_card(
        session, target, category, page=callback_data.page
    )
    await _show(callback, bot, text, markup)
    await callback.answer("Готово ✅" if new_value else "Выключено")


@router.callback_query(UserAdminCB.filter(F.action == "revoke"))
async def revoke_permission(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    config: Config,
    bot: Bot,
) -> None:
    await repo.revoke_permission(
        session, callback_data.user_id, callback_data.category_id
    )
    await callback.answer("Доступ отозван")
    target = await repo.get_user(session, callback_data.user_id)
    if target is None:
        text, markup = await _render_users_list(session, page=callback_data.page)
    else:
        text, markup = await _render_user_card(
            session, config, target, page=callback_data.page
        )
    await _show(callback, bot, text, markup)


@router.callback_query(UserAdminCB.filter(F.action == "grant"))
async def grant_pick_category(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    bot: Bot,
) -> None:
    target = await repo.get_user(session, callback_data.user_id)
    if target is None:
        await callback.answer("Хм, такого юзера уже нет 🤷", show_alert=True)
        return
    categories = await repo.list_categories(session)
    if categories:
        text = (
            "➕ <b>Выдать доступ</b>\n\n"
            f"Куда пустить {html.escape(target.full_name or 'юзера')}? "
            "Выбирай категорию:"
        )
    else:
        text = (
            "➕ <b>Выдать доступ</b>\n\n"
            "Активных категорий пока нет — сначала создай хотя бы одну 📁"
        )
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=category.title,
                callback_data=UserAdminCB(
                    action="pgrant",
                    user_id=target.id,
                    category_id=category.id,
                    page=callback_data.page,
                ).pack(),
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=UserAdminCB(
                action="open", user_id=target.id, page=callback_data.page
            ).pack(),
        )
    )
    await _show(callback, bot, text, builder.as_markup())
    await callback.answer()


@router.callback_query(UserAdminCB.filter(F.action == "pgrant"))
async def grant_permission(
    callback: CallbackQuery,
    callback_data: UserAdminCB,
    session: AsyncSession,
    user: User,
    config: Config,
    bot: Bot,
) -> None:
    target = await repo.get_user(session, callback_data.user_id)
    category = await repo.get_category(session, callback_data.category_id)
    if target is None or category is None:
        await callback.answer(
            "Юзер или категория уже не существуют 🤷", show_alert=True
        )
        return
    granted = await repo.set_permission(
        session, target.id, category.id, can_view=True, granted_by=user.id
    )
    if granted is None:
        # кросс-админская гонка: категорию/юзера успели удалить параллельно.
        # Rollback внутри set_permission протухил объекты — перечитываем
        target = await repo.get_user(session, callback_data.user_id)
        if target is None:
            await callback.answer(
                "Юзер или категория уже не существуют 🤷", show_alert=True
            )
            return
        await callback.answer("Права уже изменили параллельно 🤝", show_alert=True)
        text, markup = await _render_user_card(
            session, config, target, page=callback_data.page
        )
        await _show(callback, bot, text, markup)
        return
    text, markup = await _render_perm_card(
        session, target, category, page=callback_data.page
    )
    await _show(callback, bot, text, markup)
    await callback.answer("Доступ выдан 🎉")
