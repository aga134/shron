"""Админка: статистика Схрона и бэкап файла БД."""

import html
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.keyboards.callbacks import AdminCB
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
