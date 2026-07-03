"""Тесты групповой фичи: чаты, права групп (repo) и доступы (access)."""

from conftest import make_category

from sqlalchemy import select

from skhron.db import repo
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
