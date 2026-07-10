"""Общие клавиатуры, используемые несколькими модулями."""

from collections.abc import Callable

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from skhron.db.models import Category
from skhron.keyboards.callbacks import MediaActionCB, MenuCB


def menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="⬅️ Меню", callback_data=MenuCB(action="home").pack()
    )


def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[menu_button()]])


# Включён ли у бота инлайн-режим (/setinline у BotFather); ставится на старте
# из main.py. Кнопку «🔍 Поиск» без инлайна показывать нельзя: Telegram
# отклоняет ВСЮ клавиатуру ошибкой BUTTON_TYPE_INVALID, и меню не доходит.
INLINE_ENABLED = True


def main_menu_kb(is_admin: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎲 Рандом", callback_data=MenuCB(action="random"))
    builder.button(text="📼 Лента", callback_data=MenuCB(action="feed"))
    builder.button(text="⭐️ Избранное", callback_data=MenuCB(action="favorites"))
    builder.button(text="❤️ Лайкнутое", callback_data=MenuCB(action="liked"))
    builder.button(text="📤 Загрузить", callback_data=MenuCB(action="upload"))
    if INLINE_ENABLED:
        # открывает инлайн-режим прямо в этом чате: сетка превью + поиск
        builder.button(text="🔍 Поиск", switch_inline_query_current_chat="")
    builder.button(text="🔐 Мои доступы", callback_data=MenuCB(action="access"))
    builder.button(text="ℹ️ Помощь", callback_data=MenuCB(action="help"))
    if is_admin:
        builder.button(text="🛠 Админка", callback_data=MenuCB(action="admin"))
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def categories_pick_kb(
    categories: list[Category],
    make_cb: Callable[[Category], CallbackData],
    header_buttons: list[InlineKeyboardButton] | None = None,
    back_button: InlineKeyboardButton | None = None,
) -> InlineKeyboardMarkup:
    """Список категорий столбиком; make_cb строит callback для категории."""
    builder = InlineKeyboardBuilder()
    if header_buttons:
        builder.row(*header_buttons)
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=category.title, callback_data=make_cb(category).pack()
            )
        )
    builder.row(back_button or menu_button())
    return builder.as_markup()


def media_kb(
    media_id: int,
    deletable: bool = False,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """Стандартные кнопки под медиа: [доп. ряды] + [⭐️ (✏️ 🗑)] + [⬅️ Меню].

    ✏️ и 🗑 показываются вместе: право менять подпись то же, что и удалять
    (загрузивший или админ).
    """
    actions = [
        InlineKeyboardButton(
            text="⭐️",
            callback_data=MediaActionCB(action="fav", media_id=media_id).pack(),
        )
    ]
    if deletable:
        actions.append(
            InlineKeyboardButton(
                text="✏️",
                callback_data=MediaActionCB(action="cap", media_id=media_id).pack(),
            )
        )
        actions.append(
            InlineKeyboardButton(
                text="🗑",
                callback_data=MediaActionCB(action="del", media_id=media_id).pack(),
            )
        )
    rows = list(extra_rows or [])
    rows.append(actions)
    rows.append([menu_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_kb(media_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Точно удалить",
                    callback_data=MediaActionCB(
                        action="delc", media_id=media_id
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data=MediaActionCB(
                        action="delx", media_id=media_id
                    ).pack(),
                ),
            ]
        ]
    )
