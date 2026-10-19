"""
Lazarus application configuration module.
Reads settings from environment variables and provides validated runtime tunables.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
import os


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/lazarus",
        validation_alias="DATABASE_URL"
    )
    lease_seconds: float = Field(default=6.0, validation_alias="LEASE_SECONDS")
    heartbeat_seconds: float = Field(default=2.0, validation_alias="HEARTBEAT_SECONDS")
    worker_concurrency: int = Field(default=4, validation_alias="WORKER_CONCURRENCY")
    poll_interval: float = Field(default=0.3, validation_alias="POLL_INTERVAL")
    max_attempts: int = Field(default=5, validation_alias="MAX_ATTEMPTS")
    backoff_base: float = Field(default=1.5, validation_alias="BACKOFF_BASE")
    backoff_cap: float = Field(default=20.0, validation_alias="BACKOFF_CAP")
    poison_threshold: int = Field(default=3, validation_alias="POISON_THRESHOLD")
    reaper_interval: float = Field(default=2.0, validation_alias="REAPER_INTERVAL")
    port: int = Field(default=8000, validation_alias="PORT")
    host: str = Field(default="0.0.0.0", validation_alias="HOST")
    lazarus_allow_reset: bool = Field(default=False, validation_alias="LAZARUS_ALLOW_RESET")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
