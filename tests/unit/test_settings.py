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
            "MQTT_URL",
            "MQTT_ACK_TIMEOUT_SECONDS",
            "MQTT_WORKER_CLIENT_ID",
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
        assert settings.mqtt_url == "mqtt://localhost:1883"
        assert settings.mqtt_ack_timeout_seconds == 10.0
        assert settings.mqtt_worker_client_id == "telemetry-worker"


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
    @pytest.mark.parametrize("link", ["in_memory", "mqtt"])
    def test_the_in_memory_stand_in_and_mqtt_are_the_two_links(self, link: str) -> None:
        assert make(drone_link=link).drone_link == link

    @pytest.mark.parametrize("link", ["real", "http", "", "IN_MEMORY", "MQTT"])
    def test_nothing_else_may_be_selected(self, link: str) -> None:
        with pytest.raises(ValidationError):
            make(drone_link=link)

    def test_it_comes_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DRONE_LINK", "mqtt")

        assert make().drone_link == "mqtt"


class TestMqtt:
    def test_values_come_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_URL", "mqtt://mosquitto:1884")
        monkeypatch.setenv("MQTT_ACK_TIMEOUT_SECONDS", "2.5")
        monkeypatch.setenv("MQTT_WORKER_CLIENT_ID", "worker-7")

        settings = make()

        assert settings.mqtt_url == "mqtt://mosquitto:1884"
        assert settings.mqtt_ack_timeout_seconds == 2.5
        assert settings.mqtt_worker_client_id == "worker-7"

    @pytest.mark.parametrize(
        "url", ["localhost:1883", "http://x", "mqtts://x", "mqtt://", "tcp://x:1883", ""]
    )
    def test_the_url_must_be_a_plain_mqtt_url(self, url: str) -> None:
        """No TLS or credentials have been requested, so nothing else is accepted."""
        with pytest.raises(ValidationError, match="mqtt://"):
            make(mqtt_url=url)

    @pytest.mark.parametrize("seconds", ["0", "-1"])
    def test_the_ack_timeout_must_be_positive(self, seconds: str) -> None:
        with pytest.raises(ValidationError):
            make(mqtt_ack_timeout_seconds=seconds)


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


class TestLiveView:
    def test_it_is_off_by_default_and_the_thresholds_are_sensible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in (
            "REDIS_URL",
            "LIVE_STATE_TTL_SECONDS",
            "LINK_LOST_AFTER_SECONDS",
            "LOW_BATTERY_PERCENT",
            "STATE_MISMATCH_GRACE_SECONDS",
            "BATTERY_SYNC_STEP_PERCENT",
            "STALE_RESERVATION_MINUTES",
            "WATCHDOG_INTERVAL_SECONDS",
            "REAPER_INTERVAL_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)

        settings = make()

        assert settings.redis_url is None
        assert settings.live_state_ttl_seconds == 60
        assert settings.link_lost_after_seconds == 15.0
        assert settings.low_battery_percent == 20.0
        assert settings.state_mismatch_grace_seconds == 30.0
        assert settings.battery_sync_step_percent == 5.0
        assert settings.stale_reservation_minutes == 5
        assert settings.watchdog_interval_seconds == 5.0
        assert settings.reaper_interval_seconds == 60.0

    def test_the_link_is_declared_lost_before_the_live_state_expires(self) -> None:
        """Else a silent drone would simply vanish from the view instead of raising an alert."""
        settings = make()

        assert settings.link_lost_after_seconds < settings.live_state_ttl_seconds

    def test_values_come_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://cache:6380/2")
        monkeypatch.setenv("STALE_RESERVATION_MINUTES", "10")
        monkeypatch.setenv("LOW_BATTERY_PERCENT", "30")

        settings = make()

        assert settings.redis_url == "redis://cache:6380/2"
        assert settings.stale_reservation_minutes == 10
        assert settings.low_battery_percent == 30.0

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_url_means_no_redis(self, blank: str) -> None:
        assert make(redis_url=blank).redis_url is None

    @pytest.mark.parametrize("url", ["localhost:6379", "http://x", "tcp://x:6379", "memcached://x"])
    def test_the_url_must_be_a_redis_url(self, url: str) -> None:
        with pytest.raises(ValidationError, match="redis://"):
            make(redis_url=url)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("live_state_ttl_seconds", "0"),
            ("link_lost_after_seconds", "0"),
            ("low_battery_percent", "101"),
            ("low_battery_percent", "-1"),
            ("state_mismatch_grace_seconds", "-1"),
            ("battery_sync_step_percent", "0"),
            ("stale_reservation_minutes", "0"),
            ("watchdog_interval_seconds", "0"),
            ("reaper_interval_seconds", "0"),
        ],
    )
    def test_nonsense_values_are_refused(self, name: str, value: str) -> None:
        with pytest.raises(ValidationError):
            make(**{name: value})


class TestWebhooks:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("WEBHOOK_ENDPOINTS", "WEBHOOK_TIMEOUT_SECONDS", "WEBHOOK_MAX_ATTEMPTS"):
            monkeypatch.delenv(name, raising=False)

        settings = make()

        assert settings.webhook_endpoints == {}
        assert settings.webhook_timeout_seconds == 10.0
        assert settings.webhook_connect_timeout_seconds == 3.0
        assert settings.webhook_max_attempts == 8
        assert settings.webhook_backoff_base_seconds == 5.0
        assert settings.webhook_backoff_factor == 3.0
        assert settings.webhook_backoff_cap_seconds == 3600.0
        assert settings.webhook_backoff_jitter == 0.2
        assert settings.webhook_lease_seconds == 60.0
        assert settings.webhook_poll_seconds == 1.0
        assert settings.webhook_batch_size == 20
        assert settings.webhook_concurrency == 10

    def test_endpoints_are_read_from_json_in_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "WEBHOOK_ENDPOINTS",
            '{"mock": {"url": "https://a.example/h", "secret": "s3cret"},'
            ' "zomato": {"url": "http://127.0.0.1:9000/hook"}}',
        )

        settings = make()

        assert set(settings.webhook_endpoints) == {"mock", "zomato"}
        mock = settings.webhook_endpoints["mock"]
        assert mock.url == "https://a.example/h"
        assert mock.secret is not None
        assert mock.secret.get_secret_value() == "s3cret"
        assert settings.webhook_endpoints["zomato"].secret is None

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_a_blank_value_means_no_endpoints(
        self, monkeypatch: pytest.MonkeyPatch, blank: str
    ) -> None:
        """What ``WEBHOOK_ENDPOINTS=`` in an env file, or compose's ``${X:-}``, produces."""
        monkeypatch.setenv("WEBHOOK_ENDPOINTS", blank)

        assert make().webhook_endpoints == {}

    def test_endpoints_can_be_passed_directly(self) -> None:
        settings = make(webhook_endpoints={"mock": {"url": "https://a.example/h"}})  # type: ignore[arg-type]

        assert settings.webhook_endpoints["mock"].url == "https://a.example/h"

    @pytest.mark.parametrize(
        "bad",
        [
            "{not json",
            "[1, 2]",
            '"a string"',
            '{"mock": "https://a.example/h"}',  # needs an object with a url
            '{"mock": {"url": "ftp://a.example/h"}}',
            '{"mock": {"url": "https://user:pw@a.example/h"}}',
            '{"mock": {"url": "https://a.example/h", "scret": "typo"}}',
            '{"Mock": {"url": "https://a.example/h"}}',  # not a client id
            '{"": {"url": "https://a.example/h"}}',
            '{"-mock": {"url": "https://a.example/h"}}',
            '{"mo ck": {"url": "https://a.example/h"}}',
        ],
    )
    def test_nonsense_is_refused(self, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
        monkeypatch.setenv("WEBHOOK_ENDPOINTS", bad)

        with pytest.raises(ValidationError):
            make()

    def test_malformed_json_says_what_was_expected_and_where_it_went_wrong(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WEBHOOK_ENDPOINTS", '{"mock": {"url": ')

        with pytest.raises(ValidationError) as caught:
            make()

        message = str(caught.value)
        assert (
            'WEBHOOK_ENDPOINTS must be JSON like {"<client_id>": {"url": "https://..."}}' in message
        )
        assert "position" in message

    def test_a_refusal_never_echoes_a_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "WEBHOOK_ENDPOINTS",
            '{"Mock": {"url": "https://a.example/h", "secret": "TOPSECRET"}}',
        )

        with pytest.raises(ValidationError) as caught:
            make()

        assert "TOPSECRET" not in str(caught.value)
        assert "TOPSECRET" not in repr(caught.value)

    def test_a_secret_is_hidden_when_the_settings_are_printed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "WEBHOOK_ENDPOINTS", '{"mock": {"url": "https://a.example/h", "secret": "TOPSECRET"}}'
        )

        settings = make()

        assert "TOPSECRET" not in repr(settings)
        assert "TOPSECRET" not in str(settings.model_dump())

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("WEBHOOK_TIMEOUT_SECONDS", "0"),
            ("WEBHOOK_CONNECT_TIMEOUT_SECONDS", "-1"),
            ("WEBHOOK_MAX_ATTEMPTS", "0"),
            ("WEBHOOK_BACKOFF_BASE_SECONDS", "0"),
            ("WEBHOOK_BACKOFF_FACTOR", "0.5"),
            ("WEBHOOK_BACKOFF_CAP_SECONDS", "0"),
            ("WEBHOOK_BACKOFF_JITTER", "1"),
            ("WEBHOOK_BACKOFF_JITTER", "-0.1"),
            ("WEBHOOK_LEASE_SECONDS", "0"),
            ("WEBHOOK_POLL_SECONDS", "0"),
            ("WEBHOOK_BATCH_SIZE", "0"),
            ("WEBHOOK_CONCURRENCY", "0"),
        ],
    )
    def test_the_numbers_are_bounded(
        self, monkeypatch: pytest.MonkeyPatch, name: str, value: str
    ) -> None:
        monkeypatch.setenv(name, value)

        with pytest.raises(ValidationError):
            make()

    def test_the_lease_must_outlast_a_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Or a slow partner would be sent the same update again while the first is in flight."""
        monkeypatch.setenv("WEBHOOK_TIMEOUT_SECONDS", "30")
        monkeypatch.setenv("WEBHOOK_LEASE_SECONDS", "30")

        with pytest.raises(ValidationError, match="longer than webhook_timeout_seconds"):
            make()

        monkeypatch.setenv("WEBHOOK_LEASE_SECONDS", "31")
        assert make().webhook_lease_seconds == 31


class TestTheDocumentedConfiguration:
    ROOT = Path(__file__).resolve().parents[2]

    def test_every_webhook_setting_is_in_the_env_example(self) -> None:
        example = (self.ROOT / ".env.example").read_text()

        missing = [
            name.upper()
            for name in Settings.model_fields
            if name.startswith("webhook_") and name.upper() not in example
        ]

        assert missing == []

    def test_the_env_example_values_are_the_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An example that disagrees with the code is worse than none."""
        for line in (self.ROOT / ".env.example").read_text().splitlines():
            name, _, value = line.partition("=")
            if name.startswith("WEBHOOK_") and name != "WEBHOOK_ENDPOINTS":
                monkeypatch.setenv(name, value)

        from_example = make().model_dump(exclude={"webhook_endpoints"})
        monkeypatch.undo()
        defaults = make().model_dump(exclude={"webhook_endpoints"})

        assert {k: v for k, v in from_example.items() if k.startswith("webhook_")} == {
            k: v for k, v in defaults.items() if k.startswith("webhook_")
        }

    def test_the_documented_endpoint_example_is_valid(self) -> None:
        example = next(
            line
            for line in (self.ROOT / ".env.example").read_text().splitlines()
            if line.startswith("# WEBHOOK_ENDPOINTS=")
        )

        settings = make(webhook_endpoints=example.removeprefix("# WEBHOOK_ENDPOINTS="))  # type: ignore[arg-type]

        assert list(settings.webhook_endpoints) == ["mock"]

    def test_compose_runs_the_documented_sender_as_an_opt_in_service(self) -> None:
        compose = (self.ROOT / "docker-compose.yml").read_text()
        service = compose.split("\n  webhooks:\n", 1)[1].split("\n  simulator:", 1)[0]

        assert 'command: ["python", "-m", "app.webhooks"]' in service
        assert 'profiles: ["webhooks"]' in service  # not started by a plain `docker compose up`
        assert "WEBHOOK_ENDPOINTS" in service
        assert "ports:" not in service  # it serves nothing
