"""Перцептивное хэширование для ловли «почти дублей».

file_unique_id ловит только байт-в-байт тот же файл Telegram. Если мем
скачали и перезалили (пережатие, другой мессенджер) — файл уже другой.
dHash считается по картинке (фото) или по превью-кадру (видео/гифка/кружок):
похожие изображения дают близкие хэши, расстояние Хэмминга — мера сходства.
"""

import asyncio
import io
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message
from PIL import Image

logger = logging.getLogger(__name__)

# Максимальное расстояние Хэмминга (из 64 бит), при котором файлы считаем
# «похоже, это один и тот же мем». 0 — идентичные кадры, 8 — уверенное
# сходство с запасом на пережатие; больше — уже случайные совпадения.
PHASH_MAX_DISTANCE = 8


def dhash(image: Image.Image) -> str:
    """64-битный difference hash: 16 hex-символов.

    Картинка сжимается до 9x8 в оттенках серого, каждый бит — сравнение
    соседних по горизонтали пикселей. Чистый Pillow, без numpy.
    """
    gray = image.convert("L").resize((9, 8), Image.LANCZOS)
    # tobytes() для режима "L" — те же пиксели построчно, без deprecated getdata
    pixels = gray.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            left = pixels[row * 9 + col]
            right = pixels[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def phash_distance(h1: str, h2: str) -> int:
    """Расстояние Хэмминга между двумя hex-хэшами."""
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def _preview_file_id(message: Message) -> str | None:
    """file_id картинки, по которой считаем хэш."""
    if message.photo:
        # самое маленькое превью: dHash всё равно сжимает до 9x8
        return message.photo[0].file_id
    for media in (message.video, message.animation, message.video_note):
        if media is not None:
            return media.thumbnail.file_id if media.thumbnail else None
    return None


async def compute_phash_from_message(bot: Bot, message: Message) -> str | None:
    """dHash по медиа из сообщения; None — не картинка/не вышло скачать."""
    file_id = _preview_file_id(message)
    if file_id is None:
        return None
    return await compute_phash_from_file(bot, file_id)


async def compute_phash_from_file(bot: Bot, file_id: str) -> str | None:
    for attempt in range(2):
        try:
            buf = io.BytesIO()
            await bot.download(file_id, destination=buf)
            buf.seek(0)
            with Image.open(buf) as image:
                return dhash(image)
        except TelegramRetryAfter as e:
            # flood-wait надо выдержать, иначе остаток очереди «сгорит» в 429
            logger.warning(
                "Flood-wait %s c при pHash для %s", e.retry_after, file_id
            )
            await asyncio.sleep(e.retry_after)
        except Exception:
            # хэш — вспомогательная фича: не роняем загрузку из-за него
            logger.warning(
                "Не удалось посчитать pHash для %s", file_id, exc_info=True
            )
            return None
    return None
