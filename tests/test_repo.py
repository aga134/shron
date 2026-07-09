"""Тесты слоя репозитория (skhron.db.repo)."""

from datetime import datetime

from conftest import make_category, make_media, make_user

from skhron.db import repo
from skhron.db.models import User

# ---------------------------------------------------------------- add_media


async def test_add_media_dedup(session):
    category = await make_category(session)
    media, created = await repo.add_media(
        session, category.id, "file-1", "uniq-dup", "photo", None, 100
    )
    assert created

    duplicate, created_again = await repo.add_media(
        session, category.id, "file-1-new", "uniq-dup", "photo", None, 100
    )
    assert not created_again
    assert duplicate.id == media.id


async def test_add_media_survives_cross_user_insert_race(session, monkeypatch):
    """Гонка по uq_media_cat_file: SELECT «не увидел» строку конкурента,
    INSERT ловит IntegrityError — add_media возвращает уже существующую
    запись как дубликат, а не роняет сохранение.

    Настоящую вторую сессию на SQLite в один поток не устроить (второй
    коммит упёрся бы в файловую блокировку первого), поэтому окно гонки
    моделируем адресно: строка-дубликат уже в БД, а результат ПЕРВОГО
    SELECT подменяем на «пусто» — ровно то состояние, которое видит
    проигравший гонку INSERT.
    """
    category = await make_category(session)
    winner, created = await repo.add_media(
        session, category.id, "file-w", "uniq-race", "photo", None, 100
    )
    assert created

    real_execute = session.execute
    calls: list[object] = []

    class _EmptyResult:
        def scalar_one_or_none(self):
            return None

    async def racy_execute(stmt, *args, **kwargs):
        calls.append(stmt)
        if len(calls) == 1:
            return _EmptyResult()  # дубликат ещё «не виден»
        return await real_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "execute", racy_execute)

    loser, created_again = await repo.add_media(
        session, category.id, "file-l", "uniq-race", "photo", None, 200
    )
    assert created_again is False
    assert loser.id == winner.id
    # запись победителя гонки не перезаписана проигравшим
    assert loser.file_id == "file-w"
    assert loser.uploaded_by == 100
    # после IntegrityError был повторный SELECT существующей строки
    assert len(calls) >= 2


async def test_add_media_restores_soft_deleted_duplicate(session):
    category = await make_category(session)
    media, _ = await repo.add_media(
        session, category.id, "file-1", "uniq-restore", "photo", None, 100
    )
    old_created_at = media.created_at
    # второй файл, загруженный позже, — изначально он новее в ленте
    newer = await make_media(session, category.id, uploaded_by=100)
    await repo.soft_delete_media(session, media.id)

    # восстанавливает ДРУГОЙ пользователь — запись переатрибутируется
    restored, created = await repo.add_media(
        session, category.id, "file-2", "uniq-restore", "photo", "новая подпись", 200
    )
    assert created
    assert restored.id == media.id
    assert restored.is_deleted is False
    assert restored.file_id == "file-2"
    assert restored.caption == "новая подпись"
    # восстановление = новая загрузка: владелец — новый загрузчик
    assert restored.uploaded_by == 200
    # created_at обновлён — запись всплывает в начало ленты
    assert restored.created_at > old_created_at
    first, total = await repo.get_feed_item(session, category.id, 0)
    assert total == 2
    assert first.id == restored.id
    second, _ = await repo.get_feed_item(session, category.id, 1)
    assert second.id == newer.id


# ---------------------------------------------------------------- captions


async def test_set_media_caption_sets_and_clears(session):
    category = await make_category(session)
    media = await make_media(session, category.id, uploaded_by=100)
    assert media.caption is None

    await repo.set_media_caption(session, media.id, "смешной кот")
    assert (await repo.get_media(session, media.id)).caption == "смешной кот"

    # перезапись новой подписью
    await repo.set_media_caption(session, media.id, "очень смешной кот")
    assert (await repo.get_media(session, media.id)).caption == "очень смешной кот"

    # None — подпись убрана совсем
    await repo.set_media_caption(session, media.id, None)
    assert (await repo.get_media(session, media.id)).caption is None


async def test_set_media_caption_unknown_id_is_noop(session):
    """Несуществующий media_id (запись успели удалить жёстко) — тихий no-op."""
    category = await make_category(session)
    survivor = await make_media(session, category.id, uploaded_by=100, caption="жив")

    await repo.set_media_caption(session, 99999, "в пустоту")

    # чужие записи не задеты, новых не появилось
    assert await repo.get_media(session, 99999) is None
    assert (await repo.get_media(session, survivor.id)).caption == "жив"
    assert await repo.count_media(session, category.id) == 1


# ---------------------------------------------------------------- categories


async def test_delete_category_scrubs_invites(session):
    """Удаление категории вычищает её id из инвайтов; опустевшие инвайты гаснут."""
    cat_a = await make_category(session, "живая")
    cat_b = await make_category(session, "под снос")
    invite_both = await repo.create_invite(
        session, [cat_a.id, cat_b.id], can_upload=False, max_uses=0, created_by=1
    )
    invite_only_b = await repo.create_invite(
        session, [cat_b.id], can_upload=False, max_uses=0, created_by=1
    )
    assert invite_both is not None
    assert invite_only_b is not None

    await repo.delete_category(session, cat_b.id)

    assert await repo.get_category(session, cat_b.id) is None
    # id удалённой категории вычищен, инвайт на оставшуюся живёт дальше
    assert repo.invite_category_ids(invite_both) == [cat_a.id]
    assert invite_both.is_active is True
    # инвайт, ссылавшийся только на удалённую категорию, опустел и деактивирован
    assert repo.invite_category_ids(invite_only_b) == []
    assert invite_only_b.is_active is False


# ------------------------------------------------------------- permissions


async def test_set_permission_returns_permission_on_success(session):
    user = await make_user(session, 100)
    category = await make_category(session)

    perm = await repo.set_permission(
        session, user.id, category.id, can_view=True, can_upload=True, granted_by=1
    )
    assert perm is not None
    assert (perm.user_id, perm.category_id) == (user.id, category.id)
    assert perm.can_view is True
    assert perm.can_upload is True

    # частичный апдейт: не переданные флаги не трогаются
    updated = await repo.set_permission(session, user.id, category.id, can_upload=False)
    assert updated is not None
    assert updated.can_view is True
    assert updated.can_upload is False


async def test_set_permission_returns_none_for_deleted_category(session):
    """Категорию успели удалить параллельно — вместо IntegrityError
    наружу отдаётся None, мусорной записи в БД не остаётся."""
    user = await make_user(session, 100)
    # копируем id до вызова: rollback внутри set_permission экспайрит
    # объекты сессии, и ленивый доступ к user.id из sync-контекста упал бы
    user_id = user.id
    category = await make_category(session, "обречённая")
    doomed_id = category.id
    await repo.delete_category(session, doomed_id)

    perm = await repo.set_permission(session, user_id, doomed_id, can_view=True)

    assert perm is None
    assert await repo.get_permission(session, user_id, doomed_id) is None


# ---------------------------------------------------------------- random


async def test_get_random_media_respects_categories_and_deleted(session):
    cat_a = await make_category(session, "котики")
    cat_b = await make_category(session, "собачки")
    media_a = await make_media(session, cat_a.id, uploaded_by=100)
    media_b = await make_media(session, cat_b.id, uploaded_by=100)

    for _ in range(10):
        picked = await repo.get_random_media(session, [cat_a.id])
        assert picked is not None
        assert picked.id == media_a.id

    picked_any = await repo.get_random_media(session, [cat_a.id, cat_b.id])
    assert picked_any.id in {media_a.id, media_b.id}

    assert await repo.get_random_media(session, []) is None

    await repo.soft_delete_media(session, media_a.id)
    assert await repo.get_random_media(session, [cat_a.id]) is None


# ---------------------------------------------------------------- feed


async def test_get_feed_item_order_and_total(session):
    category = await make_category(session)
    oldest = await make_media(session, category.id, uploaded_by=100)
    middle = await make_media(session, category.id, uploaded_by=100)
    newest = await make_media(session, category.id, uploaded_by=100)

    first, total = await repo.get_feed_item(session, category.id, 0)
    assert total == 3
    assert first.id == newest.id

    second, _ = await repo.get_feed_item(session, category.id, 1)
    assert second.id == middle.id

    third, _ = await repo.get_feed_item(session, category.id, 2)
    assert third.id == oldest.id


async def test_get_feed_item_offset_out_of_bounds(session):
    category = await make_category(session)
    await make_media(session, category.id, uploaded_by=100)

    missing, total = await repo.get_feed_item(session, category.id, 5)
    assert missing is None
    assert total == 1


async def test_get_feed_item_huge_offset_is_clamped(session):
    """Поддельный offset за пределами int64 не роняет SQLite (OverflowError),
    а честно возвращает (None, total)."""
    category = await make_category(session)
    newest = await make_media(session, category.id, uploaded_by=100)

    media, total = await repo.get_feed_item(session, category.id, 10**20)
    assert media is None
    assert total == 1

    # отрицательный offset клампится к нулю — самый свежий элемент
    media, total = await repo.get_feed_item(session, category.id, -(10**20))
    assert media is not None
    assert media.id == newest.id
    assert total == 1


async def test_get_feed_item_skips_deleted(session):
    category = await make_category(session)
    kept = await make_media(session, category.id, uploaded_by=100)
    gone = await make_media(session, category.id, uploaded_by=100)
    await repo.soft_delete_media(session, gone.id)

    item, total = await repo.get_feed_item(session, category.id, 0)
    assert total == 1
    assert item.id == kept.id


# ---------------------------------------------------------------- favorites


async def test_toggle_favorite_roundtrip(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    media = await make_media(session, category.id, uploaded_by=user.id)

    assert await repo.toggle_favorite(session, user.id, media.id) is True
    assert await repo.is_favorite(session, user.id, media.id)

    assert await repo.toggle_favorite(session, user.id, media.id) is False
    assert not await repo.is_favorite(session, user.id, media.id)


async def test_get_favorite_item_filters_by_viewable_ids(session):
    user = await make_user(session, 100)
    cat_visible = await make_category(session, "доступная")
    cat_hidden = await make_category(session, "недоступная")
    media_visible = await make_media(session, cat_visible.id, uploaded_by=user.id)
    media_hidden = await make_media(session, cat_hidden.id, uploaded_by=user.id)

    await repo.toggle_favorite(session, user.id, media_visible.id)
    await repo.toggle_favorite(session, user.id, media_hidden.id)

    item, total = await repo.get_favorite_item(session, user.id, [cat_visible.id], 0)
    assert total == 1
    assert item.id == media_visible.id

    item, total = await repo.get_favorite_item(session, user.id, [], 0)
    assert item is None
    assert total == 0


async def test_get_favorite_item_huge_offset_is_clamped(session):
    """Поддельный offset за пределами int64 не роняет SQLite (OverflowError),
    а честно возвращает (None, total)."""
    user = await make_user(session, 100)
    category = await make_category(session)
    newest = await make_media(session, category.id, uploaded_by=user.id)
    await repo.toggle_favorite(session, user.id, newest.id)

    item, total = await repo.get_favorite_item(
        session, user.id, [category.id], 10**20
    )
    assert item is None
    assert total == 1

    # отрицательный offset клампится к нулю — самый свежий элемент
    item, total = await repo.get_favorite_item(
        session, user.id, [category.id], -(10**20)
    )
    assert item is not None
    assert item.id == newest.id
    assert total == 1


# ---------------------------------------------------------------- invites


async def test_create_invite_returns_none_when_all_categories_deleted(session):
    """Все выбранные категории удалили до «Создать» — инвайт не создаётся."""
    category = await make_category(session, "обречённая")
    doomed_id = category.id
    await repo.delete_category(session, doomed_id)

    invite = await repo.create_invite(
        session, [doomed_id], can_upload=True, max_uses=0, created_by=1
    )

    assert invite is None
    # откат: мусорной строки в invites не осталось
    assert await repo.list_invites(session, active_only=False) == []


async def test_create_invite_trims_deleted_categories_from_csv(session):
    """Удалённые категории вычищаются из CSV, живые остаются."""
    alive = await make_category(session, "живая")
    doomed = await make_category(session, "под снос")
    doomed_id = doomed.id
    await repo.delete_category(session, doomed_id)

    invite = await repo.create_invite(
        session, [alive.id, doomed_id], can_upload=True, max_uses=0, created_by=1
    )

    assert invite is not None
    assert repo.invite_category_ids(invite) == [alive.id]
    assert invite.is_active is True


async def test_redeem_invite_grants_view_and_upload(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    invite = await repo.create_invite(
        session, [category.id], can_upload=True, max_uses=0, created_by=1
    )
    assert invite is not None

    granted = await repo.redeem_invite(session, invite, user.id)
    assert [c.id for c in granted] == [category.id]

    perm = await repo.get_permission(session, user.id, category.id)
    assert perm is not None
    assert perm.can_view is True
    assert perm.can_upload is True
    assert invite.used_count == 1
    assert invite.is_active is True  # max_uses=0 — без ограничения


async def test_redeem_invite_view_only(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    invite = await repo.create_invite(
        session, [category.id], can_upload=False, max_uses=0, created_by=1
    )
    assert invite is not None

    granted = await repo.redeem_invite(session, invite, user.id)
    assert granted

    perm = await repo.get_permission(session, user.id, category.id)
    assert perm.can_view is True
    assert perm.can_upload is False


async def test_redeem_invite_does_not_downgrade_upload(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    # у юзера уже есть can_upload (но нет view)
    seeded = await repo.set_permission(
        session, user.id, category.id, can_view=False, can_upload=True
    )
    assert seeded is not None
    invite = await repo.create_invite(
        session, [category.id], can_upload=False, max_uses=0, created_by=1
    )
    assert invite is not None

    granted = await repo.redeem_invite(session, invite, user.id)
    assert granted

    perm = await repo.get_permission(session, user.id, category.id)
    assert perm.can_view is True  # инвайт добавил просмотр
    assert perm.can_upload is True  # и не отобрал загрузку


async def test_redeem_invite_max_uses_and_deactivation(session):
    first = await make_user(session, 100)
    second = await make_user(session, 101)
    category = await make_category(session)
    invite = await repo.create_invite(
        session, [category.id], can_upload=False, max_uses=1, created_by=1
    )
    assert invite is not None

    granted = await repo.redeem_invite(session, invite, first.id)
    assert granted
    assert invite.used_count == 1
    assert invite.is_active is False  # лимит исчерпан — деактивирован

    assert await repo.redeem_invite(session, invite, second.id) == []
    assert invite.used_count == 1


async def test_redeem_invite_repeat_by_same_user_is_idempotent(session):
    """Повторный клик по той же ссылке не сжигает лимит использований."""
    user = await make_user(session, 100)
    category = await make_category(session)
    invite = await repo.create_invite(
        session, [category.id], can_upload=True, max_uses=2, created_by=1
    )
    assert invite is not None

    first = await repo.redeem_invite(session, invite, user.id)
    assert [c.id for c in first] == [category.id]
    assert invite.used_count == 1
    assert invite.is_active is True

    # повторный редим тем же юзером: категории возвращаются (дружелюбный
    # ответ), но использование не тратится и инвайт не гаснет
    repeat = await repo.redeem_invite(session, invite, user.id)
    assert [c.id for c in repeat] == [category.id]
    assert invite.used_count == 1
    assert invite.is_active is True

    perm = await repo.get_permission(session, user.id, category.id)
    assert perm.can_view is True
    assert perm.can_upload is True


async def test_redeem_inactive_invite_returns_nothing(session):
    user = await make_user(session, 100)
    category = await make_category(session)
    invite = await repo.create_invite(
        session, [category.id], can_upload=True, max_uses=0, created_by=1
    )
    assert invite is not None
    await repo.deactivate_invite(session, invite.id)

    assert await repo.redeem_invite(session, invite, user.id) == []
    assert await repo.get_permission(session, user.id, category.id) is None


# ---------------------------------------------------------------- users


async def test_upsert_user_creates_and_updates(session):
    user = await repo.upsert_user(session, 500, "vasya", "Вася")
    assert user.id == 500
    assert user.username == "vasya"
    assert user.full_name == "Вася"

    updated = await repo.upsert_user(session, 500, "vasya_new", "Вася Новый")
    assert updated.id == 500
    assert updated.username == "vasya_new"
    assert updated.full_name == "Вася Новый"

    users, total = await repo.list_users(session)
    assert total == 1
    assert users[0].username == "vasya_new"


async def test_get_user_by_username_returns_newest_on_duplicates(session):
    """Username переходят из рук в руки: при двух строках с одинаковым
    (протухшим) username возвращается самая свежая, без MultipleResultsFound."""
    stale = User(
        id=100,
        username="nick",
        full_name="Бывший владелец ника",
        created_at=datetime(2020, 1, 1),
    )
    fresh = User(
        id=200,
        username="NICK",  # заодно проверяем регистронезависимость поиска
        full_name="Текущий владелец ника",
        created_at=datetime(2025, 1, 1),
    )
    session.add_all([stale, fresh])
    await session.commit()

    found = await repo.get_user_by_username(session, "@nick")
    assert found is not None
    assert found.id == fresh.id

    assert await repo.get_user_by_username(session, "no_such_nick") is None
