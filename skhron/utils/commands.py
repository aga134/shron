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

from skhron.config import Config

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
    BotCommand(command="categories", description="Что открыто этой группе"),
]


async def setup_bot_commands(bot: Bot, config: Config) -> None:
    try:
        await bot.set_my_commands(
            PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats()
        )
        await bot.set_my_commands(
            GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats()
        )
    except TelegramAPIError:
        logger.warning("Не удалось зарегистрировать подсказки команд", exc_info=True)
    # админам из конфига — расширенный список (в их личке с ботом)
    for admin_id in config.admin_ids:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except TelegramAPIError:
            # чат с ботом ещё не открыт — подсказки появятся после рестарта
            pass
