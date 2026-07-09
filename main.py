import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import ErrorEvent

from skhron.config import load_config
from skhron.db.base import create_engine_and_sessionmaker, init_db
from skhron.db.migrate import pre_migrate
from skhron.handlers import setup_routers
from skhron.handlers.admin.stats_backup import shutdown_rehash
from skhron.middlewares.db import DbSessionMiddleware
from skhron.middlewares.user import UserMiddleware
from skhron.services.scheduler import start_scheduler, stop_scheduler
from skhron.utils.commands import setup_bot_commands

logger = logging.getLogger("skhron")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()

    pre_migrate(config.database_path)
    engine, session_factory = create_engine_and_sessionmaker(config.database_path)
    await init_db(engine)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # events_isolation сериализует апдейты одного юзера: без него сообщения
    # альбома обрабатываются параллельно и затирают FSM-данные друг друга
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp["config"] = config
    # для фоновых задач, живущих дольше одного апдейта (/rehash)
    dp["session_factory"] = session_factory

    dp.update.outer_middleware(DbSessionMiddleware(session_factory))
    dp.update.outer_middleware(UserMiddleware())

    dp.include_router(setup_routers())
    # фоновые задачи гасим до закрытия сессии бота и engine.dispose()
    dp.shutdown.register(shutdown_rehash)
    dp.shutdown.register(stop_scheduler)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> None:
        """Последний рубеж: логируем и гасим спиннер, чтобы юзер не ждал."""
        logger.exception(
            "Необработанная ошибка в апдейте %s",
            event.update.update_id,
            exc_info=event.exception,
        )
        callback = event.update.callback_query
        if callback is not None:
            try:
                await callback.answer(
                    "Что-то пошло не так 😵 Попробуй ещё раз", show_alert=True
                )
            except Exception:  # noqa: BLE001 — уже внутри обработчика ошибок
                pass

    try:
        # без drop_pending_updates: присланное за время даунтайма не выбрасываем
        await bot.delete_webhook()
        me = await bot.me()
        if not me.supports_inline_queries:
            # без /setinline у BotFather кнопку «Поиск» показывать нельзя —
            # Telegram отклонит всю клавиатуру главного меню
            from skhron.keyboards import common

            common.INLINE_ENABLED = False
            logger.warning(
                "Инлайн-режим у BotFather выключен — кнопка «🔍 Поиск» скрыта. "
                "Включи через /setinline и перезапусти бота."
            )
        async with session_factory() as session:
            await setup_bot_commands(bot, config, session)
        await start_scheduler(bot, session_factory)
        await dp.start_polling(bot)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
