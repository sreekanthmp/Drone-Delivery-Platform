"""Application settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def make(**env: str) -> Settings:
    # _env_file=None: never let a developer's local .env leak into the assertions.
    return Settings(_env_file=None, **env)  # type: ignore[arg-type,call-arg]


class TestDefaults:
    def test_local_development_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "DATABASE_URL",
            "DATABASE_ECHO",
            "LOG_LEVEL",
            "LOG_JSON",
            "PERMISSION_VALIDITY_MINUTES",
            "DRONE_LINK",
        ):
            monkeypatch.delenv(name, raising=False)

        settings = make()

        assert settings.database_url == "postgresql+asyncpg://drone:drone@localhost:5432/drone"
        assert settings.database_echo is False
        assert settings.database_pool_size == 10
        assert settings.database_max_overflow == 10
        assert settings.log_level == "INFO"
        assert settings.log_json is False
        assert settings.compliance_provider == "mock"
        assert settings.permission_validity_minutes == 60
        assert settings.drone_link == "in_memory"


class TestEnvironmentOverrides:
    def test_values_come_from_environment_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.internal:6432/prod")
        monkeypatch.setenv("DATABASE_ECHO", "true")
        monkeypatch.setenv("DATABASE_POOL_SIZE", "25")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("LOG_JSON", "1")

        settings = make()

        assert settings.database_url == "postgresql+asyncpg://u:p@db.internal:6432/prod"
        assert settings.database_echo is True
        assert settings.database_pool_size == 25
        assert settings.log_level == "DEBUG"
        assert settings.log_json is True

    def test_an_env_file_is_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("LOG_LEVEL=ERROR\nSOMETHING_UNRELATED=1\n")

        settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

        assert settings.log_level == "ERROR"


class TestCompliance:
    def test_the_provider_and_validity_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("COMPLIANCE_PROVIDER", "mock")
        monkeypatch.setenv("PERMISSION_VALIDITY_MINUTES", "45")

        settings = make()

        assert settings.compliance_provider == "mock"
        assert settings.permission_validity_minutes == 45

    @pytest.mark.parametrize("provider", ["digital_sky", "real", "", "MOCK"])
    def test_only_the_mock_provider_exists(self, provider: str) -> None:
        """A real Digital Sky integration is not built until it is explicitly requested."""
        with pytest.raises(ValidationError):
            make(compliance_provider=provider)

    def test_the_default_validity_outlasts_a_typical_sortie(self) -> None:
        """The start gate refuses a permission that ends before the mission would (~22 min)."""
        assert make().permission_validity_minutes >= 30

    @pytest.mark.parametrize("minutes", ["0", "-5"])
    def test_the_validity_must_be_positive(self, minutes: str) -> None:
        with pytest.raises(ValidationError):
            make(permission_validity_minutes=minutes)


class TestDroneLink:
    def test_only_the_in_memory_link_exists(self) -> None:
        """The MQTT link is built with telemetry; nothing else may be selected until then."""
        for link in ("mqtt", "real", "", "IN_MEMORY"):
            with pytest.raises(ValidationError):
                make(drone_link=link)

    def test_it_comes_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRONE_LINK", "in_memory")

        assert make().drone_link == "in_memory"


class TestValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@localhost/db",
            "postgresql+psycopg://u:p@localhost/db",
            "sqlite+aiosqlite:///x.db",
            "",
        ],
    )
    def test_the_database_url_must_use_the_asyncpg_driver(self, url: str) -> None:
        with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
            make(database_url=url)

    @pytest.mark.parametrize("size", ["0", "-1"])
    def test_pool_size_must_be_positive(self, size: str) -> None:
        with pytest.raises(ValidationError):
            make(database_pool_size=size)

    def test_overflow_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            make(database_max_overflow="-1")

    def test_log_level_must_be_a_known_level(self) -> None:
        with pytest.raises(ValidationError):
            make(log_level="LOUD")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
