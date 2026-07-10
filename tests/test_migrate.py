"""Тесты pre_migrate (skhron/db/migrate.py): пересборка media с
AUTOINCREMENT и перенос устаревших лайков в избранное.

Работает с реальным sqlite3-файлом со СТАРОЙ схемой: media без AUTOINCREMENT,
phash дописан ALTER-ом (последняя колонка), плюс favorites-строка на media.
"""

import sqlite3

import pytest

from skhron.db.migrate import pre_migrate

# Схема, которую create_all генерировал ДО включения sqlite_autoincrement.
# phash здесь намеренно НЕ объявлен: в проде он добит ALTER-ом и потому
# стоит последней колонкой — фикстура повторяет эту историю.
_OLD_DDL = """
CREATE TABLE categories (
    id INTEGER NOT NULL,
    title VARCHAR(128) NOT NULL,
    created_by BIGINT,
    is_archived BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (title)
);
CREATE TABLE users (
    id BIGINT NOT NULL,
    username VARCHAR(64),
    full_name VARCHAR(256) NOT NULL,
    is_admin BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id)
);
CREATE TABLE media (
    id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id VARCHAR(64) NOT NULL,
    media_type VARCHAR(16) NOT NULL,
    caption TEXT,
    uploaded_by BIGINT,
    archive_chat_id BIGINT,
    archive_message_id BIGINT,
    is_deleted BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_media_cat_file UNIQUE (category_id, file_unique_id),
    FOREIGN KEY(category_id) REFERENCES categories (id) ON DELETE CASCADE
);
CREATE INDEX ix_media_category_id ON media (category_id);
CREATE INDEX ix_media_uploaded_by ON media (uploaded_by);
CREATE INDEX ix_media_is_deleted ON media (is_deleted);
CREATE INDEX ix_media_created_at ON media (created_at);
CREATE TABLE favorites (
    user_id BIGINT NOT NULL,
    media_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (user_id, media_id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(media_id) REFERENCES media (id) ON DELETE CASCADE
);
"""

# Порядок: id, category_id, file_id, file_unique_id, media_type, caption,
# uploaded_by, archive_chat_id, archive_message_id, is_deleted, created_at, phash
_MEDIA_ROWS = [
    (
        1, 1, "file-a", "uniq-a", "photo", "смешной кот",
        100, None, None, 0, "2024-01-01 10:00:00", "a1b2c3d4e5f60718",
    ),
    (
        2, 1, "file-b", "uniq-b", "video", None,
        200, -1009, 55, 1, "2024-02-02 11:00:00", None,
    ),
]

_SELECT_MEDIA = (
    "SELECT id, category_id, file_id, file_unique_id, media_type, caption,"
    " uploaded_by, archive_chat_id, archive_message_id, is_deleted,"
    " created_at, phash FROM media ORDER BY id"
)

_EXPECTED_INDEXES = {
    "ix_media_category_id",
    "ix_media_uploaded_by",
    "ix_media_is_deleted",
    "ix_media_created_at",
}


def _make_old_db(path, with_phash: bool = True) -> str:
    con = sqlite3.connect(path)
    try:
        con.executescript(_OLD_DDL)
        if with_phash:
            # как в проде: колонка добита ALTER-ом задним числом
            con.execute("ALTER TABLE media ADD COLUMN phash VARCHAR(16)")
        con.execute(
            "INSERT INTO categories VALUES (1, 'мемы', NULL, 0, '2024-01-01 00:00:00')"
        )
        con.execute(
            "INSERT INTO users VALUES (100, 'vasya', 'Вася', 0, '2024-01-01 00:00:00')"
        )
        for row in _MEDIA_ROWS:
            if with_phash:
                con.execute(
                    "INSERT INTO media (id, category_id, file_id, file_unique_id,"
                    " media_type, caption, uploaded_by, archive_chat_id,"
                    " archive_message_id, is_deleted, created_at, phash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
            else:
                con.execute(
                    "INSERT INTO media (id, category_id, file_id, file_unique_id,"
                    " media_type, caption, uploaded_by, archive_chat_id,"
                    " archive_message_id, is_deleted, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    row[:-1],
                )
        con.execute("INSERT INTO favorites VALUES (100, 1, '2024-03-03 12:00:00')")
        con.commit()
    finally:
        con.close()
    return str(path)


@pytest.fixture
def old_db(tmp_path) -> str:
    return _make_old_db(tmp_path / "old.db")


def _media_table_sql(con: sqlite3.Connection) -> str:
    return con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='media'"
    ).fetchone()[0]


# ------------------------------------------------------------------- tests


def test_pre_migrate_rebuilds_media_with_autoincrement(old_db):
    con = sqlite3.connect(old_db)
    assert "AUTOINCREMENT" not in _media_table_sql(con).upper()
    con.close()

    pre_migrate(old_db)

    con = sqlite3.connect(old_db)
    try:
        # схема пересобрана с AUTOINCREMENT
        assert "AUTOINCREMENT" in _media_table_sql(con).upper()
        # данные целы, включая phash и archive_*-колонки
        assert con.execute(_SELECT_MEDIA).fetchall() == _MEDIA_ROWS
        # favorites пережили DROP TABLE (FK были выключены на время пересборки)
        assert con.execute(
            "SELECT user_id, media_id FROM favorites"
        ).fetchall() == [(100, 1)]
        # индексы пересозданы
        index_names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type='index' AND tbl_name='media'"
            )
        }
        assert _EXPECTED_INDEXES <= index_names
        # ссылочная целостность не порвана
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        # счётчик AUTOINCREMENT посеян максимальным скопированным id
        seq = con.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='media'"
        ).fetchone()
        assert seq is not None
        assert seq[0] >= 2
    finally:
        con.close()


def test_pre_migrate_second_run_is_noop(old_db):
    pre_migrate(old_db)

    con = sqlite3.connect(old_db)
    try:
        master_before = con.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        media_before = con.execute(_SELECT_MEDIA).fetchall()
        favorites_before = con.execute("SELECT * FROM favorites").fetchall()
    finally:
        con.close()

    pre_migrate(old_db)  # повторный вызов — no-op

    con = sqlite3.connect(old_db)
    try:
        assert con.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == master_before
        assert con.execute(_SELECT_MEDIA).fetchall() == media_before
        assert con.execute("SELECT * FROM favorites").fetchall() == favorites_before
    finally:
        con.close()


def test_pre_migrate_blocks_media_id_reuse(old_db):
    """Смысл миграции: id удалённых записей больше не выдаются заново."""
    pre_migrate(old_db)

    con = sqlite3.connect(old_db)
    try:
        cur = con.execute(
            "INSERT INTO media (category_id, file_id, file_unique_id, media_type,"
            " is_deleted, created_at)"
            " VALUES (1, 'file-c', 'uniq-c', 'photo', 0, '2024-04-04 00:00:00')"
        )
        assert cur.lastrowid == 3  # нумерация продолжается за максимумом
        con.execute("DELETE FROM media WHERE id IN (2, 3)")
        con.commit()

        cur = con.execute(
            "INSERT INTO media (category_id, file_id, file_unique_id, media_type,"
            " is_deleted, created_at)"
            " VALUES (1, 'file-d', 'uniq-d', 'photo', 0, '2024-04-05 00:00:00')"
        )
        con.commit()
        # без AUTOINCREMENT здесь был бы переиспользован освободившийся id=2
        assert cur.lastrowid == 4
    finally:
        con.close()


def test_pre_migrate_adds_missing_phash_column(tmp_path):
    """Совсем старая БД без phash: колонка добивается перед пересборкой."""
    db_path = _make_old_db(tmp_path / "ancient.db", with_phash=False)

    pre_migrate(db_path)

    con = sqlite3.connect(db_path)
    try:
        assert "AUTOINCREMENT" in _media_table_sql(con).upper()
        columns = {row[1] for row in con.execute("PRAGMA table_info(media)")}
        assert "phash" in columns
        # данные целы, у старых строк phash пуст
        expected = [row[:-1] + (None,) for row in _MEDIA_ROWS]
        assert con.execute(_SELECT_MEDIA).fetchall() == expected
    finally:
        con.close()


def test_pre_migrate_missing_file_is_noop(tmp_path):
    missing = tmp_path / "no-such.db"
    pre_migrate(str(missing))  # не бросает
    assert not missing.exists()  # и не создаёт пустую БД


# --------------------------------------------------- likes -> favorites

# Схема likes, какой её создавал create_all до замены лайков избранным
_LIKES_DDL = """
CREATE TABLE likes (
    user_id BIGINT NOT NULL,
    media_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (user_id, media_id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(media_id) REFERENCES media (id) ON DELETE CASCADE
)
"""


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def test_pre_migrate_merges_likes_into_favorites(old_db):
    con = sqlite3.connect(old_db)
    try:
        con.execute(_LIKES_DDL)
        # лайк (100, 1) дублирует уже существующую звёздочку из фикстуры,
        # лайк (100, 2) — новый и должен переехать в favorites
        con.execute("INSERT INTO likes VALUES (100, 1, '2024-05-05 09:00:00')")
        con.execute("INSERT INTO likes VALUES (100, 2, '2024-06-06 10:00:00')")
        con.commit()
    finally:
        con.close()

    pre_migrate(old_db)

    con = sqlite3.connect(old_db)
    try:
        # таблица likes исчезла из sqlite_master
        assert not _table_exists(con, "likes")
        # обе записи в favorites; дубль схлопнулся (INSERT OR IGNORE),
        # существующая звёздочка сохранила свой created_at
        assert con.execute(
            "SELECT user_id, media_id, created_at FROM favorites"
            " ORDER BY media_id"
        ).fetchall() == [
            (100, 1, "2024-03-03 12:00:00"),
            (100, 2, "2024-06-06 10:00:00"),
        ]
        # чужие данные целы
        assert con.execute(_SELECT_MEDIA).fetchall() == _MEDIA_ROWS
        assert con.execute("SELECT id, username FROM users").fetchall() == [
            (100, "vasya")
        ]
        assert con.execute("SELECT id, title FROM categories").fetchall() == [
            (1, "мемы")
        ]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []

        master_before = con.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        favorites_before = con.execute(
            "SELECT * FROM favorites ORDER BY media_id"
        ).fetchall()
    finally:
        con.close()

    pre_migrate(old_db)  # повторный вызов — no-op

    con = sqlite3.connect(old_db)
    try:
        assert not _table_exists(con, "likes")
        assert con.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == master_before
        assert con.execute(
            "SELECT * FROM favorites ORDER BY media_id"
        ).fetchall() == favorites_before
    finally:
        con.close()
