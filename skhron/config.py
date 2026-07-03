from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str
    admin_ids: Annotated[list[int], NoDecode] = []
    archive_channel_id: int | None = None
    database_path: str = "data/skhron.db"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("archive_channel_id", mode="before")
    @classmethod
    def _empty_as_none(cls, v: object) -> object:
        if v == "":
            return None
        return v


def load_config() -> Config:
    config = Config()
    parent = Path(config.database_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    return config
