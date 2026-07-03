"""Извлечение медиа из сообщений и отправка медиа по file_id."""

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message, MessageId

from skhron.db.models import Category, Media

logger = logging.getLogger(__name__)

# Типы, у которых бывает подпись (кружкам caption не передаём)
_CAPTIONABLE = ("photo", "video", "animation", "voice", "audio")

# Человекочитаемые названия типов
MEDIA_TYPE_LABELS = {
    "photo": "🖼 фото",
    "video": "🎬 видео",
    "animation": "🎞 гифка",
    "video_note": "⚪️ кружок",
    "voice": "🎤 войс",
    "audio": "🎵 аудио",
}


def extract_media(message: Message) -> tuple[str, str, str] | None:
    """Возвращает (media_type, file_id, file_unique_id) или None,
    если в сообщении нет поддерживаемого медиа."""
    # animation проверяем до video: у гифок Bot API заполняет и document
    if message.photo:
        photo = message.photo[-1]  # самое большое разрешение
        return "photo", photo.file_id, photo.file_unique_id
    if message.animation:
        return (
            "animation",
            message.animation.file_id,
            message.animation.file_unique_id,
        )
    if message.video:
        return "video", message.video.file_id, message.video.file_unique_id
    if message.video_note:
        return (
            "video_note",
            message.video_note.file_id,
            message.video_note.file_unique_id,
        )
    if message.voice:
        return "voice", message.voice.file_id, message.voice.file_unique_id
    if message.audio:
        return "audio", message.audio.file_id, message.audio.file_unique_id
    return None


def media_caption(media: Media, category: Category | None) -> str:
    """Подпись: оригинальный текст + строка «📁 категория · дата»."""
    parts = []
    if media.caption:
        text = media.caption
        if len(text) > 800:
            text = text[:800] + "…"
        parts.append(html.escape(text))
    footer_bits = []
    if category is not None:
        footer_bits.append(f"📁 {html.escape(category.title)}")
    footer_bits.append(media.created_at.strftime("%d.%m.%Y"))
    parts.append(f"<i>{' · '.join(footer_bits)}</i>")
    return "\n\n".join(parts)


async def send_media(
    bot: Bot,
    chat_id: int,
    media: Media,
    caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    message_thread_id: int | None = None,
) -> Message | MessageId:
    """Отправляет медиа по file_id; если file_id вдруг не сработал,
    достаёт резервную копию из канала-архива. У кружков подписи не бывает.

    message_thread_id — топик форум-супергруппы (None для обычных чатов).
    """
    try:
        return await _send_by_file_id(
            bot, chat_id, media, caption, reply_markup, message_thread_id
        )
    except TelegramBadRequest:
        if not (media.archive_chat_id and media.archive_message_id):
            raise
        logger.warning(
            "file_id медиа %s не сработал — отдаём копию из канала-архива",
            media.id,
        )
        return await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=media.archive_chat_id,
            message_id=media.archive_message_id,
            caption=caption if media.media_type in _CAPTIONABLE else None,
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )


async def _send_by_file_id(
    bot: Bot,
    chat_id: int,
    media: Media,
    caption: str | None,
    reply_markup: InlineKeyboardMarkup | None,
    message_thread_id: int | None = None,
) -> Message:
    common = {
        "reply_markup": reply_markup,
        "message_thread_id": message_thread_id,
    }
    if media.media_type == "photo":
        return await bot.send_photo(
            chat_id, media.file_id, caption=caption, **common
        )
    if media.media_type == "video":
        return await bot.send_video(
            chat_id, media.file_id, caption=caption, **common
        )
    if media.media_type == "animation":
        return await bot.send_animation(
            chat_id, media.file_id, caption=caption, **common
        )
    if media.media_type == "video_note":
        return await bot.send_video_note(chat_id, media.file_id, **common)
    if media.media_type == "voice":
        return await bot.send_voice(
            chat_id, media.file_id, caption=caption, **common
        )
    if media.media_type == "audio":
        return await bot.send_audio(
            chat_id, media.file_id, caption=caption, **common
        )
    raise ValueError(f"Неизвестный тип медиа: {media.media_type}")
