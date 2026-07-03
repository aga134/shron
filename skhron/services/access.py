"""Проверки доступов. Единственное место, где решается «можно/нельзя».

Правила:
- админ (флаг в БД или ID в ADMIN_IDS) видит и загружает везде;
- архивная категория скрыта из просмотра/загрузки у всех (админ управляет ею
  только через админ-панель);
- остальным нужны записи в permissions.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from skhron.config import Config
from skhron.db import repo
from skhron.db.models import Category, User


def is_admin(user: User, config: Config) -> bool:
    return user.is_admin or user.id in config.admin_ids


async def can_view(
    session: AsyncSession, user: User, config: Config, category_id: int
) -> bool:
    category = await repo.get_category(session, category_id)
    if category is None or category.is_archived:
        return False
    if is_admin(user, config):
        return True
    perm = await repo.get_permission(session, user.id, category_id)
    return perm is not None and perm.can_view


async def can_upload(
    session: AsyncSession, user: User, config: Config, category_id: int
) -> bool:
    category = await repo.get_category(session, category_id)
    if category is None or category.is_archived:
        return False
    if is_admin(user, config):
        return True
    perm = await repo.get_permission(session, user.id, category_id)
    return perm is not None and perm.can_upload


async def viewable_categories(
    session: AsyncSession, user: User, config: Config
) -> list[Category]:
    if is_admin(user, config):
        return await repo.list_categories(session)
    perms = await repo.list_user_permissions(session, user.id)
    return [
        cat
        for perm, cat in perms
        if perm.can_view and not cat.is_archived
    ]


async def viewable_category_ids(
    session: AsyncSession, user: User, config: Config
) -> list[int]:
    return [c.id for c in await viewable_categories(session, user, config)]


async def uploadable_categories(
    session: AsyncSession, user: User, config: Config
) -> list[Category]:
    if is_admin(user, config):
        return await repo.list_categories(session)
    perms = await repo.list_user_permissions(session, user.id)
    return [
        cat
        for perm, cat in perms
        if perm.can_upload and not cat.is_archived
    ]


async def can_delete_media(
    session: AsyncSession, user: User, config: Config, media_uploaded_by: int | None
) -> bool:
    """Удалять может админ или тот, кто загрузил."""
    return is_admin(user, config) or media_uploaded_by == user.id


# ------------------------------------------------------------ group access
# В группе контент видят все участники, поэтому права проверяются
# по самой группе (chat_id), личные права участников не участвуют.


async def group_viewable_categories(
    session: AsyncSession, chat_id: int
) -> list[Category]:
    return await repo.list_chat_categories(session, chat_id)


async def group_viewable_category_ids(
    session: AsyncSession, chat_id: int
) -> list[int]:
    return [c.id for c in await repo.list_chat_categories(session, chat_id)]


async def group_can_view(
    session: AsyncSession, chat_id: int, category_id: int
) -> bool:
    category = await repo.get_category(session, category_id)
    if category is None or category.is_archived:
        return False
    perm = await repo.get_group_permission(session, chat_id, category_id)
    return perm is not None
