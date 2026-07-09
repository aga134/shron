"""Общий сброс FSM-состояния, не теряющий данные с живыми кнопками."""

from aiogram.fsm.context import FSMContext


async def clear_state_keep_pending(state: FSMContext) -> None:
    """Сбрасывает диалоговое состояние, сохраняя то, на что в чате ещё
    смотрят живые кнопки:

    - dup_candidates — вопросы «похоже на дубль — сохранить всё равно?»;
    - pending — несохранённая пачка файлов (вопрос «Куда сохранить?»);
      вместе с ней восстанавливается состояние choosing_category,
      чтобы кнопки категорий под вопросом продолжали работать.

    Использовать вместо state.clear() во всех «навигационных» выходах
    (меню, /start, админка, завершение перехода по номеру).
    """
    data = await state.get_data()
    keep = {k: data[k] for k in ("pending", "dup_candidates") if data.get(k)}
    if keep.get("pending"):
        # ленивый импорт: upload сам импортирует utils, цикл не нужен
        from skhron.handlers.upload import UploadStates

        await state.set_state(UploadStates.choosing_category)
        await state.set_data(keep)
    elif keep:
        await state.set_state(None)
        await state.set_data(keep)
    else:
        await state.clear()
