"""Where a partner's webhooks go. Operator configuration, keyed by client id."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator


class WebhookEndpoint(BaseModel):
    """One partner's webhook endpoint.

    ``secret`` is optional. When it is set, every request is signed (see ``signing.py``) so the
    partner can check it came from this platform; without one, requests are unsigned.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    secret: SecretStr | None = None

    @field_validator("url")
    @classmethod
    def _plain_http_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("a webhook url must be an absolute http:// or https:// URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("a webhook url must not contain credentials")
        return value.strip()

    @field_validator("secret")
    @classmethod
    def _empty_means_unsigned(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value():
            return None
        return value
