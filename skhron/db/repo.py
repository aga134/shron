"""Все операции с БД — тонкие асинхронные функции над сессией SQLAlchemy.

Соглашения:
- первая позиционная арга всегда session: AsyncSession;
- функции сами коммитят изменения;
- «удаление» медиа мягкое (is_deleted), файлы в Telegram живут вечно.
"""

import secrets

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from skhron.db.models import (
    Category,
    Chat,
    Favorite,
    GroupPermission,
    Invite,
    Media,
    Permission,
    User,
    utcnow,
)

# ---------------------------------------------------------------- users


async def upsert_user(
    session: AsyncSession, tg_id: int, username: str | None, full_name: str
) -> User:
    user = await session.get(User, tg_id)
    if user is None:
        user = User(id=tg_id, username=username, full_name=full_name)
        session.add(user)
        try:
            await session.commit()
            return user
        except IntegrityError:
            # параллельный апдейт того же нового юзера успел вставить строку
            await session.rollback()
            user = await session.get(User, tg_id)
            if user is None:  # не должно случиться, но лучше, чем упасть
                return User(id=tg_id, username=username, full_name=full_name)
    if user.username != username or user.full_name != full_name:
        user.username = username
        user.full_name = full_name
        await session.commit()
    return user


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    """Username в Telegram переходят из рук в руки, поэтому в users может быть
    несколько строк с одним (протухшим) username — берём самую свежую."""
    username = username.lstrip("@")
    stmt = (
        select(User)
        .where(func.lower(User.username) == username.lower())
        .order_by(User.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def list_users(
    session: AsyncSession, offset: int = 0, limit: int = 10
) -> tuple[list[User], int]:
    total = (await session.execute(select(func.count(User.id)))).scalar_one()
    stmt = select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
    users = list((await session.execute(stmt)).scalars())
    return users, total


async def set_admin(session: AsyncSession, user_id: int, is_admin: bool) -> None:
    user = await session.get(User, user_id)
    if user is not None:
        user.is_admin = is_admin
        await session.commit()


async def list_admins(session: AsyncSession) -> list[User]:
    stmt = select(User).where(User.is_admin.is_(True))
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------- categories


async def create_category(
    session: AsyncSession, title: str, created_by: int | None
) -> Category | None:
    """Возвращает None, если категория с таким названием уже есть."""
    existing = await get_category_by_title(session, title)
    if existing is not None:
        return None
    category = Category(title=title, created_by=created_by)
    session.add(category)
    await session.commit()
    return category


async def get_category(session: AsyncSession, category_id: int) -> Category | None:
    return await session.get(Category, category_id)


async def get_category_by_title(session: AsyncSession, title: str) -> Category | None:
    stmt = select(Category).where(func.lower(Category.title) == title.lower())
    return (await session.execute(stmt)).scalar_one_or_none()


async def rename_category(
    session: AsyncSession, category_id: int, title: str
) -> bool:
    """False, если название занято другой категорией."""
    existing = await get_category_by_title(session, title)
    if existing is not None and existing.id != category_id:
        return False
    category = await session.get(Category, category_id)
    if category is None:
        return False
    category.title = title
    await session.commit()
    return True


async def set_category_archived(
    session: AsyncSession, category_id: int, archived: bool
) -> None:
    category = await session.get(Category, category_id)
    if category is not None:
        category.is_archived = archived
        await session.commit()


async def delete_category(session: AsyncSession, category_id: int) -> None:
    """Жёсткое удаление: каскадом уходят медиа-записи и права.

    Файлы при этом остаются в Telegram (и в канале-архиве, если настроен).
    Инвайты хранят id категорий строкой (без FK), поэтому вычищаем id
    вручную, а опустевшие инвайты деактивируем.
    """
    category = await session.get(Category, category_id)
    if category is None:
        return
    await session.delete(category)
    invites = (await session.execute(select(Invite))).scalars()
    for invite in invites:
        ids = invite_category_ids(invite)
        if category_id in ids:
            ids.remove(category_id)
            invite.category_ids = ",".join(str(i) for i in ids)
            if not ids:
                invite.is_active = False
    await session.commit()


async def list_categories(
    session: AsyncSession, include_archived: bool = False
) -> list[Category]:
    stmt = select(Category).order_by(Category.title)
    if not include_archived:
        stmt = stmt.where(Category.is_archived.is_(False))
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------- permissions


async def get_permission(
    session: AsyncSession, user_id: int, category_id: int
) -> Permission | None:
    return await session.get(Permission, (user_id, category_id))


async def set_permission(
    session: AsyncSession,
    user_id: int,
    category_id: int,
    *,
    can_view: bool | None = None,
    can_upload: bool | None = None,
    granted_by: int | None = None,
) -> Permission | None:
    """Upsert: не переданные флаги не трогаются.

    None — запись не удалось создать/обновить из-за параллельного
    изменения (второй админ отозвал доступ или удалил категорию/юзера).
    """
    perm = await session.get(Permission, (user_id, category_id))
    if perm is None:
        perm = Permission(
            user_id=user_id,
            category_id=category_id,
            can_view=bool(can_view) if can_view is not None else True,
            can_upload=bool(can_upload) if can_upload is not None else False,
            granted_by=granted_by,
        )
        session.add(perm)
    else:
        if can_view is not None:
            perm.can_view = can_view
        if can_upload is not None:
            perm.can_upload = can_upload
        if granted_by is not None:
            perm.granted_by = granted_by
    try:
        await session.commit()
    except (IntegrityError, StaleDataError):
        # кросс-админская гонка: вставку/апдейт перегнал revoke или
        # удаление категории — перечитываем финальное состояние
        await session.rollback()
        return await session.get(Permission, (user_id, category_id))
    return perm


async def revoke_permission(
    session: AsyncSession, user_id: int, category_id: int
) -> None:
    await session.execute(
        delete(Permission).where(
            Permission.user_id == user_id, Permission.category_id == category_id
        )
    )
    await session.commit()


async def list_user_permissions(
    session: AsyncSession, user_id: int
) -> list[tuple[Permission, Category]]:
    stmt = (
        select(Permission, Category)
        .join(Category, Category.id == Permission.category_id)
        .where(Permission.user_id == user_id)
        .order_by(Category.title)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def list_category_users(
    session: AsyncSession, category_id: int
) -> list[tuple[Permission, User]]:
    stmt = (
        select(Permission, User)
        .join(User, User.id == Permission.user_id)
        .where(Permission.category_id == category_id)
        .order_by(User.full_name)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


# ---------------------------------------------------------------- media


async def add_media(
    session: AsyncSession,
    category_id: int,
    file_id: str,
    file_unique_id: str,
    media_type: str,
    caption: str | None,
    uploaded_by: int | None,
    archive_chat_id: int | None = None,
    archive_message_id: int | None = None,
    phash: str | None = None,
) -> tuple[Media, bool]:
    """Возвращает (media, created).

    created=False — такой файл уже есть в категории (дубликат).
    Мягко удалённый дубликат восстанавливается и считается created=True.
    """
    stmt = select(Media).where(
        Media.category_id == category_id, Media.file_unique_id == file_unique_id
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        if existing.is_deleted:
            # восстановление — это новая загрузка: переатрибутируем запись,
            # чтобы новый загрузчик мог её удалять, а лента показала её сверху
            existing.is_deleted = False
            existing.file_id = file_id
            existing.uploaded_by = uploaded_by
            existing.created_at = utcnow()
            if archive_chat_id is not None:
                existing.archive_chat_id = archive_chat_id
                existing.archive_message_id = archive_message_id
            if caption:
                existing.caption = caption
            if phash:
                existing.phash = phash
            await session.commit()
            return existing, True
        return existing, False

    media = Media(
        category_id=category_id,
        file_id=file_id,
        file_unique_id=file_unique_id,
        media_type=media_type,
        caption=caption,
        uploaded_by=uploaded_by,
        archive_chat_id=archive_chat_id,
        archive_message_id=archive_message_id,
        phash=phash,
    )
    session.add(media)
    try:
        await session.commit()
    except IntegrityError:
        # кросс-юзерская гонка: тот же файл успел вставить кто-то другой
        await session.rollback()
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing, False
        raise
    return media, True


async def get_media(session: AsyncSession, media_id: int) -> Media | None:
    return await session.get(Media, media_id)


async def get_media_by_unique_id(
    session: AsyncSession, category_id: int, file_unique_id: str
) -> Media | None:
    """Точный дубль в категории (включая мягко удалённые — для restore-пути)."""
    stmt = select(Media).where(
        Media.category_id == category_id,
        Media.file_unique_id == file_unique_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_similar_media(
    session: AsyncSession,
    category_id: int,
    phash: str | None,
    max_distance: int,
    exclude_file_unique_id: str | None = None,
) -> tuple[Media, int] | None:
    """Ищет в категории визуально похожий файл по dHash.

    Возвращает (самый похожий, расстояние) или None. Линейный проход
    по хэшам категории — на масштабе «сотни мемов» это мгновенно.
    """
    from skhron.services.dedup import phash_distance

    if not phash:
        return None
    stmt = select(Media).where(
        Media.category_id == category_id,
        Media.is_deleted.is_(False),
        Media.phash.is_not(None),
    )
    if exclude_file_unique_id is not None:
        stmt = stmt.where(Media.file_unique_id != exclude_file_unique_id)
    best: Media | None = None
    best_distance = max_distance + 1
    for media in (await session.execute(stmt)).scalars():
        distance = phash_distance(phash, media.phash)
        if distance < best_distance:
            best, best_distance = media, distance
    if best is None:
        return None
    return best, best_distance


async def list_media_without_phash(
    session: AsyncSession, media_type: str | None = None
) -> list[Media]:
    """Медиа без хэша — для докачки хэшей задним числом (/rehash)."""
    stmt = select(Media).where(
        Media.is_deleted.is_(False), Media.phash.is_(None)
    )
    if media_type is not None:
        stmt = stmt.where(Media.media_type == media_type)
    return list((await session.execute(stmt)).scalars())


async def set_media_phash(
    session: AsyncSession, media_id: int, phash: str
) -> None:
    media = await session.get(Media, media_id)
    if media is not None:
        media.phash = phash
        await session.commit()


async def set_media_caption(
    session: AsyncSession, media_id: int, caption: str | None
) -> None:
    media = await session.get(Media, media_id)
    if media is not None:
        media.caption = caption
        await session.commit()


async def soft_delete_media(session: AsyncSession, media_id: int) -> None:
    media = await session.get(Media, media_id)
    if media is not None:
        media.is_deleted = True
        await session.commit()


async def count_media(
    session: AsyncSession, category_id: int | None = None
) -> int:
    stmt = select(func.count(Media.id)).where(Media.is_deleted.is_(False))
    if category_id is not None:
        stmt = stmt.where(Media.category_id == category_id)
    return (await session.execute(stmt)).scalar_one()


async def get_random_media(
    session: AsyncSession, category_ids: list[int]
) -> Media | None:
    if not category_ids:
        return None
    stmt = (
        select(Media)
        .where(Media.category_id.in_(category_ids), Media.is_deleted.is_(False))
        .order_by(func.random())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_feed_item(
    session: AsyncSession, category_id: int, offset: int
) -> tuple[Media | None, int]:
    """Лента категории: (элемент на позиции offset, всего элементов).

    offset=0 — самый новый.
    """
    total = await count_media(session, category_id)
    # кламп: callback_data подделываем, а OFFSET за 2^63 роняет SQLite
    offset = max(0, min(offset, total))
    stmt = (
        select(Media)
        .where(Media.category_id == category_id, Media.is_deleted.is_(False))
        .order_by(Media.created_at.desc(), Media.id.desc())
        .offset(offset)
        .limit(1)
    )
    media = (await session.execute(stmt)).scalar_one_or_none()
    return media, total


async def get_favorite_item(
    session: AsyncSession,
    user_id: int,
    viewable_category_ids: list[int],
    offset: int,
) -> tuple[Media | None, int]:
    """Лента избранного: только из доступных пользователю категорий."""
    if not viewable_category_ids:
        return None, 0
    base = (
        select(Media)
        .join(Favorite, Favorite.media_id == Media.id)
        .where(
            Favorite.user_id == user_id,
            Media.is_deleted.is_(False),
            Media.category_id.in_(viewable_category_ids),
        )
    )
    total = (
        await session.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar_one()
    offset = max(0, min(offset, total))
    stmt = (
        base.order_by(Favorite.created_at.desc(), Media.id.desc())
        .offset(offset)
        .limit(1)
    )
    media = (await session.execute(stmt)).scalar_one_or_none()
    return media, total


async def toggle_favorite(
    session: AsyncSession, user_id: int, media_id: int
) -> bool:
    """True — добавили в избранное, False — убрали."""
    fav = await session.get(Favorite, (user_id, media_id))
    if fav is None:
        session.add(Favorite(user_id=user_id, media_id=media_id))
        try:
            await session.commit()
        except IntegrityError:
            # двойной тап: параллельный апдейт уже вставил запись
            await session.rollback()
        return True
    await session.delete(fav)
    await session.commit()
    return False


async def is_favorite(session: AsyncSession, user_id: int, media_id: int) -> bool:
    return await session.get(Favorite, (user_id, media_id)) is not None


async def recent_media(
    session: AsyncSession, category_ids: list[int], limit: int = 20
) -> list[Media]:
    if not category_ids:
        return []
    stmt = (
        select(Media)
        .where(Media.category_id.in_(category_ids), Media.is_deleted.is_(False))
        .order_by(Media.created_at.desc(), Media.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def search_media(
    session: AsyncSession, category_ids: list[int], query: str, limit: int = 20
) -> list[Media]:
    """Поиск по подписи. Регистронезависимость для кириллицы делаем в Python,
    т.к. SQLite lower() умеет только ASCII."""
    if not category_ids or not query.strip():
        return []
    query_lower = query.strip().lower()
    stmt = (
        select(Media)
        .where(
            Media.category_id.in_(category_ids),
            Media.is_deleted.is_(False),
            Media.caption.is_not(None),
        )
        .order_by(Media.created_at.desc(), Media.id.desc())
        .limit(1000)
    )
    candidates = (await session.execute(stmt)).scalars()
    result = []
    for media in candidates:
        if media.caption and query_lower in media.caption.lower():
            result.append(media)
            if len(result) >= limit:
                break
    return result


# ---------------------------------------------------------------- chats


async def upsert_chat(
    session: AsyncSession, chat_id: int, title: str, chat_type: str
) -> Chat:
    chat = await session.get(Chat, chat_id)
    if chat is None:
        chat = Chat(id=chat_id, title=title, type=chat_type)
        session.add(chat)
        try:
            await session.commit()
            return chat
        except IntegrityError:
            await session.rollback()
            chat = await session.get(Chat, chat_id)
            if chat is None:
                return Chat(id=chat_id, title=title, type=chat_type)
    if chat.title != title or chat.type != chat_type or not chat.is_active:
        chat.title = title
        chat.type = chat_type
        chat.is_active = True
        await session.commit()
    return chat


async def set_chat_active(session: AsyncSession, chat_id: int, active: bool) -> None:
    chat = await session.get(Chat, chat_id)
    if chat is not None:
        chat.is_active = active
        await session.commit()


async def get_chat(session: AsyncSession, chat_id: int) -> Chat | None:
    return await session.get(Chat, chat_id)


async def list_chats(session: AsyncSession, active_only: bool = False) -> list[Chat]:
    stmt = select(Chat).order_by(Chat.title)
    if active_only:
        stmt = stmt.where(Chat.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def set_chat_daily(
    session: AsyncSession, chat_id: int, minutes: int | None
) -> None:
    """Время «мема дня» (минуты от полуночи в DISPLAY_TZ), None — выключить.

    daily_last_sent сознательно НЕ сбрасывается: отметка «сегодня уже
    постили» переживает смену времени, иначе повторный тап по пресету
    отправлял бы второй мем за день. Новое время в будущем всё равно
    сработает сегодня, если сегодня ещё не постили.
    """
    chat = await session.get(Chat, chat_id)
    if chat is not None and chat.daily_minutes != minutes:
        chat.daily_minutes = minutes
        await session.commit()


async def set_chat_daily_sent(
    session: AsyncSession, chat_id: int, sent_date: str
) -> None:
    chat = await session.get(Chat, chat_id)
    if chat is not None:
        chat.daily_last_sent = sent_date
        await session.commit()


async def list_daily_chats(session: AsyncSession) -> list[Chat]:
    """Активные группы с включённым «мемом дня»."""
    stmt = select(Chat).where(
        Chat.is_active.is_(True), Chat.daily_minutes.is_not(None)
    )
    return list((await session.execute(stmt)).scalars())


async def delete_chat(session: AsyncSession, chat_id: int) -> None:
    """«Забыть» группу: каскадом уходят и её права."""
    chat = await session.get(Chat, chat_id)
    if chat is not None:
        await session.delete(chat)
        await session.commit()


async def migrate_chat(session: AsyncSession, old_id: int, new_id: int) -> None:
    """Telegram превратил группу в супергруппу (новый chat_id):
    переносим запись чата и все выданные права на новый id."""
    old = await session.get(Chat, old_id)
    if old is None:
        return
    new = await session.get(Chat, new_id)
    if new is None:
        new = Chat(
            id=new_id,
            title=old.title,
            type="supergroup",
            is_active=True,
            daily_minutes=old.daily_minutes,
            daily_last_sent=old.daily_last_sent,
        )
        session.add(new)
        await session.flush()
    else:
        new.is_active = True
        new.type = "supergroup"
        if old.title and not new.title:
            new.title = old.title
        # расписание «мема дня» переезжает, если у новой записи его нет
        if new.daily_minutes is None and old.daily_minutes is not None:
            new.daily_minutes = old.daily_minutes
            new.daily_last_sent = old.daily_last_sent
    existing_ids = {
        p.category_id
        for p in (
            await session.execute(
                select(GroupPermission).where(GroupPermission.chat_id == new_id)
            )
        ).scalars()
    }
    old_perms = (
        await session.execute(
            select(GroupPermission).where(GroupPermission.chat_id == old_id)
        )
    ).scalars()
    for perm in old_perms:
        if perm.category_id not in existing_ids:
            session.add(
                GroupPermission(
                    chat_id=new_id,
                    category_id=perm.category_id,
                    granted_by=perm.granted_by,
                )
            )
    # старую запись удаляем — каскадом уйдут и её права
    await session.delete(old)
    await session.commit()


# ------------------------------------------------------- group permissions


async def get_group_permission(
    session: AsyncSession, chat_id: int, category_id: int
) -> GroupPermission | None:
    return await session.get(GroupPermission, (chat_id, category_id))


async def set_group_permission(
    session: AsyncSession, chat_id: int, category_id: int, granted_by: int | None
) -> GroupPermission | None:
    """None — выдать право не вышло (категорию/чат успели удалить)."""
    perm = await session.get(GroupPermission, (chat_id, category_id))
    if perm is None:
        perm = GroupPermission(
            chat_id=chat_id, category_id=category_id, granted_by=granted_by
        )
        session.add(perm)
        try:
            await session.commit()
        except IntegrityError:
            # либо FK (категорию/чат удалили) — вернём None,
            # либо параллельная вставка того же права — вернём её
            await session.rollback()
            return await session.get(GroupPermission, (chat_id, category_id))
    return perm


async def revoke_group_permission(
    session: AsyncSession, chat_id: int, category_id: int
) -> None:
    await session.execute(
        delete(GroupPermission).where(
            GroupPermission.chat_id == chat_id,
            GroupPermission.category_id == category_id,
        )
    )
    await session.commit()


async def list_chat_categories(
    session: AsyncSession, chat_id: int, include_archived: bool = False
) -> list[Category]:
    stmt = (
        select(Category)
        .join(GroupPermission, GroupPermission.category_id == Category.id)
        .where(GroupPermission.chat_id == chat_id)
        .order_by(Category.title)
    )
    if not include_archived:
        stmt = stmt.where(Category.is_archived.is_(False))
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------- stats


async def get_stats(session: AsyncSession) -> dict:
    users_count = (await session.execute(select(func.count(User.id)))).scalar_one()
    categories = await list_categories(session, include_archived=True)
    media_total = await count_media(session)
    by_type_stmt = (
        select(Media.media_type, func.count(Media.id))
        .where(Media.is_deleted.is_(False))
        .group_by(Media.media_type)
    )
    by_type = dict((await session.execute(by_type_stmt)).all())
    top_stmt = (
        select(Category, func.count(Media.id).label("cnt"))
        .join(Media, Media.category_id == Category.id)
        .where(Media.is_deleted.is_(False))
        .group_by(Category.id)
        .order_by(func.count(Media.id).desc())
        .limit(5)
    )
    top_categories = [(row[0], row[1]) for row in (await session.execute(top_stmt)).all()]
    favorites_count = (
        await session.execute(select(func.count()).select_from(Favorite))
    ).scalar_one()
    return {
        "users": users_count,
        "categories_active": sum(1 for c in categories if not c.is_archived),
        "categories_archived": sum(1 for c in categories if c.is_archived),
        "media_total": media_total,
        "media_by_type": by_type,
        "top_categories": top_categories,
        "favorites": favorites_count,
    }


# ---------------------------------------------------------------- invites


async def create_invite(
    session: AsyncSession,
    category_ids: list[int],
    can_upload: bool,
    max_uses: int,
    created_by: int | None,
) -> Invite | None:
    """None — все выбранные категории успели удалить (черновик протух)."""
    invite = Invite(
        code=secrets.token_urlsafe(8),
        category_ids=",".join(str(c) for c in category_ids),
        can_upload=can_upload,
        max_uses=max_uses,
        created_by=created_by,
    )
    session.add(invite)
    # flush открывает транзакцию записи — сериализуемся с delete_category
    await session.flush()
    existing = set(
        (
            await session.execute(
                select(Category.id).where(Category.id.in_(category_ids))
            )
        ).scalars()
    )
    valid = [c for c in category_ids if c in existing]
    if not valid:
        await session.rollback()
        return None
    invite.category_ids = ",".join(str(c) for c in valid)
    await session.commit()
    return invite


async def get_invite(session: AsyncSession, invite_id: int) -> Invite | None:
    return await session.get(Invite, invite_id)


async def get_invite_by_code(session: AsyncSession, code: str) -> Invite | None:
    stmt = select(Invite).where(Invite.code == code)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_invites(
    session: AsyncSession, active_only: bool = True
) -> list[Invite]:
    stmt = select(Invite).order_by(Invite.created_at.desc())
    if active_only:
        stmt = stmt.where(Invite.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def deactivate_invite(session: AsyncSession, invite_id: int) -> None:
    invite = await session.get(Invite, invite_id)
    if invite is not None:
        invite.is_active = False
        await session.commit()


def invite_category_ids(invite: Invite) -> list[int]:
    return [int(x) for x in invite.category_ids.split(",") if x]


async def redeem_invite(
    session: AsyncSession, invite: Invite, user_id: int
) -> list[Category]:
    """Выдаёт права по инвайту. Возвращает список категорий, к которым
    открыт доступ (пустой — если инвайт недействителен)."""
    if not invite.is_active:
        return []
    if invite.max_uses and invite.used_count >= invite.max_uses:
        return []

    granted: list[Category] = []
    newly_granted = False  # повторный клик того же юзера не сжигает лимит
    for category_id in invite_category_ids(invite):
        category = await session.get(Category, category_id)
        if category is None or category.is_archived:
            continue
        perm = await session.get(Permission, (user_id, category_id))
        if perm is None:
            session.add(
                Permission(
                    user_id=user_id,
                    category_id=category_id,
                    can_view=True,
                    can_upload=invite.can_upload,
                    granted_by=invite.created_by,
                )
            )
            newly_granted = True
        else:
            if not perm.can_view or (invite.can_upload and not perm.can_upload):
                newly_granted = True
            perm.can_view = True
            perm.can_upload = perm.can_upload or invite.can_upload
        granted.append(category)

    if granted and newly_granted:
        # атомарно «занимаем» использование: два конкурентных редима
        # одноразового инвайта не должны пройти оба
        result = await session.execute(
            update(Invite)
            .where(
                Invite.id == invite.id,
                Invite.is_active.is_(True),
                or_(Invite.max_uses == 0, Invite.used_count < Invite.max_uses),
            )
            .values(used_count=Invite.used_count + 1)
        )
        if result.rowcount == 0:
            await session.rollback()
            return []
        await session.refresh(invite)
        if invite.max_uses and invite.used_count >= invite.max_uses:
            invite.is_active = False
    try:
        await session.commit()
    except IntegrityError:
        # гонка с параллельной выдачей тех же прав — считаем редим неудавшимся,
        # юзер просто кликнет ссылку ещё раз
        await session.rollback()
        return []
    return granted
