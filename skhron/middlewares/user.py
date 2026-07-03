from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from skhron.db import repo
from skhron.services import access


class UserMiddleware(BaseMiddleware):
    """Регистрирует/обновляет пользователя и кладёт в data:

    - data["user"] — модель User из БД;
    - data["is_admin"] — bool.

    Регистрируется на dp.update ПОСЛЕ DbSessionMiddleware.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is not None and not tg_user.is_bot:
            session = data["session"]
            config = data["config"]
            user = await repo.upsert_user(
                session, tg_user.id, tg_user.username, tg_user.full_name or ""
            )
            data["user"] = user
            data["is_admin"] = access.is_admin(user, config)
        return await handler(event, data)
