from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration from the environment. Shop-level options live in the database."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["production", "staging", "development", "test"] = "production"

    database_url: str = "postgresql+asyncpg://paurubot:paurubot@localhost:5432/paurubot"
    redis_url: str = "redis://localhost:6379/0"

    bot_token: SecretStr = SecretStr("")
    bot_mode: Literal["polling", "webhook"] = "polling"
    owner_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    public_url: str = ""
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    # Bundled Caddy overwrites X-Forwarded-For with a single IP, so 1 is correct
    # behind the default stack. 0 would make every client look like the proxy IP
    # (shared rate-limit buckets, metrics bypass confusion).
    trust_proxy_hops: int = 1
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    master_key: SecretStr = SecretStr("")
    backup_passphrase: SecretStr = SecretStr("")
    backup_dir: str = "/var/lib/paurubot/backups"
    backup_hour: int = Field(default=4, ge=0, le=23)
    backup_keep: int = Field(default=14, ge=1, le=365)
    backup_to_telegram: bool = True

    allow_private_panel_hosts: bool = False
    http_connect_timeout: float = 5.0
    http_total_timeout: float = 15.0

    # Short window so a stolen x-telegram-init-data bearer is not usable for a day.
    webapp_auth_max_age: int = 600
    session_ttl_seconds: int = 60 * 60 * 12
    login_max_attempts: int = 5
    login_lock_seconds: int = 900

    timezone: str = "Asia/Tehran"
    log_level: str = "INFO"
    log_json: bool = True
    sentry_dsn: SecretStr = SecretStr("")
    # Bearer token for GET /metrics (JOB-05); empty means only private addresses may read it.
    metrics_token: SecretStr = SecretStr("")
    # Forward ERROR logs to the "errors" Telegram log channel (ARC-04).
    log_errors_to_telegram: bool = True

    @field_validator("owner_telegram_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().rstrip("/") for part in value.split(",") if part.strip()]
        return value

    @field_validator("public_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def origins(self) -> list[str]:
        origins = list(self.allowed_origins)
        if self.public_url and self.public_url not in origins:
            origins.append(self.public_url)
        return origins

    def validate_runtime(self) -> list[str]:
        """Problems that must stop a production process from starting."""
        problems: list[str] = []
        if not self.database_url.startswith("postgresql"):
            problems.append("DATABASE_URL must point to PostgreSQL")
        if len(self.master_key.get_secret_value()) < 32:
            problems.append("MASTER_KEY must be at least 32 characters")
        if self.is_production and self.public_url and not self.public_url.startswith("https://"):
            problems.append("PUBLIC_URL must use https in production")
        if "paurubot:paurubot@" in self.database_url or "change-me" in self.database_url:
            problems.append("POSTGRES_PASSWORD still uses a default value; set a unique password")
        if self.is_production and not self.bot_token.get_secret_value():
            problems.append("BOT_TOKEN is empty")
        if self.is_production and not self.backup_passphrase.get_secret_value():
            problems.append("BACKUP_PASSPHRASE is empty; backups cannot be restored without it")
        if not self.metrics_token.get_secret_value():
            problems.append("METRICS_TOKEN is empty; /metrics falls back to private-network check only")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
