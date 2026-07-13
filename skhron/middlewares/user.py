from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from skhron.db import repo
from skhron.services import access

_GROUP_TYPES = {"group", "supergroup"}


class UserMiddleware(BaseMiddleware):
    """Регистрирует/обновляет пользователя и кладёт в data:

    - data["user"] — модель User из БД;
    - data["is_admin"] — bool.

    Регистрируется на dp.update ПОСЛЕ DbSessionMiddleware.

    В группах регистрируем только тех, кто реально взаимодействует с ботом
    (команда или ответ на его сообщение): при выключенном privacy mode бот
    видит каждое сообщение группы, и без этого фильтра любой болтун попадал
    бы в таблицу users (и в админские списки «выдать доступ»), а каждое
    сообщение стоило бы обращения к SQLite.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if (
            tg_user is not None
            and not tg_user.is_bot
            and self._should_register(event, data)
        ):
            session = data["session"]
            config = data["config"]
            user = await repo.upsert_user(
                session, tg_user.id, tg_user.username, tg_user.full_name or ""
            )
            data["user"] = user
            data["is_admin"] = access.is_admin(user, config)
        return await handler(event, data)

    @staticmethod
    def _should_register(event: TelegramObject, data: dict[str, Any]) -> bool:
        if not isinstance(event, Update) or event.message is None:
            # колбэки, инлайн-запросы, my_chat_member — всегда осознанное
            # взаимодействие с ботом
            return True
        message = event.message
        if message.chat.type not in _GROUP_TYPES:
            return True  # личка
        text = message.text or message.caption or ""
        if text.startswith("/"):
            return True  # команда
        reply = message.reply_to_message
        bot = data.get("bot")
        if (
            reply is not None
            and reply.from_user is not None
            and bot is not None
            and reply.from_user.id == bot.id
        ):
            return True  # ответ на сообщение бота (ввод номера и т.п.)
        return False
