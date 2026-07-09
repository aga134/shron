"""«Мем дня»: фоновый планировщик автопостов в группы.

Каждую минуту проверяет группы с включённым расписанием и шлёт случайный
мем из открытых группе категорий при первом тике после назначенного
времени (DISPLAY_TZ). Дата последней отправки хранится в chats —
рестарт бота не приводит к дублям.
"""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

from skhron.db import repo
from skhron.keyboards.callbacks import GroupRandomCB
from skhron.services import access
from skhron.utils.dates import DISPLAY_TZ
from skhron.utils.media import media_caption, send_media

logger = logging.getLogger(__name__)

_daily_task: asyncio.Task | None = None


async def start_scheduler(bot: Bot, session_factory: async_sessionmaker) -> None:
    global _daily_task
    if _daily_task is None or _daily_task.done():
        _daily_task = asyncio.create_task(_daily_loop(bot, session_factory))


async def stop_scheduler() -> None:
    """dp.shutdown-хук: гасим цикл до закрытия сессии бота."""
    if _daily_task and not _daily_task.done():
        _daily_task.cancel()
        with suppress(asyncio.CancelledError):
            await _daily_task


async def _daily_loop(bot: Bot, session_factory: async_sessionmaker) -> None:
    while True:
        try:
            await _tick(bot, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — цикл не должен умирать
            logger.exception("Сбой тика «мема дня»")
        await asyncio.sleep(60)


def _more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎲 Ещё",
                    callback_data=GroupRandomCB(category_id=0).pack(),
                )
            ]
        ]
    )


async def _tick(bot: Bot, session_factory: async_sessionmaker) -> None:
    now = datetime.now(DISPLAY_TZ)
    minutes_now = now.hour * 60 + now.minute
    today = now.strftime("%Y-%m-%d")

    async with session_factory() as session:
        for chat in await repo.list_daily_chats(session):
            if chat.daily_last_sent == today or minutes_now < chat.daily_minutes:
                continue
            category_ids = await access.group_viewable_category_ids(
                session, chat.id
            )
            media = (
                await repo.get_random_media(session, category_ids)
                if category_ids
                else None
            )
            if media is None:
                # группе нечего показать — не долбим проверку до завтра
                await repo.set_chat_daily_sent(session, chat.id, today)
                continue
            category = await repo.get_category(session, media.category_id)
            try:
                await send_media(
                    bot,
                    chat.id,
                    media,
                    caption="🌅 Мем дня\n\n" + media_caption(media, category),
                    reply_markup=_more_kb(),
                )
            except TelegramRetryAfter:
                # флуд-лимит: не помечаем — попробуем на следующем тике
                continue
            except TelegramForbiddenError:
                # бота выгнали, а my_chat_member потерялся — гасим группу
                await repo.set_chat_active(session, chat.id, False)
                continue
            except TelegramBadRequest:
                # постоянная ошибка (например, закрытый General-топик
                # форум-группы): выключаем расписание, чтобы не «отправлять»
                # мем в никуда каждый день — админ увидит «выключен» в карточке
                logger.warning(
                    "«Мем дня» для чата %s выключен: отправка невозможна",
                    chat.id,
                    exc_info=True,
                )
                await repo.set_chat_daily(session, chat.id, None)
                continue
            except TelegramAPIError:
                logger.warning(
                    "Не удалось отправить «мем дня» в чат %s", chat.id,
                    exc_info=True,
                )
                # помечаем, чтобы не спамить ретраями каждую минуту
            await repo.set_chat_daily_sent(session, chat.id, today)
