"""Тесты сервиса доступов (skhron.services.access)."""

from conftest import make_category, make_user

from skhron.db import repo
from skhron.services import access

# config.admin_ids == [1] (см. conftest)


async def test_admin_from_config_sees_and_uploads_everywhere(session, config):
    admin = await make_user(session, 1)  # id есть в config.admin_ids
    category = await make_category(session, "мемы")

    assert access.is_admin(admin, config)
    assert await access.can_view(session, admin, config, category.id)
    assert await access.can_upload(session, admin, config, category.id)


async def test_admin_by_db_flag(session, config):
    admin = await make_user(session, 999, admin=True)  # флаг в БД, не в config
    category = await make_category(session, "мемы")

    assert access.is_admin(admin, config)
    assert await access.can_view(session, admin, config, category.id)
    assert await access.can_upload(session, admin, config, category.id)


async def test_regular_user_needs_permission(session, config):
    user = await make_user(session, 100)
    category = await make_category(session, "мемы")

    assert not await access.can_view(session, user, config, category.id)
    assert not await access.can_upload(session, user, config, category.id)


async def test_view_and_upload_are_independent(session, config):
    category = await make_category(session, "мемы")

    viewer = await make_user(session, 100)
    await repo.set_permission(
        session, viewer.id, category.id, can_view=True, can_upload=False
    )
    assert await access.can_view(session, viewer, config, category.id)
    assert not await access.can_upload(session, viewer, config, category.id)

    uploader = await make_user(session, 101)
    await repo.set_permission(
        session, uploader.id, category.id, can_view=False, can_upload=True
    )
    assert not await access.can_view(session, uploader, config, category.id)
    assert await access.can_upload(session, uploader, config, category.id)


async def test_archived_category_hidden_even_for_admin(session, config):
    admin = await make_user(session, 1)
    user = await make_user(session, 100)
    category = await make_category(session, "старьё", archived=True)
    await repo.set_permission(
        session, user.id, category.id, can_view=True, can_upload=True
    )

    assert not await access.can_view(session, admin, config, category.id)
    assert not await access.can_upload(session, admin, config, category.id)
    assert not await access.can_view(session, user, config, category.id)
    assert not await access.can_upload(session, user, config, category.id)


async def test_viewable_and_uploadable_categories_filter(session, config):
    user = await make_user(session, 100)
    cat_view_only = await make_category(session, "только смотреть")
    cat_full = await make_category(session, "смотреть и грузить")
    cat_foreign = await make_category(session, "чужая")
    cat_archived = await make_category(session, "архивная", archived=True)

    await repo.set_permission(
        session, user.id, cat_view_only.id, can_view=True, can_upload=False
    )
    await repo.set_permission(
        session, user.id, cat_full.id, can_view=True, can_upload=True
    )
    # права на архивную есть, но она всё равно скрыта
    await repo.set_permission(
        session, user.id, cat_archived.id, can_view=True, can_upload=True
    )

    viewable = await access.viewable_categories(session, user, config)
    assert {c.id for c in viewable} == {cat_view_only.id, cat_full.id}

    viewable_ids = await access.viewable_category_ids(session, user, config)
    assert set(viewable_ids) == {cat_view_only.id, cat_full.id}

    uploadable = await access.uploadable_categories(session, user, config)
    assert {c.id for c in uploadable} == {cat_full.id}

    # админ видит все активные (без архивной), без записей в permissions
    admin = await make_user(session, 1)
    admin_viewable = await access.viewable_categories(session, admin, config)
    assert {c.id for c in admin_viewable} == {
        cat_view_only.id,
        cat_full.id,
        cat_foreign.id,
    }


async def test_can_delete_media(session, config):
    admin = await make_user(session, 1)
    author = await make_user(session, 100)
    stranger = await make_user(session, 200)

    assert await access.can_delete_media(session, admin, config, author.id)
    assert await access.can_delete_media(session, author, config, author.id)
    assert not await access.can_delete_media(session, stranger, config, author.id)
