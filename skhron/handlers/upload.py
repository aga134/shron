"""Загрузка медиа в Схрон.

Два потока:
1. Через меню «📤 Загрузить»: выбор категории → состояние collecting,
   каждый присланный файл сохраняется сразу, в конце «✅ Готово».
2. Юзер просто прислал медиа без состояния (catch-all, роутер подключается
   последним): файлы копятся в pending, потом один вопрос «Куда сохранить?»
   и сохранение всей пачки после выбора категории.
"""

import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import Category, Media, User
from skhron.keyboards.callbacks import (
    MenuCB,
    UploadDoneCB,
    UploadPendingPickCB,
    UploadPickCB,
)
from skhron.keyboards.common import categories_pick_kb, main_menu_kb
from skhron.services import access
from skhron.services.archive import archive_copy
from skhron.utils.media import extract_media

router = Router(name="upload")

# Только медиа, текст не перехватываем
MEDIA_FILTER = F.photo | F.video | F.animation | F.video_note | F.voice | F.audio


class UploadStates(StatesGroup):
    choosing_category = State()
    collecting = State()


# ---------------------------------------------------------------- helpers


def _done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Готово", callback_data=UploadDoneCB().pack()
                )
            ]
        ]
    )


def _cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="↩️ Отмена", callback_data=UploadDoneCB().pack()
    )


async def _show_screen(
    message: Message | InaccessibleMessage | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Правило 10: сообщение с медиа нельзя edit_text — тогда удаляем
    и шлём новое. Старое (>48ч) сообщение приходит как InaccessibleMessage —
    его нельзя ни редактировать, ни удалять, поэтому просто шлём новое."""
    if message is None:
        return
    if not isinstance(message, Message):
        await message.answer(text, reply_markup=reply_markup)
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramAPIError as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        await message.answer(text, reply_markup=reply_markup)


async def _save_one(
    bot: Bot,
    session: AsyncSession,
    config: Config,
    user: User,
    category: Category,
    media_type: str,
    file_id: str,
    file_unique_id: str,
    caption: str | None,
    src_chat_id: int,
    src_message_id: int,
) -> tuple[bool, Media]:
    """Запись в БД (с дедупом), затем копия в канал-архив — только для
    реально созданных записей, чтобы дубликаты не засоряли архив.
    Возвращает (created, media)."""
    media, created = await repo.add_media(
        session,
        category.id,
        file_id,
        file_unique_id,
        media_type,
        caption,
        user.id,
    )
    if not created:
        return False, media
    if media.archive_message_id is None:
        archive_chat_id, archive_message_id = await archive_copy(
            bot,
            config,
            src_chat_id,
            src_message_id,
            media_type,
            category.title,
            user.full_name,
        )
        if archive_message_id is not None:
            media.archive_chat_id = archive_chat_id
            media.archive_message_id = archive_message_id
            await session.commit()
    return created, media


# ---------------------------------------------------------------- поток 1: из меню


@router.callback_query(MenuCB.filter(F.action == "upload"))
async def menu_upload(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    is_admin: bool,
) -> None:
    cats = await access.uploadable_categories(session, user, config)
    if not cats:
        if is_admin:
            await callback.answer(
                "Категорий пока нет — создай первую через /admin 😉",
                show_alert=True,
            )
        else:
            await callback.answer(
                "У тебя нет прав на загрузку — попроси админа 🔒", show_alert=True
            )
        return
    await _show_screen(
        callback.message,
        "Куда сохраняем? 📤",
        categories_pick_kb(
            cats, make_cb=lambda c: UploadPickCB(category_id=c.id)
        ),
    )
    await callback.answer()


# -------- выбор категории: специфичные хендлеры — раньше, fallback-и — после


@router.callback_query(
    UploadPendingPickCB.filter(), StateFilter(UploadStates.choosing_category)
)
async def pick_category_for_pending(
    callback: CallbackQuery,
    callback_data: UploadPendingPickCB,
    session: AsyncSession,
    user: User,
    config: Config,
    is_admin: bool,
    state: FSMContext,
    bot: Bot,
) -> None:
    """Поток 2: юзер прислал медиа заранее — сохраняем всю пачку pending."""
    if not await access.can_upload(session, user, config, callback_data.category_id):
        await callback.answer(
            "Сюда загружать нельзя — попроси доступ у админа 🔒", show_alert=True
        )
        return
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория куда-то делась 🤔", show_alert=True)
        return

    data = await state.get_data()
    pending = data.get("pending", [])
    await state.clear()

    if not pending:
        await _show_screen(
            callback.message,
            "Хм, файлы потерялись — пришли их ещё раз, пожалуйста 🤔",
            main_menu_kb(is_admin),
        )
        await callback.answer()
        return

    saved = 0
    duplicates = 0
    for item in pending:
        created, _ = await _save_one(
            bot,
            session,
            config,
            user,
            category,
            item["media_type"],
            item["file_id"],
            item["file_unique_id"],
            item.get("caption"),
            item["src_chat_id"],
            item["src_message_id"],
        )
        if created:
            saved += 1
        else:
            duplicates += 1

    title = html.escape(category.title)
    text = f"✅ Сохранено {saved} шт. в «{title}»"
    if duplicates:
        text += f", дубликатов: {duplicates}"
    await _show_screen(callback.message, text, main_menu_kb(is_admin))
    await callback.answer()


@router.callback_query(UploadPickCB.filter(), StateFilter(UploadStates.collecting))
async def pick_category_switch(
    callback: CallbackQuery,
    callback_data: UploadPickCB,
    session: AsyncSession,
    user: User,
    config: Config,
    state: FSMContext,
) -> None:
    """Смена категории на лету в режиме collecting."""
    if not await access.can_upload(session, user, config, callback_data.category_id):
        await callback.answer(
            "Сюда загружать нельзя — попроси доступ у админа 🔒", show_alert=True
        )
        return
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория куда-то делась 🤔", show_alert=True)
        return
    await state.update_data(category_id=category.id)
    title = html.escape(category.title)
    await _show_screen(
        callback.message,
        f"Теперь сохраняю в «{title}» 📁 Кидай дальше, "
        "как закончишь — жми Готово ✅",
        _done_kb(),
    )
    await callback.answer(f"Теперь сохраняю в «{category.title}»")


@router.callback_query(UploadPickCB.filter(), StateFilter(None))
async def pick_category_start_collecting(
    callback: CallbackQuery,
    callback_data: UploadPickCB,
    session: AsyncSession,
    user: User,
    config: Config,
    state: FSMContext,
) -> None:
    """Поток 1: категория выбрана из меню — включаем режим collecting."""
    if not await access.can_upload(session, user, config, callback_data.category_id):
        await callback.answer(
            "Сюда загружать нельзя — попроси доступ у админа 🔒", show_alert=True
        )
        return
    category = await repo.get_category(session, callback_data.category_id)
    if category is None:
        await callback.answer("Категория куда-то делась 🤔", show_alert=True)
        return
    await state.set_state(UploadStates.collecting)
    await state.set_data({"category_id": category.id, "saved_count": 0})
    title = html.escape(category.title)
    await _show_screen(
        callback.message,
        f"Кидай фото/видео/гифки/кружки/войсы — всё сохраню в «{title}». "
        "Альбомы тоже можно. Когда закончишь — жми Готово ✅",
        _done_kb(),
    )
    await callback.answer()


# -------- fallback-и: регистрируются ПОСЛЕ специфичных хендлеров,
# срабатывают только когда ни один StateFilter выше не подошёл


@router.callback_query(UploadPendingPickCB.filter())
async def pick_pending_stale(
    callback: CallbackQuery,
    is_admin: bool,
    state: FSMContext,
) -> None:
    """Кнопка «Куда сохранить?» протухла: state=None (бота перезапускали
    или пачку уже сохранили) либо юзер сейчас в другом FSM-диалоге."""
    if await state.get_state() is None:
        await _show_screen(
            callback.message,
            "Хм, файлы потерялись (наверное, бота перезапускали) — "
            "пришли их ещё раз, пожалуйста 🤔",
            main_menu_kb(is_admin),
        )
        await callback.answer()
        return
    # Чужое состояние (админ-диалог и т.п.) — не трогаем, только подсказка
    await callback.answer("Сначала заверши текущее действие 🙂", show_alert=True)


@router.callback_query(UploadPickCB.filter())
async def pick_category_other_state(callback: CallbackQuery) -> None:
    """Кнопка выбора категории нажата в чужом FSM-состоянии
    (админ-диалог и т.п.) — не ломаем активный поток, просто отвечаем."""
    await callback.answer(
        "Сначала заверши текущее действие, потом выбирай категорию 🙂",
        show_alert=True,
    )


# -------- режим collecting: приём файлов


@router.message(
    StateFilter(UploadStates.collecting), F.chat.type == "private", MEDIA_FILTER
)
async def collect_media(
    message: Message,
    session: AsyncSession,
    user: User,
    config: Config,
    is_admin: bool,
    state: FSMContext,
    bot: Bot,
) -> None:
    extracted = extract_media(message)
    if extracted is None:
        return
    media_type, file_id, file_unique_id = extracted

    data = await state.get_data()
    category_id = data.get("category_id")
    category = (
        await repo.get_category(session, category_id)
        if category_id is not None
        else None
    )
    # Повторная проверка: права могли отозвать, категорию — удалить/заархивировать
    if category is None or not await access.can_upload(
        session, user, config, category.id
    ):
        await state.clear()
        await message.answer(
            "🔒 Эта категория больше недоступна — загрузку остановил.",
            reply_markup=main_menu_kb(is_admin),
        )
        return

    created, _ = await _save_one(
        bot,
        session,
        config,
        user,
        category,
        media_type,
        file_id,
        file_unique_id,
        message.caption,
        message.chat.id,
        message.message_id,
    )
    title = html.escape(category.title)
    if not created:
        await message.reply(f"⚠️ Это уже есть в «{title}»", reply_markup=_done_kb())
        return
    saved_count = data.get("saved_count", 0) + 1
    await state.update_data(saved_count=saved_count)
    await message.reply(
        f"✅ #{saved_count} сохранено в «{title}»", reply_markup=_done_kb()
    )


@router.message(StateFilter(UploadStates.collecting), F.chat.type == "private", F.text)
async def collect_text_hint(message: Message) -> None:
    await message.answer(
        "Жду медиа 👀 Закончил — жми ✅ Готово", reply_markup=_done_kb()
    )


# -------- завершение / отмена


@router.callback_query(UploadDoneCB.filter())
async def upload_done(
    callback: CallbackQuery,
    is_admin: bool,
    state: FSMContext,
) -> None:
    current_state = await state.get_state()
    data = await state.get_data()
    await state.clear()

    if current_state == UploadStates.choosing_category.state:
        # Отмена выбора категории (поток 2)
        text = "Окей, отменил 👌"
    else:
        saved_count = data.get("saved_count", 0)
        text = f"Готово! Сохранено {saved_count} шт. 📦"

    await _show_screen(callback.message, text, main_menu_kb(is_admin))
    await callback.answer()


# ---------------------------------------------------------------- поток 2: catch-all
# Роутер upload подключается ПОСЛЕДНИМ — эти хендлеры ловят «просто прислал медиа».


@router.message(
    StateFilter(UploadStates.choosing_category),
    F.chat.type == "private",
    MEDIA_FILTER,
)
async def append_pending_media(
    message: Message,
    state: FSMContext,
) -> None:
    """Прилетело ещё медиа (альбом), пока висит вопрос «Куда сохранить?» —
    просто дописываем в pending без нового вопроса."""
    extracted = extract_media(message)
    if extracted is None:
        return
    media_type, file_id, file_unique_id = extracted
    data = await state.get_data()
    pending = data.get("pending", [])
    pending.append(
        {
            "media_type": media_type,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "caption": message.caption,
            "src_chat_id": message.chat.id,
            "src_message_id": message.message_id,
        }
    )
    await state.update_data(pending=pending)


@router.message(StateFilter(None), F.chat.type == "private", MEDIA_FILTER)
async def unsolicited_media(
    message: Message,
    session: AsyncSession,
    user: User,
    config: Config,
    is_admin: bool,
    state: FSMContext,
) -> None:
    extracted = extract_media(message)
    if extracted is None:
        return
    media_type, file_id, file_unique_id = extracted

    uploadable = await access.uploadable_categories(session, user, config)
    if not uploadable:
        if is_admin:
            await message.answer(
                "Категорий пока нет — создай первую через /admin 😉"
            )
        else:
            await message.answer(
                "🔒 Сохранять могут только те, кому админ выдал право на загрузку"
            )
        return

    await state.set_state(UploadStates.choosing_category)
    await state.set_data(
        {
            "pending": [
                {
                    "media_type": media_type,
                    "file_id": file_id,
                    "file_unique_id": file_unique_id,
                    "caption": message.caption,
                    "src_chat_id": message.chat.id,
                    "src_message_id": message.message_id,
                }
            ]
        }
    )
    await message.answer(
        "Куда сохранить? 📁",
        reply_markup=categories_pick_kb(
            uploadable,
            make_cb=lambda c: UploadPendingPickCB(category_id=c.id),
            back_button=_cancel_button(),
        ),
    )
