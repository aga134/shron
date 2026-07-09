"""/start (в т.ч. активация инвайтов по deep-link) и справка."""

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.db import repo
from skhron.db.models import User
from skhron.keyboards.callbacks import MenuCB
from skhron.keyboards.common import back_to_menu_kb, main_menu_kb
from skhron.utils.fsm import clear_state_keep_pending

router = Router(name="start")


def _greeting_text(user: User) -> str:
    name = html.escape(user.full_name or user.username or "дружище")
    return (
        f"Привет, {name}! 👋\n\n"
        "Это «Схрон» — приватный архив мемов и видосов для своих. "
        "Всё, что жалко потерять в чатах, лежит тут: сохраняем, листаем, "
        "кидаем друг другу.\n\n"
        "Выбирай, что делаем 👇"
    )


def _help_text(is_admin: bool, bot_username: str | None) -> str:
    inline_hint = f"@{bot_username}" if bot_username else "@имя_бота"
    lines = [
        "ℹ️ <b>Что умеет «Схрон»</b>",
        "",
        "💬 <b>Личка:</b>",
        "📤 Просто пришли фото, видео, гифку, кружок, войс или аудио — предложу "
        "выбрать категорию и сохраню (альбомы тоже можно)",
        "/menu — главное меню",
        "/help — эта справка",
        "/start — перезапуск",
        "🎲 Рандом, 📼 лента, ⭐️ избранное и 🔐 доступы — кнопками в меню",
        "",
        "👥 <b>В группе</b> (добавь меня в чат с друзьями):",
        "/random — случайный мем из открытых группе категорий",
        "/feed — лента категории",
        "/categories — что открыто группе",
        "Категории группе открывает админ в своей админке",
        "",
        "🔎 <b>В любом чате:</b>",
        f"Инлайн: набери {inline_hint} &lt;запрос&gt; — и вставишь мем "
        "прямо в разговор",
    ]
    if is_admin:
        lines += [
            "",
            "🛠 <b>Для админа</b> (видно только админам):",
            "/admin — админ-панель",
            "/rehash — досчитать хэши старых фото для ловли дублей "
            "(/rehash_stop — остановить)",
        ]
    lines += ["", "💡 Команды подсвечиваются в меню «/» рядом с полем ввода"]
    return "\n".join(lines)


async def _show_text_screen(
    callback: CallbackQuery, bot: Bot, text: str, kb: InlineKeyboardMarkup
) -> None:
    """Показывает текстовый экран поверх сообщения callback'а.

    Медиа-сообщение нельзя edit_text — тогда удаляем и шлём новое.
    """
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except TelegramAPIError as e:
            if "message is not modified" in str(e).lower():
                return
            try:
                await message.delete()
            except TelegramAPIError:
                pass
            await message.answer(text, reply_markup=kb)
            return
    await bot.send_message(callback.from_user.id, text, reply_markup=kb)


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    is_admin: bool,
) -> None:
    # не state.clear(): и при обычном /start, и при активации инвайта
    # вопросы «куда сохранить?» / «похоже на дубль» ещё висят в чате
    # с живыми кнопками — сохраняем pending и dup_candidates
    await clear_state_keep_pending(state)

    # приветствие собираем до редима: rollback внутри redeem_invite
    # (гонка) протухает объект user, и доступ к его полям упадёт
    greeting = _greeting_text(user)

    args = command.args or ""
    if args.startswith("inv_"):
        code = args[4:]
        invite = await repo.get_invite_by_code(session, code)
        granted = (
            await repo.redeem_invite(session, invite, user.id)
            if invite is not None
            else []
        )
        if not granted:
            await message.answer(
                "😕 Увы, этот инвайт недействителен или уже истёк.\n"
                "Попроси у того, кто его прислал, свежую ссылку!"
            )
        else:
            lines = "\n".join(f"📁 {html.escape(c.title)}" for c in granted)
            upload_note = (
                "\n\nИ да — в них можно загружать свои находки 📤"
                if invite is not None and invite.can_upload
                else ""
            )
            await message.answer(
                "🎉 Инвайт принят! Теперь тебе открыты категории:\n\n"
                f"{lines}{upload_note}"
            )

    await message.answer(greeting, reply_markup=main_menu_kb(is_admin))


@router.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: Message, bot: Bot, is_admin: bool) -> None:
    me = await bot.me()
    await message.answer(
        _help_text(is_admin, me.username), reply_markup=back_to_menu_kb()
    )


@router.callback_query(MenuCB.filter(F.action == "help"))
async def cb_help(callback: CallbackQuery, bot: Bot, is_admin: bool) -> None:
    me = await bot.me()
    await _show_text_screen(
        callback, bot, _help_text(is_admin, me.username), back_to_menu_kb()
    )
    await callback.answer()
