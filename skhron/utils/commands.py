"""Регистрация подсказок команд (меню «/» в клиентах Telegram)."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo

logger = logging.getLogger(__name__)

PRIVATE_COMMANDS = [
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="help", description="Справка и список команд"),
    BotCommand(command="start", description="Перезапуск бота"),
]

ADMIN_COMMANDS = PRIVATE_COMMANDS + [
    BotCommand(command="admin", description="Админ-панель"),
    BotCommand(command="rehash", description="Досчитать хэши старых фото"),
]

GROUP_COMMANDS = [
    BotCommand(command="random", description="Случайный мем"),
    BotCommand(command="feed", description="Лента категории"),
    BotCommand(command="save", description="Сохранить мем (ответом на него)"),
    BotCommand(command="top", description="Топ мемов группы по лайкам"),
    BotCommand(command="categories", description="Что открыто этой группе"),
]


async def setup_bot_commands(
    bot: Bot, config: Config, session: AsyncSession
) -> None:
    try:
        await bot.set_my_commands(
            PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats()
        )
        await bot.set_my_commands(
            GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats()
        )
    except TelegramAPIError:
        logger.warning("Не удалось зарегистрировать подсказки команд", exc_info=True)
    # админам — расширенный список: и из конфига, и повышенным через админку
    admin_ids = set(config.admin_ids)
    admin_ids.update(user.id for user in await repo.list_admins(session))
    for admin_id in admin_ids:
        await set_admin_scope(bot, admin_id, True)


async def set_admin_scope(bot: Bot, user_id: int, is_admin: bool) -> None:
    """Включает/выключает админ-подсказки в личке конкретного юзера.

    Зовётся на старте и при повышении/снятии через админку.
    """
    try:
        if is_admin:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=user_id)
            )
        else:
            await bot.delete_my_commands(
                scope=BotCommandScopeChat(chat_id=user_id)
            )
    except TelegramAPIError:
        # чат с ботом ещё не открыт — подсказки появятся после рестарта
        pass
