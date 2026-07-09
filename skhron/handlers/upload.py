"""Загрузка медиа в Схрон.

Два потока:
1. Через меню «📤 Загрузить»: выбор категории → состояние collecting,
   каждый присланный файл сохраняется сразу, в конце «✅ Готово».
2. Юзер просто прислал медиа без состояния (catch-all, роутер подключается
   последним): файлы копятся в pending, потом один вопрос «Куда сохранить?»
   и сохранение всей пачки после выбора категории.
"""

import asyncio
import html
import secrets

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import Category, Media, User
from skhron.keyboards.callbacks import (
    DupCB,
    MenuCB,
    UploadDoneCB,
    UploadPendingPickCB,
    UploadPickCB,
)
from skhron.keyboards.common import categories_pick_kb, main_menu_kb
from skhron.services import access
from skhron.services.archive import archive_copy
from skhron.services.dedup import PHASH_MAX_DISTANCE, compute_phash_from_message
from skhron.utils.fsm import clear_state_keep_pending
from skhron.utils.media import extract_media, media_caption, send_media

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


def _media_item(
    message: Message, extracted: tuple[str, str, str], phash: str | None
) -> dict:
    """Item-словарь одного файла для FSM data и _save_one."""
    media_type, file_id, file_unique_id = extracted
    return {
        "media_type": media_type,
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "caption": message.caption,
        "src_chat_id": message.chat.id,
        "src_message_id": message.message_id,
        "phash": phash,
    }


async def _save_one(
    bot: Bot,
    session: AsyncSession,
    config: Config,
    user: User,
    category: Category,
    item: dict,
    *,
    skip_similar_check: bool = False,
) -> tuple[str, Media | None]:
    """Сохранение одного файла из item-словаря. Статусы:
    - "similar" — в категории нашёлся визуально похожий файл (по pHash),
      ничего не сохранили и в архив не копировали;
    - "duplicate" — байт-в-байт такой файл уже есть (точный дедуп в add_media);
    - "created" — сохранили; копия в канал-архив — только для реально
      созданных записей, чтобы дубликаты не засоряли архив.

    Точный дубль (по file_unique_id, включая мягко удалённые) проверяется
    ДО поиска похожих: тот же самый файл всегда идёт прежним путём add_media
    (честное «уже есть» + восстановление мягко удалённых), даже если рядом
    в категории лежит визуально похожий сосед."""
    if not skip_similar_check and item.get("phash"):
        exact = await repo.get_media_by_unique_id(
            session, category.id, item["file_unique_id"]
        )
        if exact is None:
            similar = await repo.find_similar_media(
                session,
                category.id,
                item["phash"],
                PHASH_MAX_DISTANCE,
                exclude_file_unique_id=item["file_unique_id"],
            )
            if similar is not None:
                return "similar", similar[0]
    media, created = await repo.add_media(
        session,
        category.id,
        item["file_id"],
        item["file_unique_id"],
        item["media_type"],
        item.get("caption"),
        user.id,
        phash=item.get("phash"),
    )
    if not created:
        return "duplicate", media
    if media.archive_message_id is None:
        archive_chat_id, archive_message_id = await archive_copy(
            bot,
            config,
            item["src_chat_id"],
            item["src_message_id"],
            item["media_type"],
            category.title,
            user.full_name,
        )
        if archive_message_id is not None:
            media.archive_chat_id = archive_chat_id
            media.archive_message_id = archive_message_id
            await session.commit()
    return "created", media


# Кап отложенных вопросов «похоже на дубль» в FSM data (как у gjumps):
# без него игнорируемые вопросы копились бы в MemoryStorage бессрочно
_DUP_CANDIDATES_MAX = 20


async def _ask_about_similar(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    category: Category,
    item: dict,
    existing: Media,
    *,
    show_existing: bool = True,
) -> None:
    """Показывает найденный похожий файл (без кнопок), затем отдельным
    ТЕКСТОВЫМ сообщением задаёт вопрос с кнопками — edit_text потом работает
    для любых типов медиа. Сам item (вместе с category_id) прячем в FSM data
    под коротким уникальным ключом — его заберут хендлеры DupCB. Ключ —
    случайный токен, а не счётчик: после сброса data / рестарта бота
    протухшая кнопка не найдёт ключ и попадёт в ветку «Кнопка устарела»,
    а не в чужой (новый) файл.

    show_existing=False — у загружающего нет права просмотра категории
    (drop-box «только загрузка»): существующий файл НЕ показываем и в тексте
    вопроса его не раскрываем, но поток «Сохранить всё равно?» работает.

    Кандидат записывается в dup_candidates только ПОСЛЕ успешной отправки
    вопроса: упавшая отправка не должна оставлять «невидимую» запись без
    кнопок. Безопасно под SimpleEventIsolation — колбэк кнопки не начнёт
    обрабатываться, пока текущий хендлер держит per-user lock."""
    title = html.escape(category.title)
    key = secrets.token_urlsafe(4)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💾 Сохранить всё равно",
                    callback_data=DupCB(action="save", key=key).pack(),
                ),
                InlineKeyboardButton(
                    text="👌 Не нужно",
                    callback_data=DupCB(action="skip", key=key).pack(),
                ),
            ]
        ]
    )
    if show_existing:
        try:
            await send_media(
                bot, chat_id, existing, caption=media_caption(existing, category)
            )
        except TelegramAPIError:
            pass  # похожий файл не показался — вопрос всё равно задаём
        question = (
            f"🤔 Очень похоже на этот файл из «{title}» (выше). "
            "Сохранить твой всё равно?"
        )
    else:
        question = (
            f"🤔 Очень похоже на файл, который уже есть в «{title}». "
            "Сохранить твой всё равно?"
        )
    try:
        await bot.send_message(chat_id, question, reply_markup=kb)
    except TelegramRetryAfter as e:
        # flood-лимит: выдерживаем паузу и повторяем один раз;
        # вторая ошибка честно уходит наружу — её ловят вызывающие
        await asyncio.sleep(e.retry_after)
        await bot.send_message(chat_id, question, reply_markup=kb)
    # вопрос отправлен — только теперь фиксируем кандидата
    data = await state.get_data()
    candidates = data.get("dup_candidates", {})
    candidates[key] = {**item, "category_id": category.id}
    # dict хранит порядок вставки — выкидываем старейших сверх капа
    while len(candidates) > _DUP_CANDIDATES_MAX:
        candidates.pop(next(iter(candidates)))
    await state.update_data(dup_candidates=candidates)


async def _edit_question(
    message: Message | InaccessibleMessage | None, text: str
) -> None:
    """Меняет текст вопроса про похожий дубль (и тем самым убирает кнопки).
    Вопрос — всегда текстовое сообщение, поэтому edit_text безопасен."""
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text)
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------- поток 1: из меню


@router.callback_query(MenuCB.filter(F.action == "upload"))
async def menu_upload(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    config: Config,
    is_admin: bool,
    state: FSMContext,
) -> None:
    current_state = await state.get_state()
    if current_state not in (
        None,
        UploadStates.collecting.state,
        UploadStates.choosing_category.state,
    ):
        # Чужой FSM-диалог (переход по номеру, админка): кнопки UploadPickCB
        # в нём не работают — не рисуем заведомо мёртвую клавиатуру
        await callback.answer("Сначала заверши текущее действие 🙂", show_alert=True)
        return
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
    if current_state == UploadStates.choosing_category.state:
        # Уже висит вопрос «Куда сохранить?» с pending-пачкой: показываем
        # тот же выбор, но с РАБОЧИМИ кнопками (UploadPendingPickCB)
        await _show_screen(
            callback.message,
            "Куда сохранить? 📁",
            categories_pick_kb(
                cats,
                make_cb=lambda c: UploadPendingPickCB(category_id=c.id),
                back_button=_cancel_button(),
            ),
        )
        await callback.answer()
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

    if not pending:
        await clear_state_keep_pending(state)
        await _show_screen(
            callback.message,
            "Хм, файлы потерялись — пришли их ещё раз, пожалуйста 🤔",
            main_menu_kb(is_admin),
        )
        await callback.answer()
        return

    # pending изымаем из data сразу — защита от двойного клика по кнопке
    data.pop("pending", None)
    await state.set_data(data)

    # Похожий файл нельзя показывать без права просмотра категории
    # (drop-box «только загрузка»): can_upload не подразумевает can_view
    show_existing = await access.can_view(session, user, config, category.id)

    # скаляры снимаем заранее: rollback в except протухает ORM-объекты
    category_id = category.id
    user_id = user.id
    title = html.escape(category.title)

    saved = 0
    duplicates = 0
    similar = 0
    failed = 0
    for index, item in enumerate(pending):
        # Ошибка Telegram или БД (flood-лимит, гонка IntegrityError)
        # на одном файле не должна ронять хендлер и терять остаток
        # пачки — pending уже изъят из data
        try:
            status, existing = await _save_one(
                bot, session, config, user, category, item
            )
            if status == "similar" and existing is not None:
                await _ask_about_similar(
                    bot,
                    item["src_chat_id"],
                    state,
                    category,
                    item,
                    existing,
                    show_existing=show_existing,
                )
        except (TelegramAPIError, SQLAlchemyError):
            # rollback оставляет сессию рабочей, но протухает загруженные
            # объекты — перечитываем их перед следующей итерацией
            await session.rollback()
            failed += 1
            category = await repo.get_category(session, category_id)
            user = await repo.get_user(session, user_id)
            if category is None or user is None:
                # категорию/юзера успели удалить — остаток пачки не спасти
                failed += len(pending) - index - 1
                break
            continue
        if status == "created":
            saved += 1
        elif status == "duplicate":
            duplicates += 1
        else:
            similar += 1

    # pending уже потреблён (сохранён), dup_candidates с живыми кнопками
    # «Сохранить всё равно» переживают сброс
    await clear_state_keep_pending(state)

    text = f"✅ Сохранено {saved} шт. в «{title}»"
    if duplicates:
        text += f", дубликатов: {duplicates}"
    if similar:
        text += f", похожих на дубли: {similar} — спросил выше 👆"
    if failed:
        text += f", не получилось отправить: {failed} — пришли их ещё раз"
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
    data = await state.get_data()
    new_data = {"category_id": category.id, "saved_count": 0}
    # не затираем отложенные вопросы «похоже на дубль» — их кнопки ещё живы
    if "dup_candidates" in data:
        new_data["dup_candidates"] = data["dup_candidates"]
    await state.set_data(new_data)
    title = html.escape(category.title)
    await _show_screen(
        callback.message,
        f"Кидай фото/видео/гифки/кружки/войсы — всё сохраню в «{title}». "
        "Альбомы тоже можно. Когда закончишь — жми Готово ✅",
        _done_kb(),
    )
    await callback.answer()


# -------- вопрос «похоже на дубль»: работает в любом состоянии,
# регистрируется ДО fallback-хендлеров выбора категории


@router.callback_query(DupCB.filter(F.action == "save"))
async def dup_save(
    callback: CallbackQuery,
    callback_data: DupCB,
    session: AsyncSession,
    user: User,
    config: Config,
    state: FSMContext,
    bot: Bot,
) -> None:
    """«Сохранить всё равно»: достаём отложенный item из FSM data и сохраняем
    без повторной проверки похожести (точный дедуп остаётся)."""
    data = await state.get_data()
    candidates = data.get("dup_candidates", {})
    item = candidates.pop(callback_data.key, None)
    if item is None:
        await _edit_question(callback.message, "⌛️ Кнопка устарела")
        await callback.answer(
            "Кнопка устарела 🕰 Пришли файл ещё раз", show_alert=True
        )
        return
    await state.update_data(dup_candidates=candidates)

    # права могли отозвать, категорию — удалить/заархивировать
    category = await repo.get_category(session, item["category_id"])
    if category is None:
        await _edit_question(
            callback.message, "🤔 Категория куда-то делась — не сохранил"
        )
        await callback.answer("Категория куда-то делась 🤔", show_alert=True)
        return
    if not await access.can_upload(session, user, config, category.id):
        await _edit_question(
            callback.message, "🔒 Сюда загружать больше нельзя — не сохранил"
        )
        await callback.answer(
            "Сюда загружать нельзя — попроси доступ у админа 🔒", show_alert=True
        )
        return

    status, _ = await _save_one(
        bot, session, config, user, category, item, skip_similar_check=True
    )
    title = html.escape(category.title)
    # текст и в callback.answer: если вопрос старше 48ч (InaccessibleMessage)
    # или edit_text не прошёл, юзер всё равно увидит результат в тосте
    if status == "created":
        await _edit_question(callback.message, f"✅ Сохранил в «{title}»")
        await callback.answer(f"✅ Сохранил в «{category.title}»")
    else:
        await _edit_question(
            callback.message, f"⚠️ Оказалось, байт-в-байт уже есть в «{title}»"
        )
        await callback.answer(f"⚠️ Такой файл уже есть в «{category.title}»")


@router.callback_query(DupCB.filter(F.action == "skip"))
async def dup_skip(
    callback: CallbackQuery,
    callback_data: DupCB,
    state: FSMContext,
) -> None:
    """«Не нужно»: выкидываем отложенный item."""
    data = await state.get_data()
    candidates = data.get("dup_candidates", {})
    if callback_data.key in candidates:
        candidates.pop(callback_data.key)
        await state.update_data(dup_candidates=candidates)
    await _edit_question(callback.message, "👌 Ок, не сохранил")
    await callback.answer("👌 Ок, не сохранил")


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
        # state.clear() стёр бы отложенные вопросы «похоже на дубль»
        await clear_state_keep_pending(state)
        await message.answer(
            "🔒 Эта категория больше недоступна — загрузку остановил.",
            reply_markup=main_menu_kb(is_admin),
        )
        return

    phash = await compute_phash_from_message(bot, message)
    item = _media_item(message, extracted, phash)
    # снимаем до _save_one: rollback при гонке IntegrityError в add_media
    # протухает ORM-объекты, и category.title после него недоступен
    title = html.escape(category.title)
    # без права просмотра категории существующий похожий файл не показываем
    show_existing = await access.can_view(session, user, config, category.id)
    status, existing = await _save_one(bot, session, config, user, category, item)
    if status == "similar" and existing is not None:
        # похожий файл не сохраняем и saved_count не увеличиваем — спрашиваем
        try:
            await _ask_about_similar(
                bot,
                message.chat.id,
                state,
                category,
                item,
                existing,
                show_existing=show_existing,
            )
        except TelegramAPIError:
            await message.reply(
                "Не смог задать вопрос про похожий файл — пришли его ещё раз 😕"
            )
        return
    if status == "duplicate":
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


@router.message(StateFilter(UploadStates.collecting), F.chat.type == "private")
async def collect_unsupported_hint(message: Message) -> None:
    """Не-медиа и не-текст в режиме collecting (документ, стикер и т.п.)."""
    await message.answer(
        "Такой тип пока не умею — пришли фото, видео, гифку, "
        "кружок, войс или аудио",
        reply_markup=_done_kb(),
    )


# -------- завершение / отмена


@router.callback_query(UploadDoneCB.filter())
async def upload_done(
    callback: CallbackQuery,
    is_admin: bool,
    state: FSMContext,
) -> None:
    current_state = await state.get_state()
    if current_state not in (
        None,
        UploadStates.collecting.state,
        UploadStates.choosing_category.state,
    ):
        # Чужой FSM-диалог (переход по номеру, админка): протухшая кнопка
        # «Готово»/«Отмена» не должна его убивать — как pick_pending_stale
        await callback.answer("Сначала заверши текущее действие 🙂", show_alert=True)
        return

    data = await state.get_data()
    if current_state == UploadStates.choosing_category.state:
        # Отмена выбора категории (поток 2): pending сознательно выбрасываем,
        # поэтому НЕ clear_state_keep_pending — он бы восстановил пачку
        if data.get("dup_candidates"):
            # state.clear() стёр бы отложенные вопросы «похоже на дубль»
            await state.set_state(None)
            await state.set_data({"dup_candidates": data["dup_candidates"]})
        else:
            await state.clear()
        text = "Окей, отменил 👌"
    elif current_state == UploadStates.collecting.state:
        await clear_state_keep_pending(state)
        saved_count = data.get("saved_count", 0)
        text = f"Готово! Сохранено {saved_count} шт. 📦"
        if saved_count:
            text += (
                "\n💡 Подпись помогает найти мем через 🔍 Поиск — "
                "кнопка ✏️ под ним в ленте"
            )
    else:
        # state=None: протухшая кнопка под старым сообщением — ничего
        # не чистим (dup_candidates живы) и не пугаем «Сохранено 0 шт.»
        text = "Эта загрузка уже завершена 👌 Всё сохранённое лежит в Схроне"

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
    bot: Bot,
) -> None:
    """Прилетело ещё медиа (альбом), пока висит вопрос «Куда сохранить?» —
    просто дописываем в pending без нового вопроса."""
    extracted = extract_media(message)
    if extracted is None:
        return
    phash = await compute_phash_from_message(bot, message)
    data = await state.get_data()
    pending = data.get("pending", [])
    pending.append(_media_item(message, extracted, phash))
    await state.update_data(pending=pending)


@router.message(
    StateFilter(UploadStates.choosing_category), F.chat.type == "private", F.text
)
async def choosing_category_text_hint(message: Message) -> None:
    """Текст в ответ на «Куда сохранить?» — категорию словами не выбрать."""
    await message.answer(
        "Категорию нужно выбрать кнопкой под вопросом 👆 (или жми ↩️ Отмена)"
    )


@router.message(StateFilter(None), F.chat.type == "private", MEDIA_FILTER)
async def unsolicited_media(
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

    phash = await compute_phash_from_message(bot, message)
    await state.set_state(UploadStates.choosing_category)
    data = await state.get_data()
    new_data = {"pending": [_media_item(message, extracted, phash)]}
    # не затираем отложенные вопросы «похоже на дубль» — их кнопки ещё живы
    if "dup_candidates" in data:
        new_data["dup_candidates"] = data["dup_candidates"]
    await state.set_data(new_data)
    await message.answer(
        "Куда сохранить? 📁",
        reply_markup=categories_pick_kb(
            uploadable,
            make_cb=lambda c: UploadPendingPickCB(category_id=c.id),
            back_button=_cancel_button(),
        ),
    )


# Финальный catch-all в личке: регистрируется САМЫМ ПОСЛЕДНИМ (роутер upload
# подключается последним, этот хендлер — последний в нём). Известные команды
# (/start, /menu, /help, /admin, …) матчатся роутерами выше, медиа — хендлером
# unsolicited_media (StateFilter(None) + MEDIA_FILTER) выше по файлу. Сюда
# падают «привет» новичка, /random из групповой памяти, документы и стикеры
# вне состояний — раньше они оставались вообще без ответа.
@router.message(StateFilter(None), F.chat.type == "private")
async def private_fallback(message: Message, is_admin: bool) -> None:
    await message.answer(
        "Я храню мемы 📦 Пришли фото/видео — сохраню. Или жми /menu",
        reply_markup=main_menu_kb(is_admin),
    )
