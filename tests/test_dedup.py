"""Тесты перцептивного дедупа: skhron.services.dedup + поиск похожих в repo."""

import random

from conftest import make_category, make_media
from PIL import Image

from skhron.db import repo
from skhron.services.dedup import PHASH_MAX_DISTANCE, dhash, phash_distance

# ---------------------------------------------------------------- helpers


def make_texture(seed: int, size: int = 256, grid: int = 12) -> Image.Image:
    """Детерминированная «мем-подобная» текстура с крупными пятнами.

    Однотонные заливки и монотонные градиенты дают вырожденный dHash
    (все биты одинаковые), поэтому генерируем случайный шум на маленькой
    сетке и растягиваем его сглаживающим resize — получается картинка
    со структурой на масштабе 9x8, по которой dHash реально различает.
    """
    rng = random.Random(seed)
    base = Image.new("L", (grid, grid))
    base.putdata([rng.randrange(256) for _ in range(grid * grid)])
    return base.resize((size, size), Image.BILINEAR)


def flip_bits(phash: str, count: int) -> str:
    """Инвертирует count разрозненных битов 64-битного hex-хэша."""
    value = int(phash, 16)
    for i in range(count):
        value ^= 1 << (i * 7 % 64)  # позиции не повторяются при count <= 12
    return f"{value:016x}"


# ---------------------------------------------------------------- dhash


def test_dhash_returns_16_hex_chars():
    h = dhash(make_texture(seed=3))
    assert len(h) == 16
    int(h, 16)  # валидный hex — не бросает


def test_dhash_stable_after_resize_roundtrip():
    """Пережатая картинка (уменьшили-увеличили) — «почти дубль»."""
    original = make_texture(seed=1)
    squeezed = original.resize((77, 77), Image.LANCZOS).resize(
        (256, 256), Image.LANCZOS
    )
    distance = phash_distance(dhash(original), dhash(squeezed))
    assert distance <= PHASH_MAX_DISTANCE


def test_dhash_differs_for_different_textures():
    h1 = dhash(make_texture(seed=1))
    h2 = dhash(make_texture(seed=2))
    assert phash_distance(h1, h2) > PHASH_MAX_DISTANCE


def test_phash_distance_basics():
    assert phash_distance("0" * 16, "0" * 16) == 0
    assert phash_distance("f" * 16, "0" * 16) == 64
    h = dhash(make_texture(seed=4))
    assert phash_distance(h, h) == 0
    assert phash_distance(h, flip_bits(h, 3)) == 3


# ------------------------------------------------- repo.find_similar_media


async def test_find_similar_media_exact_match(session):
    category = await make_category(session)
    h = dhash(make_texture(seed=5))
    media, created = await repo.add_media(
        session, category.id, "file-a", "uniq-a", "photo", None, 100, phash=h
    )
    assert created

    found = await repo.find_similar_media(
        session, category.id, h, PHASH_MAX_DISTANCE
    )
    assert found is not None
    similar, distance = found
    assert similar.id == media.id
    assert distance == 0


async def test_find_similar_media_close_hash(session):
    """Хэш с тремя инвертированными битами — всё ещё «почти дубль»."""
    category = await make_category(session)
    h = dhash(make_texture(seed=6))
    media, _ = await repo.add_media(
        session, category.id, "file-b", "uniq-b", "photo", None, 100, phash=h
    )

    found = await repo.find_similar_media(
        session, category.id, flip_bits(h, 3), PHASH_MAX_DISTANCE
    )
    assert found is not None
    similar, distance = found
    assert similar.id == media.id
    assert distance == 3


async def test_find_similar_media_beyond_max_distance(session):
    category = await make_category(session)
    h = dhash(make_texture(seed=7))
    await repo.add_media(
        session, category.id, "file-c", "uniq-c", "photo", None, 100, phash=h
    )

    far = flip_bits(h, PHASH_MAX_DISTANCE + 4)  # 12 бит > порога
    assert (
        await repo.find_similar_media(
            session, category.id, far, PHASH_MAX_DISTANCE
        )
        is None
    )


async def test_find_similar_media_ignores_deleted(session):
    category = await make_category(session)
    h = dhash(make_texture(seed=8))
    media, _ = await repo.add_media(
        session, category.id, "file-d", "uniq-d", "photo", None, 100, phash=h
    )
    await repo.soft_delete_media(session, media.id)

    assert (
        await repo.find_similar_media(
            session, category.id, h, PHASH_MAX_DISTANCE
        )
        is None
    )


async def test_find_similar_media_ignores_records_without_phash(session):
    category = await make_category(session)
    await make_media(session, category.id, uploaded_by=100)  # phash=None

    h = dhash(make_texture(seed=9))
    assert (
        await repo.find_similar_media(
            session, category.id, h, PHASH_MAX_DISTANCE
        )
        is None
    )


async def test_find_similar_media_with_none_phash_returns_none(session):
    category = await make_category(session)
    h = dhash(make_texture(seed=9))
    await repo.add_media(
        session, category.id, "file-e", "uniq-e", "photo", None, 100, phash=h
    )

    assert (
        await repo.find_similar_media(
            session, category.id, None, PHASH_MAX_DISTANCE
        )
        is None
    )


async def test_find_similar_media_excludes_own_file(session):
    """При докачке хэшей файл не должен «находить» сам себя."""
    category = await make_category(session)
    h = dhash(make_texture(seed=10))
    await repo.add_media(
        session, category.id, "file-f", "uniq-f", "photo", None, 100, phash=h
    )

    assert (
        await repo.find_similar_media(
            session,
            category.id,
            h,
            PHASH_MAX_DISTANCE,
            exclude_file_unique_id="uniq-f",
        )
        is None
    )


async def test_find_similar_media_returns_nearest_of_several(session):
    category = await make_category(session)
    target = dhash(make_texture(seed=11))
    await repo.add_media(
        session,
        category.id,
        "file-far",
        "uniq-far",
        "photo",
        None,
        100,
        phash=flip_bits(target, 6),
    )
    nearest, _ = await repo.add_media(
        session,
        category.id,
        "file-near",
        "uniq-near",
        "photo",
        None,
        100,
        phash=flip_bits(target, 2),
    )

    found = await repo.find_similar_media(
        session, category.id, target, PHASH_MAX_DISTANCE
    )
    assert found is not None
    similar, distance = found
    assert similar.id == nearest.id
    assert distance == 2


async def test_find_similar_media_scoped_to_category(session):
    cat_a = await make_category(session, "котики")
    cat_b = await make_category(session, "собачки")
    h = dhash(make_texture(seed=12))
    await repo.add_media(
        session, cat_a.id, "file-g", "uniq-g", "photo", None, 100, phash=h
    )

    assert (
        await repo.find_similar_media(session, cat_b.id, h, PHASH_MAX_DISTANCE)
        is None
    )


# ---------------------------------------------- repo.get_media_by_unique_id


async def test_get_media_by_unique_id_finds_live_record(session):
    category = await make_category(session)
    media = await make_media(session, category.id, uploaded_by=100)

    found = await repo.get_media_by_unique_id(
        session, category.id, media.file_unique_id
    )
    assert found is not None
    assert found.id == media.id


async def test_get_media_by_unique_id_finds_soft_deleted(session):
    """Мягко удалённый точный дубль тоже виден — нужен restore-пути add_media."""
    category = await make_category(session)
    media = await make_media(session, category.id, uploaded_by=100)
    await repo.soft_delete_media(session, media.id)

    found = await repo.get_media_by_unique_id(
        session, category.id, media.file_unique_id
    )
    assert found is not None
    assert found.id == media.id
    assert found.is_deleted


async def test_get_media_by_unique_id_scoped_to_category(session):
    cat_a = await make_category(session, "котики")
    cat_b = await make_category(session, "собачки")
    media = await make_media(session, cat_a.id, uploaded_by=100)

    assert (
        await repo.get_media_by_unique_id(
            session, cat_b.id, media.file_unique_id
        )
        is None
    )


async def test_get_media_by_unique_id_unknown_id_returns_none(session):
    category = await make_category(session)
    await make_media(session, category.id, uploaded_by=100)

    assert (
        await repo.get_media_by_unique_id(session, category.id, "uniq-nope")
        is None
    )


# --------------------------------------------------- phash в add_media и repo


async def test_add_media_saves_phash(session):
    category = await make_category(session)
    h = dhash(make_texture(seed=13))
    media, created = await repo.add_media(
        session, category.id, "file-h", "uniq-h", "photo", None, 100, phash=h
    )
    assert created

    fetched = await repo.get_media(session, media.id)
    assert fetched is not None
    assert fetched.phash == h


async def test_restore_soft_deleted_updates_phash(session):
    """Восстановление дубля — новая загрузка: хэш берём от нового файла."""
    category = await make_category(session)
    old_hash = dhash(make_texture(seed=14))
    media, _ = await repo.add_media(
        session, category.id, "file-i", "uniq-i", "photo", None, 100,
        phash=old_hash,
    )
    await repo.soft_delete_media(session, media.id)

    new_hash = flip_bits(old_hash, 5)
    restored, created = await repo.add_media(
        session, category.id, "file-i2", "uniq-i", "photo", None, 200,
        phash=new_hash,
    )
    assert created
    assert restored.id == media.id
    assert restored.phash == new_hash

    # восстановление без хэша (не удалось скачать превью) не затирает старый
    await repo.soft_delete_media(session, media.id)
    restored_again, _ = await repo.add_media(
        session, category.id, "file-i3", "uniq-i", "photo", None, 300,
        phash=None,
    )
    assert restored_again.phash == new_hash


async def test_list_media_without_phash_filters_by_type(session):
    category = await make_category(session)
    photo_no_hash = await make_media(
        session, category.id, uploaded_by=100, media_type="photo"
    )
    video_no_hash = await make_media(
        session, category.id, uploaded_by=100, media_type="video"
    )
    hashed, _ = await repo.add_media(
        session, category.id, "file-j", "uniq-j", "photo", None, 100,
        phash=dhash(make_texture(seed=15)),
    )
    deleted_no_hash = await make_media(
        session, category.id, uploaded_by=100, media_type="photo"
    )
    await repo.soft_delete_media(session, deleted_no_hash.id)

    everything = await repo.list_media_without_phash(session)
    assert {m.id for m in everything} == {photo_no_hash.id, video_no_hash.id}

    only_video = await repo.list_media_without_phash(session, media_type="video")
    assert [m.id for m in only_video] == [video_no_hash.id]


async def test_set_media_phash(session):
    category = await make_category(session)
    media = await make_media(session, category.id, uploaded_by=100)
    assert media.phash is None

    h = dhash(make_texture(seed=16))
    await repo.set_media_phash(session, media.id, h)

    fetched = await repo.get_media(session, media.id)
    assert fetched.phash == h
    assert await repo.list_media_without_phash(session) == []

    # несуществующий id — тихий no-op, без исключений
    await repo.set_media_phash(session, 999_999, h)
