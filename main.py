import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation

from skhron.config import load_config
from skhron.db.base import create_engine_and_sessionmaker, init_db
from skhron.handlers import setup_routers
from skhron.middlewares.db import DbSessionMiddleware
from skhron.middlewares.user import UserMiddleware
from skhron.utils.commands import setup_bot_commands


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config()

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

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await setup_bot_commands(bot, config)
        await dp.start_polling(bot)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
