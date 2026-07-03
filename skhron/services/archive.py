"""Резервное копирование загруженных медиа в приватный канал-архив.

Канал — страховка на случай потери БД: все файлы лежат в нём подряд,
с подписью-хэштегом категории, по которой их можно найти глазами.
"""

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from skhron.config import Config

logger = logging.getLogger(__name__)

# Типы, у которых бывает подпись (кружкам caption не передаём)
_CAPTIONABLE = ("photo", "video", "animation", "voice", "audio")


async def archive_copy(
    bot: Bot,
    config: Config,
    from_chat_id: int,
    message_id: int,
    media_type: str,
    category_title: str,
    uploader_name: str,
) -> tuple[int | None, int | None]:
    """Копирует сообщение с медиа в канал-архив.

    Возвращает (chat_id, message_id) копии или (None, None), если архив
    не настроен либо копирование не удалось (это не считается ошибкой
    загрузки — просто пишем warning в лог).
    """
    if config.archive_channel_id is None:
        return None, None

    caption = None
    if media_type in _CAPTIONABLE:
        tag = "#" + category_title.replace(" ", "_")
        caption = f"{html.escape(tag)} · от {html.escape(uploader_name)}"

    try:
        result = await bot.copy_message(
            chat_id=config.archive_channel_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            caption=caption,
        )
        return config.archive_channel_id, result.message_id
    except TelegramAPIError:
        logger.warning(
            "Не удалось скопировать файл в канал-архив %s",
            config.archive_channel_id,
            exc_info=True,
        )
        return None, None
