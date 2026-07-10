from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, TelegramObject


class AdminFilter(BaseFilter):
    """Пропускает событие, только если UserMiddleware пометил юзера админом."""

    async def __call__(self, event: TelegramObject, is_admin: bool = False) -> bool:
        return is_admin


class PrivateCallback(BaseFilter):
    """Колбэк нажат в личке с ботом.

    Инлайн-кнопки переживают пересылку сообщения: без этого фильтра пост
    личной ленты, пересланный в группу, продолжил бы работать там —
    и по нажатию публиковал бы контент в чат мимо групповых прав.
    None-сообщение (очень старые кнопки) пропускаем: хендлеры сами
    обрабатывают его аккуратно.
    """

    async def __call__(self, callback: CallbackQuery) -> bool:
        message = callback.message
        return message is None or message.chat.type == "private"
