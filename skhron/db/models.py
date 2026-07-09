from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Типы медиа, которые бот умеет сохранять и отдавать
MEDIA_TYPES = ("photo", "video", "animation", "video_note", "voice", "audio")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # Telegram ID пользователя
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(256), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Category(Base):
    __tablename__ = "categories"
    # AUTOINCREMENT запрещает SQLite переиспользовать id удалённых категорий:
    # иначе старый инвайт (хранит id строкой) открыл бы доступ к новой категории
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), unique=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Media(Base):
    __tablename__ = "media"
    # AUTOINCREMENT: id удалённых записей не переиспользуются, иначе
    # протухшие кнопки ⭐️/🗑 действовали бы на чужой новый файл
    __table_args__ = (
        UniqueConstraint("category_id", "file_unique_id", name="uq_media_cat_file"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), index=True
    )
    # file_id достаточно, чтобы бот мог отправить файл заново; сам файл живёт в Telegram
    file_id: Mapped[str] = mapped_column(Text)
    # file_unique_id стабилен для одного и того же файла — используется для дедупликации
    file_unique_id: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(16))
    caption: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # перцептивный dHash картинки/кадра-превью (16 hex-символов, 64 бита);
    # None — тип без картинки (войс/аудио) или превью не удалось скачать
    phash: Mapped[str | None] = mapped_column(String(16))
    # Ссылка на резервную копию в канале-архиве (если настроен)
    archive_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    archive_message_id: Mapped[int | None] = mapped_column(BigInteger)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Permission(Base):
    __tablename__ = "permissions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    can_view: Mapped[bool] = mapped_column(Boolean, default=True)
    can_upload: Mapped[bool] = mapped_column(Boolean, default=False)
    granted_by: Mapped[int | None] = mapped_column(BigInteger)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Favorite(Base):
    __tablename__ = "favorites"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    media_id: Mapped[int] = mapped_column(
        ForeignKey("media.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Chat(Base):
    """Группы, в которые добавлен бот."""

    __tablename__ = "chats"

    # Telegram ID группы (отрицательный)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(256), default="")
    type: Mapped[str] = mapped_column(String(16), default="group")
    # False — бота выгнали из группы (запись храним, права переживают возврат)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # «Мем дня»: минуты от полуночи в DISPLAY_TZ (None — выключен)
    # и локальная дата последней отправки «YYYY-MM-DD» (защита от дублей)
    daily_minutes: Mapped[int | None] = mapped_column(Integer)
    daily_last_sent: Mapped[str | None] = mapped_column(String(10))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GroupPermission(Base):
    """Какие категории разрешено дёргать из конкретной группы.

    В группе контент видят все участники сразу, поэтому права выдаются
    на саму группу, а не на людей.
    """

    __tablename__ = "group_permissions"

    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by: Mapped[int | None] = mapped_column(BigInteger)
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    # ID категорий через запятую, например "1,3,7"
    category_ids: Mapped[str] = mapped_column(Text)
    can_upload: Mapped[bool] = mapped_column(Boolean, default=False)
    # 0 = без ограничения
    max_uses: Mapped[int] = mapped_column(Integer, default=0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
