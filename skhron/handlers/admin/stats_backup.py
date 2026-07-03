"""Админка: статистика Схрона, бэкап файла БД и докачка pHash (/rehash)."""

import asyncio
import html
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skhron.config import Config
from skhron.db import repo
from skhron.keyboards.callbacks import AdminCB
from skhron.services.dedup import compute_phash_from_file
from skhron.utils.media import MEDIA_TYPE_LABELS

logger = logging.getLogger(__name__)

router = Router(name="admin_stats_backup")


def _stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=AdminCB(section="stats").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ В админку",
                    callback_data=AdminCB(section="home").pack(),
                )
            ],
        ]
    )


def _stats_text(stats: dict) -> str:
    lines = [
        "📊 <b>Статистика Схрона</b>",
        "",
        f"👥 Юзеров: <b>{stats['users']}</b>",
        f"📁 Категорий: <b>{stats['categories_active']}</b> активных, "
        f"<b>{stats['categories_archived']}</b> в архиве 📦",
        f"🗃 Всего файлов: <b>{stats['media_total']}</b>",
    ]
    by_type: dict = stats["media_by_type"]
    if by_type:
        lines += ["", "<b>По типам:</b>"]
        for media_type, count in sorted(by_type.items(), key=lambda x: -x[1]):
            label = MEDIA_TYPE_LABELS.get(media_type, media_type)
            lines.append(f"· {label}: {count}")
    top = stats["top_categories"]
    if top:
        lines += ["", "<b>Топ-5 категорий:</b>"]
        for place, (category, count) in enumerate(top, start=1):
            lines.append(f"{place}. {html.escape(category.title)} — {count}")
    lines += ["", f"⭐️ Всего в избранных: <b>{stats['favorites']}</b>"]
    return "\n".join(lines)


@router.callback_query(AdminCB.filter(F.section == "stats"))
async def show_stats(callback: CallbackQuery, session: AsyncSession) -> None:
    text = _stats_text(await repo.get_stats(session))
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("Не вижу сообщение, открой статистику заново 🙈")
        return
    try:
        await message.edit_text(text, reply_markup=_stats_kb())
    except TelegramAPIError as error:
        if "message is not modified" in str(error).lower():
            # «Обновить» без изменений — так и говорим
            await callback.answer("Без изменений")
            return
        # Скорее всего, предыдущее сообщение с медиа — его нельзя edit_text
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        await message.answer(text, reply_markup=_stats_kb())
    await callback.answer()


@router.callback_query(AdminCB.filter(F.section == "backup"))
async def send_backup(callback: CallbackQuery, bot: Bot, config: Config) -> None:
    caption = (
        f"💾 Бэкап Схрона · {datetime.now().strftime('%d.%m.%Y %H:%M')}.\n"
        "Вместе с каналом-архивом это полная резервная копия."
    )
    try:
        await bot.send_document(
            chat_id=callback.from_user.id,
            document=FSInputFile(config.database_path),
            caption=caption,
        )
    except (TelegramAPIError, OSError):
        logger.exception("Не удалось отправить бэкап БД %s", config.database_path)
        await callback.answer(
            "Не получилось отправить бэкап 😔 Проверь, что файл БД на месте, "
            "и загляни в логи.",
            show_alert=True,
        )
        return
    await callback.answer("💾 Бэкап улетел!")


_REHASH_VIDEO_NOTE = (
    "Видео и гифки получают хэш при загрузке — "
    "для старых записей его не восстановить."
)

# Фоновая задача /rehash: прогон долгий, и держать его внутри хендлера нельзя —
# per-user lock SimpleEventIsolation заблокировал бы все апдейты админа.
_rehash_task: asyncio.Task | None = None


async def _rehash_worker(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    items: list[tuple[int, str]],
    progress_msg: Message,
) -> None:
    """Считает хэши со своей сессией: сессия хендлера закрывается вместе с ним."""
    total = len(items)
    done = 0
    hashed = 0
    failed = 0
    try:
        async with session_factory() as session:
            for media_id, file_id in items:
                # compute_phash_from_file сам глотает ошибки и вернёт None
                phash = await compute_phash_from_file(bot, file_id)
                if phash is None:
                    failed += 1
                else:
                    await repo.set_media_phash(session, media_id, phash)
                    hashed += 1
                done += 1
                if done % 25 == 0:
                    try:
                        await progress_msg.edit_text(f"🔁 {done}/{total}…")
                    except TelegramAPIError:
                        logger.warning("Не удалось обновить прогресс /rehash")
                # небольшая пауза, чтобы не провоцировать 429
                await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        try:
            await progress_msg.edit_text(f"⏹ Остановлено: успел {done}/{total}")
        except TelegramAPIError:
            logger.warning("Не удалось отправить итог остановки /rehash")
        return
    except Exception:
        logger.exception("Прогон /rehash упал на %s/%s", done, total)
        try:
            await progress_msg.edit_text(
                f"Прервалось на {done}/{total}, детали в логах"
            )
        except TelegramAPIError:
            logger.warning("Не удалось отправить итог падения /rehash")
        return
    try:
        await progress_msg.edit_text(
            f"Готово: захэшировано {hashed}, не удалось {failed}.\n"
            f"{_REHASH_VIDEO_NOTE}"
        )
    except TelegramAPIError:
        logger.exception("Не удалось отправить итог /rehash")


@router.message(Command("rehash"), F.chat.type == "private")
async def rehash_media(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Докачивает pHash задним числом — фоновой задачей.

    Только для фото: их file_id — сама картинка. У видео/гифок/кружков
    превью-кадр по file_id не достать, они хэшируются при новых загрузках.
    """
    global _rehash_task
    if _rehash_task and not _rehash_task.done():
        await message.answer(
            "Уже считаю — останови через /rehash_stop, если надо"
        )
        return
    items = [
        (m.id, m.file_id)
        for m in await repo.list_media_without_phash(session, media_type="photo")
    ]
    if not items:
        await message.answer(f"Все фото уже с хэшами 👌\n{_REHASH_VIDEO_NOTE}")
        return
    progress_msg = await message.answer(
        f"🔁 Запустил пересчёт: {len(items)} фото. "
        "Прогресс буду обновлять здесь; /rehash_stop — остановить"
    )
    _rehash_task = asyncio.create_task(
        _rehash_worker(bot, session_factory, items, progress_msg)
    )


@router.message(Command("rehash_stop"), F.chat.type == "private")
async def rehash_stop(message: Message) -> None:
    if _rehash_task and not _rehash_task.done():
        _rehash_task.cancel()
        await message.answer("Останавливаю…")
    else:
        await message.answer("Сейчас ничего не считается")
