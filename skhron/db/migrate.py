"""Синхронные миграции схемы, выполняемые ДО подключения SQLAlchemy.

Здесь то, что нельзя сделать простым ALTER TABLE ADD COLUMN
(его делает init_db): пересборки таблиц и прочая хирургия.
"""

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

# Схема media с AUTOINCREMENT — повторяет skhron/db/models.py.
# AUTOINCREMENT запрещает SQLite переиспользовать id удалённых записей:
# иначе протухшая кнопка ⭐️/🗑 со старым media_id действовала бы
# на чужой новый файл.
_MEDIA_DDL = """CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories (id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    file_unique_id VARCHAR(64) NOT NULL,
    media_type VARCHAR(16) NOT NULL,
    caption TEXT,
    uploaded_by BIGINT,
    phash VARCHAR(16),
    archive_chat_id BIGINT,
    archive_message_id BIGINT,
    is_deleted BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    CONSTRAINT uq_media_cat_file UNIQUE (category_id, file_unique_id)
)"""

# Явный список колонок: в старых БД phash дописан ALTER-ом в конец,
# поэтому порядок колонок может отличаться — SELECT * не годится
_MEDIA_COLS = (
    "id, category_id, file_id, file_unique_id, media_type, caption, "
    "uploaded_by, phash, archive_chat_id, archive_message_id, "
    "is_deleted, created_at"
)


def pre_migrate(database_path: str) -> None:
    """Пересобирает media с AUTOINCREMENT, если таблица создана без него."""
    if not os.path.exists(database_path):
        return
    con = sqlite3.connect(database_path)
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media'"
        ).fetchone()
        if row is None or "AUTOINCREMENT" in row[0].upper():
            return

        logger.info("Миграция: пересборка таблицы media с AUTOINCREMENT")
        columns = {r[1] for r in con.execute("PRAGMA table_info(media)")}
        if "phash" not in columns:
            # совсем старая БД: сначала добиваем колонку
            con.execute("ALTER TABLE media ADD COLUMN phash VARCHAR(16)")
            con.commit()

        # FK выключаем, иначе DROP TABLE каскадом снесёт favorites
        con.execute("PRAGMA foreign_keys=OFF")
        try:
            con.execute("BEGIN")
            con.execute(_MEDIA_DDL.format(name="media_new"))
            con.execute(
                f"INSERT INTO media_new ({_MEDIA_COLS}) "
                f"SELECT {_MEDIA_COLS} FROM media"
            )
            con.execute("DROP TABLE media")
            con.execute("ALTER TABLE media_new RENAME TO media")
            con.execute("CREATE INDEX ix_media_category_id ON media (category_id)")
            con.execute("CREATE INDEX ix_media_uploaded_by ON media (uploaded_by)")
            con.execute("CREATE INDEX ix_media_is_deleted ON media (is_deleted)")
            con.execute("CREATE INDEX ix_media_created_at ON media (created_at)")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.execute("PRAGMA foreign_keys=ON")
    finally:
        con.close()
