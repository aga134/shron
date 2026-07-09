"""Главный экран админки: /admin, вход из меню и возврат по AdminCB(home)."""

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from skhron.keyboards.callbacks import AdminCB, MenuCB
from skhron.utils.fsm import clear_state_keep_pending

router = Router(name="admin_panel")

ADMIN_HOME_TEXT = (
    "🛠 <b>Админка Схрона</b>\n\n"
    "Ты за пультом. Категории, люди, инвайты — всё здесь. Что делаем?"
)


def admin_home_kb() -> InlineKeyboardMarkup:
    """Клавиатура главного экрана админки (используется и другими модулями)."""
    rows = [
        [
            InlineKeyboardButton(
                text="📁 Категории", callback_data=AdminCB(section="cats").pack()
            ),
            InlineKeyboardButton(
                text="👥 Пользователи", callback_data=AdminCB(section="users").pack()
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎟 Инвайты", callback_data=AdminCB(section="invites").pack()
            ),
            InlineKeyboardButton(
                text="💬 Группы", callback_data=AdminCB(section="groups").pack()
            ),
        ],
        [
            InlineKeyboardButton(
                text="📊 Статистика", callback_data=AdminCB(section="stats").pack()
            ),
            InlineKeyboardButton(
                text="💾 Бэкап БД", callback_data=AdminCB(section="backup").pack()
            ),
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Меню", callback_data=MenuCB(action="home").pack()
            ),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_text_screen(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    """Показывает текстовый экран поверх сообщения callback'а.

    Сообщение с медиа нельзя edit_text: в этом случае (и в любом другом
    сбое правки) удаляем старое сообщение и шлём новое.
    """
    message = callback.message
    if not isinstance(message, Message):
        # Сообщение недоступно (inaccessible) — просто шлём новое в тот же чат
        if message is not None and callback.bot is not None:
            await callback.bot.send_message(
                message.chat.id, text, reply_markup=reply_markup
            )
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramAPIError as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        await message.answer(text, reply_markup=reply_markup)


@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await clear_state_keep_pending(state)
    await message.answer(ADMIN_HOME_TEXT, reply_markup=admin_home_kb())


@router.callback_query(MenuCB.filter(F.action == "admin"))
async def admin_from_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await clear_state_keep_pending(state)
    await show_text_screen(callback, ADMIN_HOME_TEXT, admin_home_kb())
    await callback.answer()


@router.callback_query(AdminCB.filter(F.section == "home"))
async def admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    # Кнопки «назад/отмена» ведут сюда — чистим возможное FSM-состояние,
    # не теряя данные живых кнопок (pending/dup_candidates)
    await clear_state_keep_pending(state)
    await show_text_screen(callback, ADMIN_HOME_TEXT, admin_home_kb())
    await callback.answer()
