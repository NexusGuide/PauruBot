from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from typing_extensions import Annotated


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    database_url: str = "sqlite+aiosqlite:///./data/paurubot.db"

    web_host: str = "0.0.0.0"
    web_port: int = 8080
    public_url: str = ""
    secret_key: str = "change-me"
    web_admin_username: str = "admin"
    web_admin_password: str = ""

    use_webhook: bool = False
    timezone: str = "Asia/Tehran"
    log_level: str = "INFO"
    data_dir: str = "./data"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("public_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def webhook_path(self) -> str:
        return "/tg/webhook"


@lru_cache
def get_settings() -> Settings:
    return Settings()
