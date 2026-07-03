"""Общие фикстуры и фабрики тестовых данных.

БД — SQLite in-memory через тот же create_engine_and_sessionmaker, что и прод,
поэтому PRAGMA foreign_keys=ON действует и в тестах.
"""

import itertools
import sys
from pathlib import Path

# Гарантируем импортируемость пакета skhron независимо от способа запуска
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db.base import create_engine_and_sessionmaker, init_db
from skhron.db.models import Category, Media, User

# ---------------------------------------------------------------- fixtures


@pytest.fixture
async def session():
    engine, session_factory = create_engine_and_sessionmaker(":memory:")
    await init_db(engine)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def config() -> Config:
    return Config(_env_file=None, bot_token="42:TEST", admin_ids=[1])


# ---------------------------------------------------------------- factories

_unique_counter = itertools.count(1)


async def make_user(
    session: AsyncSession,
    user_id: int,
    admin: bool = False,
    username: str | None = None,
) -> User:
    user = User(
        id=user_id,
        username=username if username is not None else f"user{user_id}",
        full_name=f"Юзер {user_id}",
        is_admin=admin,
    )
    session.add(user)
    await session.commit()
    return user


async def make_category(
    session: AsyncSession,
    title: str = "тест",
    archived: bool = False,
) -> Category:
    category = Category(title=title, is_archived=archived)
    session.add(category)
    await session.commit()
    return category


async def make_media(
    session: AsyncSession,
    category_id: int,
    uploaded_by: int | None = None,
    media_type: str = "photo",
    caption: str | None = None,
    file_unique_id: str | None = None,
) -> Media:
    if file_unique_id is None:
        file_unique_id = f"uniq-{next(_unique_counter)}"
    media = Media(
        category_id=category_id,
        file_id=f"file-{file_unique_id}",
        file_unique_id=file_unique_id,
        media_type=media_type,
        caption=caption,
        uploaded_by=uploaded_by,
    )
    session.add(media)
    await session.commit()
    return media
