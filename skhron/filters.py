from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject


class AdminFilter(BaseFilter):
    """Пропускает событие, только если UserMiddleware пометил юзера админом."""

    async def __call__(self, event: TelegramObject, is_admin: bool = False) -> bool:
        return is_admin
