"""Тесты групповой фичи: чаты, права групп (repo) и доступы (access)."""

import sqlite3

from conftest import make_category

from sqlalchemy import select

from skhron.db import repo
from skhron.db.base import create_engine_and_sessionmaker, init_db
from skhron.db.models import GroupPermission
from skhron.services import access

CHAT_ID = -100500

# ---------------------------------------------------------------- chats


async def test_upsert_chat_creates(session):
    chat = await repo.upsert_chat(session, CHAT_ID, "Чат с парнями", "supergroup")
    assert chat.id == CHAT_ID
    assert chat.title == "Чат с парнями"
    assert chat.type == "supergroup"
    assert chat.is_active is True


async def test_upsert_chat_updates_title(session):
    await repo.upsert_chat(session, CHAT_ID, "Старое название", "group")
    updated = await repo.upsert_chat(session, CHAT_ID, "Новое название", "supergroup")

    assert updated.title == "Новое название"
    assert updated.type == "supergroup"
    # запись одна — это апдейт, а не дубль
    chats = await repo.list_chats(session)
    assert len(chats) == 1


async def test_upsert_chat_reactivates_inactive(session):
    """Бота выгнали (is_active=False) и вернули — upsert оживляет запись."""
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    await repo.set_chat_active(session, CHAT_ID, False)
    stored = await repo.get_chat(session, CHAT_ID)
    assert stored.is_active is False

    revived = await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    assert revived.is_active is True
    assert (await repo.get_chat(session, CHAT_ID)).is_active is True


async def test_list_chats_active_only(session):
    await repo.upsert_chat(session, -1, "Активный", "group")
    await repo.upsert_chat(session, -2, "Покинутый", "group")
    await repo.set_chat_active(session, -2, False)

    all_chats = await repo.list_chats(session)
    assert {c.id for c in all_chats} == {-1, -2}

    active = await repo.list_chats(session, active_only=True)
    assert {c.id for c in active} == {-1}


# ------------------------------------------------------- group permissions


async def test_set_and_revoke_group_permission(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    category = await make_category(session, "мемы")

    perm = await repo.set_group_permission(
        session, CHAT_ID, category.id, granted_by=1
    )
    assert perm.chat_id == CHAT_ID
    assert perm.category_id == category.id
    assert perm.granted_by == 1
    assert await repo.get_group_permission(session, CHAT_ID, category.id) is not None

    await repo.revoke_group_permission(session, CHAT_ID, category.id)
    assert await repo.get_group_permission(session, CHAT_ID, category.id) is None


async def test_set_group_permission_idempotent(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    category = await make_category(session, "мемы")

    first = await repo.set_group_permission(session, CHAT_ID, category.id, granted_by=1)
    second = await repo.set_group_permission(
        session, CHAT_ID, category.id, granted_by=2
    )
    assert (second.chat_id, second.category_id) == (first.chat_id, first.category_id)

    rows = list(
        (
            await session.execute(
                select(GroupPermission).where(GroupPermission.chat_id == CHAT_ID)
            )
        ).scalars()
    )
    assert len(rows) == 1


async def test_list_chat_categories_sorted_and_archived(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    cat_b = await make_category(session, "собачки")
    cat_a = await make_category(session, "котики")
    cat_archived = await make_category(session, "архивная", archived=True)
    cat_foreign = await make_category(session, "чужая")  # без права — не видна

    for cat in (cat_b, cat_a, cat_archived):
        await repo.set_group_permission(session, CHAT_ID, cat.id, granted_by=1)

    cats = await repo.list_chat_categories(session, CHAT_ID)
    # архивная скрыта, сортировка по названию
    assert [c.id for c in cats] == [cat_a.id, cat_b.id]

    with_archived = await repo.list_chat_categories(
        session, CHAT_ID, include_archived=True
    )
    assert [c.id for c in with_archived] == [cat_archived.id, cat_a.id, cat_b.id]
    assert cat_foreign.id not in {c.id for c in with_archived}


# -------------------------------------------------------------- migration


async def test_migrate_chat_moves_chat_and_permissions(session):
    """Новый id ещё не зарегистрирован: Chat и права переезжают целиком."""
    old_id, new_id = -300, -100300
    await repo.upsert_chat(session, old_id, "Группа", "group")
    cat_a = await make_category(session, "котики")
    cat_b = await make_category(session, "собачки")
    await repo.set_group_permission(session, old_id, cat_a.id, granted_by=1)
    await repo.set_group_permission(session, old_id, cat_b.id, granted_by=2)

    await repo.migrate_chat(session, old_id, new_id)

    # старой записи нет
    assert await repo.get_chat(session, old_id) is None
    # новая — супергруппа, активна, с тем же названием
    new_chat = await repo.get_chat(session, new_id)
    assert new_chat is not None
    assert new_chat.type == "supergroup"
    assert new_chat.is_active is True
    assert new_chat.title == "Группа"
    # права переехали
    perm_a = await repo.get_group_permission(session, new_id, cat_a.id)
    perm_b = await repo.get_group_permission(session, new_id, cat_b.id)
    assert perm_a is not None and perm_a.granted_by == 1
    assert perm_b is not None and perm_b.granted_by == 2
    # на старом id прав не осталось
    old_rows = list(
        (
            await session.execute(
                select(GroupPermission).where(GroupPermission.chat_id == old_id)
            )
        ).scalars()
    )
    assert old_rows == []


async def test_migrate_chat_merges_into_existing(session):
    """Новый id уже зарегистрирован лениво: права сливаются без дублей,
    старая запись удаляется."""
    old_id, new_id = -301, -100301
    await repo.upsert_chat(session, old_id, "Группа", "group")
    # супергруппа уже создана лениво (например, через /random после миграции)
    await repo.upsert_chat(session, new_id, "Группа", "supergroup")
    cat_shared = await make_category(session, "общая")
    cat_old_only = await make_category(session, "только старая")
    await repo.set_group_permission(session, old_id, cat_shared.id, granted_by=1)
    await repo.set_group_permission(session, old_id, cat_old_only.id, granted_by=1)
    await repo.set_group_permission(session, new_id, cat_shared.id, granted_by=2)

    await repo.migrate_chat(session, old_id, new_id)

    # старая запись удалена, новая жива и активна
    assert await repo.get_chat(session, old_id) is None
    new_chat = await repo.get_chat(session, new_id)
    assert new_chat is not None
    assert new_chat.type == "supergroup"
    assert new_chat.is_active is True
    # права слились: общая категория одна, «только старая» доехала
    rows = list(
        (
            await session.execute(
                select(GroupPermission).where(GroupPermission.chat_id == new_id)
            )
        ).scalars()
    )
    assert {r.category_id for r in rows} == {cat_shared.id, cat_old_only.id}
    assert len(rows) == 2  # без дублей
    # уже существовавшее право не перезаписано
    shared = await repo.get_group_permission(session, new_id, cat_shared.id)
    assert shared.granted_by == 2


async def test_migrate_chat_unknown_old_id_is_noop(session):
    """migrate_chat для незнакомого old_id — тихий no-op."""
    await repo.migrate_chat(session, -99999, -100999)
    assert await repo.get_chat(session, -99999) is None
    assert await repo.get_chat(session, -100999) is None
    assert await repo.list_chats(session) == []


# --------------------------------------------- set_group_permission result


async def test_set_group_permission_returns_none_for_deleted_category(session):
    """Категорию жёстко удалили — выдать право нельзя, возвращается None."""
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    category = await make_category(session, "обречённая")
    doomed_id = category.id
    await repo.delete_category(session, doomed_id)

    perm = await repo.set_group_permission(session, CHAT_ID, doomed_id, granted_by=1)

    assert perm is None
    # скрытой записи в БД не осталось
    assert await repo.get_group_permission(session, CHAT_ID, doomed_id) is None


async def test_set_group_permission_returns_object_on_success(session):
    """Нормальная выдача возвращает живой GroupPermission, а не None."""
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    category = await make_category(session, "мемы")

    perm = await repo.set_group_permission(session, CHAT_ID, category.id, granted_by=7)

    assert isinstance(perm, GroupPermission)
    assert (perm.chat_id, perm.category_id, perm.granted_by) == (
        CHAT_ID,
        category.id,
        7,
    )


# ------------------------------------------------------------ group access


async def test_group_can_view(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    cat_allowed = await make_category(session, "разрешённая")
    cat_denied = await make_category(session, "неразрешённая")
    cat_archived = await make_category(session, "архивная", archived=True)

    await repo.set_group_permission(session, CHAT_ID, cat_allowed.id, granted_by=1)
    await repo.set_group_permission(session, CHAT_ID, cat_archived.id, granted_by=1)

    assert await access.group_can_view(session, CHAT_ID, cat_allowed.id) is True
    assert await access.group_can_view(session, CHAT_ID, cat_denied.id) is False
    # право есть, но категория архивная — скрыта и для группы
    assert await access.group_can_view(session, CHAT_ID, cat_archived.id) is False
    # несуществующая категория
    assert await access.group_can_view(session, CHAT_ID, 99999) is False


async def test_group_viewable_category_ids(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    cat_a = await make_category(session, "котики")
    cat_b = await make_category(session, "собачки")
    cat_archived = await make_category(session, "архивная", archived=True)
    await make_category(session, "чужая")

    for cat in (cat_a, cat_b, cat_archived):
        await repo.set_group_permission(session, CHAT_ID, cat.id, granted_by=1)

    ids = await access.group_viewable_category_ids(session, CHAT_ID)
    assert set(ids) == {cat_a.id, cat_b.id}

    # у чата без прав — пусто
    assert await access.group_viewable_category_ids(session, -999) == []


# ---------------------------------------------------------------- cascade


async def test_delete_chat_cascades_group_permissions(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    cat_a = await make_category(session, "котики")
    cat_b = await make_category(session, "собачки")
    await repo.set_group_permission(session, CHAT_ID, cat_a.id, granted_by=1)
    await repo.set_group_permission(session, CHAT_ID, cat_b.id, granted_by=1)

    await repo.delete_chat(session, CHAT_ID)

    assert await repo.get_chat(session, CHAT_ID) is None
    rows = list(
        (
            await session.execute(
                select(GroupPermission).where(GroupPermission.chat_id == CHAT_ID)
            )
        ).scalars()
    )
    assert rows == []


# ------------------------------------------------------------- daily meme


async def test_set_chat_daily_enables_and_disables(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")

    # включение: минуты сохраняются, отметка последней отправки чиста
    await repo.set_chat_daily(session, CHAT_ID, 540)
    chat = await repo.get_chat(session, CHAT_ID)
    assert chat.daily_minutes == 540
    assert chat.daily_last_sent is None

    # выключение: None вместо минут
    await repo.set_chat_daily(session, CHAT_ID, None)
    chat = await repo.get_chat(session, CHAT_ID)
    assert chat.daily_minutes is None
    assert chat.daily_last_sent is None


async def test_set_chat_daily_keeps_last_sent(session):
    """Отметка «сегодня уже постили» переживает смену времени: повторный
    тап по пресету или перенос времени не шлёт второй мем за день."""
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    await repo.set_chat_daily(session, CHAT_ID, 540)
    await repo.set_chat_daily_sent(session, CHAT_ID, "2026-07-10")

    await repo.set_chat_daily(session, CHAT_ID, 720)  # смена времени
    chat = await repo.get_chat(session, CHAT_ID)
    assert chat.daily_minutes == 720
    assert chat.daily_last_sent == "2026-07-10"

    await repo.set_chat_daily(session, CHAT_ID, 720)  # no-op повтор
    chat = await repo.get_chat(session, CHAT_ID)
    assert chat.daily_last_sent == "2026-07-10"


async def test_migrate_chat_carries_daily_schedule(session):
    """Миграция в супергруппу переносит расписание «мема дня»."""
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    await repo.set_chat_daily(session, CHAT_ID, 540)
    await repo.set_chat_daily_sent(session, CHAT_ID, "2026-07-10")

    new_id = CHAT_ID - 1_000_000
    await repo.migrate_chat(session, CHAT_ID, new_id)

    migrated = await repo.get_chat(session, new_id)
    assert migrated is not None
    assert migrated.daily_minutes == 540
    assert migrated.daily_last_sent == "2026-07-10"
    assert new_id in {c.id for c in await repo.list_daily_chats(session)}


async def test_set_chat_daily_unknown_chat_is_noop(session):
    """Незнакомый chat_id не роняет и не создаёт запись."""
    await repo.set_chat_daily(session, -424242, 540)
    assert await repo.get_chat(session, -424242) is None


async def test_set_chat_daily_sent(session):
    await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
    await repo.set_chat_daily(session, CHAT_ID, 540)

    await repo.set_chat_daily_sent(session, CHAT_ID, "2026-07-10")

    chat = await repo.get_chat(session, CHAT_ID)
    assert chat.daily_last_sent == "2026-07-10"
    assert chat.daily_minutes == 540  # расписание не тронуто

    # незнакомый чат — тихий no-op
    await repo.set_chat_daily_sent(session, -424242, "2026-07-10")
    assert await repo.get_chat(session, -424242) is None


async def test_list_daily_chats_only_active_with_schedule(session):
    scheduled = await repo.upsert_chat(session, -1, "С расписанием", "group")
    await repo.set_chat_daily(session, scheduled.id, 540)

    midnight = await repo.upsert_chat(session, -2, "Полуночный", "group")
    await repo.set_chat_daily(session, midnight.id, 0)  # 0 минут — валидное время

    plain = await repo.upsert_chat(session, -3, "Без расписания", "group")

    kicked = await repo.upsert_chat(session, -4, "Покинутый", "group")
    await repo.set_chat_daily(session, kicked.id, 600)
    await repo.set_chat_active(session, kicked.id, False)

    switched_off = await repo.upsert_chat(session, -5, "Выключенный", "group")
    await repo.set_chat_daily(session, switched_off.id, 600)
    await repo.set_chat_daily(session, switched_off.id, None)

    daily = await repo.list_daily_chats(session)
    assert {c.id for c in daily} == {scheduled.id, midnight.id}
    assert plain.id not in {c.id for c in daily}


# ------------------------------------------------- light schema migration

# Схема chats, которую create_all генерировал ДО колонок «мема дня»
_OLD_CHATS_DDL = """
CREATE TABLE chats (
    id BIGINT NOT NULL,
    title VARCHAR(256) NOT NULL,
    type VARCHAR(16) NOT NULL,
    is_active BOOLEAN NOT NULL,
    added_at DATETIME NOT NULL,
    PRIMARY KEY (id)
)
"""


async def test_init_db_adds_daily_columns_to_existing_chats(tmp_path):
    """Лёгкая миграция: существующая таблица chats без daily_* получает
    обе колонки, старые данные целы, новые поля — NULL."""
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    try:
        con.execute(_OLD_CHATS_DDL)
        con.execute(
            "INSERT INTO chats VALUES (?,?,?,?,?)",
            (-100500, "Старый чат", "group", 1, "2024-01-01 00:00:00"),
        )
        con.commit()
        columns = {row[1] for row in con.execute("PRAGMA table_info(chats)")}
        assert "daily_minutes" not in columns
        assert "daily_last_sent" not in columns
    finally:
        con.close()

    engine, session_factory = create_engine_and_sessionmaker(db_path.as_posix())
    try:
        await init_db(engine)

        # ORM видит мигрированную запись и умеет включать «мем дня»
        async with session_factory() as session:
            chat = await repo.get_chat(session, -100500)
            assert chat is not None
            assert chat.title == "Старый чат"
            assert chat.daily_minutes is None
            assert chat.daily_last_sent is None

            await repo.set_chat_daily(session, -100500, 540)
            await repo.set_chat_daily_sent(session, -100500, "2026-07-10")
            daily = await repo.list_daily_chats(session)
            assert [c.id for c in daily] == [-100500]
    finally:
        await engine.dispose()

    con = sqlite3.connect(db_path)
    try:
        info = list(con.execute("PRAGMA table_info(chats)"))
        columns = {row[1] for row in info}
        assert {"daily_minutes", "daily_last_sent"} <= columns
        # новые колонки — nullable (ALTER без NOT NULL/DEFAULT)
        notnull = {row[1]: row[3] for row in info}
        assert notnull["daily_minutes"] == 0
        assert notnull["daily_last_sent"] == 0
        # старые данные целы и дополнены новыми значениями
        row = con.execute(
            "SELECT id, title, type, is_active, added_at,"
            " daily_minutes, daily_last_sent FROM chats"
        ).fetchone()
        assert row == (
            -100500, "Старый чат", "group", 1, "2024-01-01 00:00:00",
            540, "2026-07-10",
        )
    finally:
        con.close()


async def test_init_db_second_run_keeps_daily_columns(tmp_path):
    """Повторный init_db не дублирует колонки и не трогает данные."""
    db_path = tmp_path / "twice.db"
    engine, session_factory = create_engine_and_sessionmaker(db_path.as_posix())
    try:
        await init_db(engine)
        async with session_factory() as session:
            await repo.upsert_chat(session, CHAT_ID, "Чат", "group")
            await repo.set_chat_daily(session, CHAT_ID, 540)

        await init_db(engine)  # второй прогон — no-op для chats

        async with session_factory() as session:
            chat = await repo.get_chat(session, CHAT_ID)
            assert chat.daily_minutes == 540
    finally:
        await engine.dispose()

    con = sqlite3.connect(db_path)
    try:
        names = [row[1] for row in con.execute("PRAGMA table_info(chats)")]
        assert names.count("daily_minutes") == 1
        assert names.count("daily_last_sent") == 1
    finally:
        con.close()
