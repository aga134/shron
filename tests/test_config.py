"""Тесты парсинга конфига (без .env — конструируем Config напрямую)."""

from skhron.config import Config


def test_admin_ids_from_comma_string():
    config = Config(_env_file=None, bot_token="42:TEST", admin_ids="1, 2,3")
    assert config.admin_ids == [1, 2, 3]


def test_admin_ids_from_single_int():
    config = Config(_env_file=None, bot_token="42:TEST", admin_ids=1)
    assert config.admin_ids == [1]


def test_admin_ids_from_list():
    config = Config(_env_file=None, bot_token="42:TEST", admin_ids=[7, 8])
    assert config.admin_ids == [7, 8]


def test_admin_ids_default_empty():
    config = Config(_env_file=None, bot_token="42:TEST")
    assert config.admin_ids == []


def test_archive_channel_id_empty_string_is_none():
    config = Config(_env_file=None, bot_token="42:TEST", archive_channel_id="")
    assert config.archive_channel_id is None


def test_archive_channel_id_value_kept():
    config = Config(
        _env_file=None, bot_token="42:TEST", archive_channel_id="-1001234567890"
    )
    assert config.archive_channel_id == -1001234567890
