"""Тесты избранного из групп (skhron.db.repo: top_favorited,
get_favorite_item без фильтра личных прав, каскады)."""

from datetime import datetime

from conftest import make_category, make_media, make_user

from sqlalchemy import select

from skhron.db import repo
from skhron.db.models import Favorite

# ---------------------------------------------------------------- helpers


async def _fav_at(session, user_id: int, media_id: int, when: datetime) -> None:
    """Звезда с детерминированным created_at — для проверок порядка ленты."""
    added = await repo.toggle_favorite(session, user_id, media_id)
    assert added
    fav = await session.get(Favorite, (user_id, media_id))
    fav.created_at = when
    await session.commit()


# --------------------------------------------------------- favorites feed


async def test_get_favorite_item_orders_by_star_time_desc(session):
    """Порядок ленты — по времени звезды (последняя добавленная первой),
    а не по времени загрузки медиа."""
    user = await make_user(session, 100)
    category = await make_category(session)
    m1 = await make_media(session, category.id, uploaded_by=user.id)
    m2 = await make_media(session, category.id, uploaded_by=user.id)
    m3 = await make_media(session, category.id, uploaded_by=user.id)

    # звёздим не в порядке загрузки: m2 → m3 → m1
    await _fav_at(session, user.id, m2.id, datetime(2026, 1, 1))
    await _fav_at(session, user.id, m3.id, datetime(2026, 1, 2))
    await _fav_at(session, user.id, m1.id, datetime(2026, 1, 3))

    expected = [m1.id, m3.id, m2.id]
    for offset, media_id in enumerate(expected):
        item, total = await repo.get_favorite_item(session, user.id, offset)
        assert total == 3
        assert item.id == media_id


async def test_get_favorite_item_hides_deleted(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    kept = await make_media(session, category.id, uploaded_by=user.id)
    gone = await make_media(session, category.id, uploaded_by=user.id)
    await repo.toggle_favorite(session, user.id, kept.id)
    await repo.toggle_favorite(session, user.id, gone.id)

    await repo.soft_delete_media(session, gone.id)

    item, total = await repo.get_favorite_item(session, user.id, 0)
    assert total == 1
    assert item.id == kept.id


async def test_get_favorite_item_huge_offset_is_clamped(session):
    """Поддельный offset за пределами int64 не роняет SQLite (OverflowError),
    а честно возвращает (None, total)."""
    user = await make_user(session, 100)
    category = await make_category(session)
    newest = await make_media(session, category.id, uploaded_by=user.id)
    await repo.toggle_favorite(session, user.id, newest.id)

    item, total = await repo.get_favorite_item(session, user.id, 10**20)
    assert item is None
    assert total == 1

    # отрицательный offset клампится к нулю — последняя добавленная звезда
    item, total = await repo.get_favorite_item(session, user.id, -(10**20))
    assert item is not None
    assert item.id == newest.id
    assert total == 1


async def test_get_favorite_item_ignores_personal_permissions(session):
    """Лента избранного сознательно БЕЗ фильтра личных прав: юзер легально
    видел мем в группе, когда жал ⭐️, — «чужая» категория отдаётся."""
    user = await make_user(session, 100)
    cat_granted = await make_category(session, "доступная")
    cat_foreign = await make_category(session, "чужая")
    # личное право есть только на одну категорию
    perm = await repo.set_permission(
        session, user.id, cat_granted.id, can_view=True, granted_by=1
    )
    assert perm is not None

    media_granted = await make_media(session, cat_granted.id, uploaded_by=1)
    media_foreign = await make_media(session, cat_foreign.id, uploaded_by=1)
    await _fav_at(session, user.id, media_granted.id, datetime(2026, 1, 1))
    await _fav_at(session, user.id, media_foreign.id, datetime(2026, 1, 2))

    item, total = await repo.get_favorite_item(session, user.id, 0)
    assert total == 2  # обе категории, права не фильтруют
    assert item.id == media_foreign.id
    item, _ = await repo.get_favorite_item(session, user.id, 1)
    assert item.id == media_granted.id


async def test_get_favorite_item_hides_archived_categories(session):
    """Архив прячет контент ото всех — и из ленты избранного тоже."""
    user = await make_user(session, 100)
    cat_live = await make_category(session, "живая")
    cat_archived = await make_category(session, "архивная")
    media_live = await make_media(session, cat_live.id, uploaded_by=1)
    media_archived = await make_media(session, cat_archived.id, uploaded_by=1)
    await repo.toggle_favorite(session, user.id, media_live.id)
    await repo.toggle_favorite(session, user.id, media_archived.id)

    await repo.set_category_archived(session, cat_archived.id, True)

    item, total = await repo.get_favorite_item(session, user.id, 0)
    assert total == 1
    assert item.id == media_live.id

    # разархивация возвращает контент в ленту
    await repo.set_category_archived(session, cat_archived.id, False)
    _, total = await repo.get_favorite_item(session, user.id, 0)
    assert total == 2


async def test_get_favorite_item_empty_for_user_without_favorites(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    media = await make_media(session, category.id, uploaded_by=user.id)
    # звезда чужого юзера не попадает в ленту нашего
    other = await make_user(session, 200)
    await repo.toggle_favorite(session, other.id, media.id)

    item, total = await repo.get_favorite_item(session, user.id, 0)
    assert item is None
    assert total == 0


# ---------------------------------------------------------------- top


async def test_top_favorited_sorts_by_stars_and_skips_unstarred_and_deleted(session):
    u1 = await make_user(session, 100)
    u2 = await make_user(session, 101)
    u3 = await make_user(session, 102)
    category = await make_category(session)
    bronze = await make_media(session, category.id, uploaded_by=u1.id)
    gold = await make_media(session, category.id, uploaded_by=u1.id)
    silver = await make_media(session, category.id, uploaded_by=u1.id)
    unstarred = await make_media(session, category.id, uploaded_by=u1.id)
    deleted = await make_media(session, category.id, uploaded_by=u1.id)

    for user in (u1, u2, u3):
        await repo.toggle_favorite(session, user.id, gold.id)
    for user in (u1, u2):
        await repo.toggle_favorite(session, user.id, silver.id)
    await repo.toggle_favorite(session, u1.id, bronze.id)
    await repo.toggle_favorite(session, u1.id, deleted.id)
    await repo.soft_delete_media(session, deleted.id)

    top = await repo.top_favorited(session, [category.id])
    assert [(m.id, stars) for m, stars in top] == [
        (gold.id, 3),
        (silver.id, 2),
        (bronze.id, 1),
    ]
    top_ids = {m.id for m, _ in top}
    assert unstarred.id not in top_ids  # без звёзд — вне топа
    assert deleted.id not in top_ids  # мягко удалённое скрыто


async def test_top_favorited_filters_by_categories(session):
    user = await make_user(session, 100)
    cat_a = await make_category(session, "котики")
    cat_b = await make_category(session, "собачки")
    media_a = await make_media(session, cat_a.id, uploaded_by=user.id)
    media_b = await make_media(session, cat_b.id, uploaded_by=user.id)
    await repo.toggle_favorite(session, user.id, media_a.id)
    await repo.toggle_favorite(session, user.id, media_b.id)

    top_a = await repo.top_favorited(session, [cat_a.id])
    assert [m.id for m, _ in top_a] == [media_a.id]

    top_both = await repo.top_favorited(session, [cat_a.id, cat_b.id])
    assert {m.id for m, _ in top_both} == {media_a.id, media_b.id}


async def test_top_favorited_respects_limit(session):
    users = [await make_user(session, 100 + i) for i in range(3)]
    category = await make_category(session)
    # 6 медиа с убывающим числом звёзд: 3, 3, 2, 2, 1, 1
    for stars in (3, 3, 2, 2, 1, 1):
        media = await make_media(session, category.id, uploaded_by=users[0].id)
        for user in users[:stars]:
            await repo.toggle_favorite(session, user.id, media.id)

    assert len(await repo.top_favorited(session, [category.id])) == 5  # limit=5
    top_two = await repo.top_favorited(session, [category.id], limit=2)
    assert [stars for _, stars in top_two] == [3, 3]


async def test_top_favorited_empty_categories_returns_empty(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    media = await make_media(session, category.id, uploaded_by=user.id)
    await repo.toggle_favorite(session, user.id, media.id)

    assert await repo.top_favorited(session, []) == []


# ---------------------------------------------------------------- cascade


async def test_delete_category_cascades_favorites(session):
    """Жёсткое удаление категории уносит и звёзды её медиа (FK CASCADE
    через media), звёзды в других категориях не задеты."""
    u1 = await make_user(session, 100)
    u2 = await make_user(session, 101)
    doomed = await make_category(session, "под снос")
    alive = await make_category(session, "живая")
    doomed_media_a = await make_media(session, doomed.id, uploaded_by=u1.id)
    doomed_media_b = await make_media(session, doomed.id, uploaded_by=u1.id)
    survivor_media = await make_media(session, alive.id, uploaded_by=u1.id)

    for user in (u1, u2):
        await repo.toggle_favorite(session, user.id, doomed_media_a.id)
    await repo.toggle_favorite(session, u1.id, doomed_media_b.id)
    await repo.toggle_favorite(session, u2.id, survivor_media.id)
    survivor_id = survivor_media.id

    await repo.delete_category(session, doomed.id)

    favorites = list((await session.execute(select(Favorite))).scalars())
    assert [(fav.user_id, fav.media_id) for fav in favorites] == [
        (u2.id, survivor_id)
    ]
    assert await repo.is_favorite(session, u2.id, survivor_id)
    # лента звездившего снесённую категорию опустела
    item, total = await repo.get_favorite_item(session, u1.id, 0)
    assert item is None
    assert total == 0
