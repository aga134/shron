from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from skhron.db.models import Base


def create_engine_and_sessionmaker(database_path: str):
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


# create_all не добавляет колонки в существующие таблицы —
# недостающие добиваем вручную: {таблица: {колонка: DDL-тип}}
_LIGHT_MIGRATIONS = {
    "media": {"phash": "VARCHAR(16)"},
    "chats": {
        "daily_minutes": "INTEGER",
        "daily_last_sent": "VARCHAR(10)",
    },
}


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table, columns in _LIGHT_MIGRATIONS.items():
            result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
            existing = {row[1] for row in result.fetchall()}
            for column, ddl in columns.items():
                if column not in existing:
                    await conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                    )
