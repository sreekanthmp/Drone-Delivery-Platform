"""Application settings, read from environment variables (and an optional ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.mqtt.url import parse_mqtt_url

_ASYNC_PG_PREFIX = "postgresql+asyncpg://"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"{_ASYNC_PG_PREFIX}drone:drone@localhost:5432/drone"
    database_echo: bool = False
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False

    # Only the mock exists: a real Digital Sky integration is not built until requested.
    compliance_provider: Literal["mock"] = "mock"
    permission_validity_minutes: int = Field(default=60, ge=1)
    """How long the mock grants a permission. A typical sortie is estimated at ~22 minutes,
    and the start gate refuses a permission that ends before the mission would, so keep this
    comfortably above the longest sortie you expect."""

    # How commands reach drones: an in-memory stand-in (development, tests) or MQTT.
    drone_link: Literal["in_memory", "mqtt"] = "in_memory"
    mqtt_url: str = "mqtt://localhost:1883"
    """The broker. Plain ``mqtt://`` only: no authentication or TLS has been requested."""

    mqtt_ack_timeout_seconds: float = Field(default=10.0, gt=0)
    """How long to wait for a drone to acknowledge a command before it counts as unanswered."""

    mqtt_worker_client_id: str = "telemetry-worker"
    """The telemetry worker's durable MQTT identity. Run exactly one worker per broker."""

    # Live tracking (Phase 5B). Redis is a view, never the source of truth: leave it unset and
    # dispatch, commands and the audit trail work exactly the same, with no live tracking.
    redis_url: str | None = None
    live_state_ttl_seconds: int = Field(default=60, ge=1)
    """A drone's latest sample is forgotten this long after its last one."""

    link_lost_after_seconds: float = Field(default=15.0, gt=0)
    """A drone in the air that sends no samples for this long raises a link-lost alert."""

    low_battery_percent: float = Field(default=20.0, ge=0, le=100)
    state_mismatch_grace_seconds: float = Field(default=30.0, ge=0)
    """How long the platform and a drone may disagree about flying before it is an alert."""

    battery_sync_step_percent: float = Field(default=5.0, gt=0)
    """The stored battery follows the drone's in steps this big (selection reads the stored one)."""

    stale_reservation_minutes: int = Field(default=5, ge=1)
    """A flight a dispatch left unfinished this long is cancelled and its drone freed."""

    ws_queue_size: int = Field(default=100, ge=1)
    """Messages a WebSocket watcher may fall behind by before it is told to re-sync."""

    ws_ping_seconds: float = Field(default=15.0, gt=0)
    """A quiet stream sends a ping this often, so a dead connection is noticed."""

    watchdog_interval_seconds: float = Field(default=5.0, gt=0)
    reaper_interval_seconds: float = Field(default=60.0, gt=0)

    @field_validator("redis_url")
    @classmethod
    def _require_redis_scheme(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None  # "REDIS_URL=" in an env file means "no Redis"
        if not value.startswith(("redis://", "rediss://")):
            raise ValueError("redis_url must start with 'redis://'")
        return value

    @field_validator("mqtt_url")
    @classmethod
    def _require_mqtt_scheme(cls, value: str) -> str:
        parse_mqtt_url(value)  # raises ValueError with a clear message
        return value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: str) -> str:
        if not value.startswith(_ASYNC_PG_PREFIX):
            raise ValueError(f"database_url must start with '{_ASYNC_PG_PREFIX}'")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
