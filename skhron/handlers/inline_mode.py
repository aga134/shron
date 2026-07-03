"""Инлайн-режим: набери @бота в любом чате — и кидай мемы из Схрона друзьям.

Пустой запрос — свежие загрузки, текст — поиск по подписям.
Показываются только категории, доступные пользователю на просмотр.

Инлайн-режим нужно включить у BotFather командой /setinline.
"""

import logging

from aiogram import Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResult,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedMpeg4Gif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
    InlineQueryResultsButton,
)
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import Media, User
from skhron.services import access

logger = logging.getLogger(__name__)

router = Router(name="inline_mode")

# Сколько результатов отдаём за раз (лимит Telegram — 50)
MAX_RESULTS = 30
# Лимит подписи в инлайн-результате (лимит Telegram — 1024)
CAPTION_LIMIT = 900
# Лимит заголовка в списке результатов
TITLE_LIMIT = 64


def _plain_caption(media: Media) -> str | None:
    """Подпись как есть (plain, без HTML), обрезанная до CAPTION_LIMIT."""
    if not media.caption:
        return None
    caption = media.caption
    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 1] + "…"
    return caption


def _title(media: Media, fallback: str) -> str:
    """Заголовок результата: первая строка подписи или запасной вариант."""
    if media.caption:
        first_line = media.caption.strip().splitlines()[0].strip()
        if first_line:
            if len(first_line) > TITLE_LIMIT:
                first_line = first_line[: TITLE_LIMIT - 1] + "…"
            return first_line
    return fallback


def _to_result(media: Media) -> InlineQueryResult | None:
    """Маппит медиа в инлайн-результат. None — тип не поддерживается инлайном."""
    result_id = str(media.id)
    caption_kwargs: dict = {}
    caption = _plain_caption(media)
    if caption is not None:
        # parse_mode=None перебивает дефолтный HTML бота: подпись уходит plain
        caption_kwargs = {"caption": caption, "parse_mode": None}

    if media.media_type == "photo":
        return InlineQueryResultCachedPhoto(
            id=result_id, photo_file_id=media.file_id, **caption_kwargs
        )
    if media.media_type == "video":
        return InlineQueryResultCachedVideo(
            id=result_id,
            video_file_id=media.file_id,
            title=_title(media, "Видео из Схрона"),
            **caption_kwargs,
        )
    if media.media_type == "animation":
        return InlineQueryResultCachedMpeg4Gif(
            id=result_id, mpeg4_file_id=media.file_id, **caption_kwargs
        )
    if media.media_type == "voice":
        return InlineQueryResultCachedVoice(
            id=result_id,
            voice_file_id=media.file_id,
            title=_title(media, "Войс из Схрона"),
            **caption_kwargs,
        )
    if media.media_type == "audio":
        return InlineQueryResultCachedAudio(
            id=result_id, audio_file_id=media.file_id, **caption_kwargs
        )
    # video_note инлайном отправить нельзя — пропускаем
    return None


@router.inline_query()
async def inline_search(
    query: InlineQuery,
    session: AsyncSession,
    user: User,
    config: Config,
) -> None:
    ids = await access.viewable_category_ids(session, user, config)

    text = (query.query or "").strip()
    if text:
        media_items = await repo.search_media(session, ids, text, MAX_RESULTS)
    else:
        media_items = await repo.recent_media(session, ids, MAX_RESULTS)

    results: list[InlineQueryResult] = []
    for media in media_items:
        result = _to_result(media)
        if result is not None:
            results.append(result)

    button = None
    if not results:
        button = InlineQueryResultsButton(
            text="Открыть Схрон", start_parameter="inline"
        )

    await query.answer(results, cache_time=5, is_personal=True, button=button)
