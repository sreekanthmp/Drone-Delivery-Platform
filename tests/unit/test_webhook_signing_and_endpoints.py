"""Signing a webhook, and the shape of a partner's endpoint configuration."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from pydantic import ValidationError

from app.webhooks.endpoints import WebhookEndpoint
from app.webhooks.signing import sign, verify

BODY = b'{"order_id":"ORD-1","status":"created"}'


class TestSigning:
    def test_it_is_hmac_sha256_over_timestamp_dot_body(self) -> None:
        expected = hmac.new(b"key", b"1700000000." + BODY, hashlib.sha256).hexdigest()

        assert sign("key", 1700000000, BODY) == f"sha256={expected}"

    def test_a_receiver_can_verify_what_was_sent(self) -> None:
        assert verify("key", 1700000000, BODY, sign("key", 1700000000, BODY))

    @pytest.mark.parametrize(
        ("secret", "timestamp", "body"),
        [
            ("other", 1700000000, BODY),  # wrong secret
            ("key", 1700000001, BODY),  # replayed with another timestamp
            ("key", 1700000000, BODY + b" "),  # tampered body
        ],
    )
    def test_anything_changed_fails_verification(
        self, secret: str, timestamp: int, body: bytes
    ) -> None:
        assert not verify(secret, timestamp, body, sign("key", 1700000000, BODY))

    def test_a_garbage_signature_fails_cleanly(self) -> None:
        assert not verify("key", 1700000000, BODY, "")
        assert not verify("key", 1700000000, BODY, "sha256=zz")


class TestEndpoints:
    def test_an_https_url_is_fine(self) -> None:
        endpoint = WebhookEndpoint(url="https://partner.example/hooks/orders?v=2")

        assert endpoint.url == "https://partner.example/hooks/orders?v=2"
        assert endpoint.secret is None

    def test_plain_http_is_allowed_for_development(self) -> None:
        assert WebhookEndpoint(url="http://127.0.0.1:9000/hook").url == "http://127.0.0.1:9000/hook"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "partner.example/hook",
            "ftp://partner.example/hook",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://",
            "//partner.example/hook",
            "https://user:pass@partner.example/hook",
            "https://user@partner.example/hook",
        ],
    )
    def test_anything_but_a_plain_absolute_http_url_is_refused(self, url: str) -> None:
        with pytest.raises(ValidationError):
            WebhookEndpoint(url=url)

    def test_a_secret_is_hidden_when_printed(self) -> None:
        endpoint = WebhookEndpoint(url="https://p.example/h", secret="hunter2")  # type: ignore[arg-type]

        assert "hunter2" not in repr(endpoint)
        assert "hunter2" not in str(endpoint.model_dump())
        assert endpoint.secret is not None
        assert endpoint.secret.get_secret_value() == "hunter2"

    def test_an_empty_secret_means_unsigned(self) -> None:
        assert WebhookEndpoint(url="https://p.example/h", secret="").secret is None  # type: ignore[arg-type]

    def test_unknown_fields_are_refused_so_a_typo_is_not_silently_ignored(self) -> None:
        with pytest.raises(ValidationError):
            WebhookEndpoint(url="https://p.example/h", scret="x")  # type: ignore[call-arg]

    def test_it_is_immutable(self) -> None:
        endpoint = WebhookEndpoint(url="https://p.example/h")

        with pytest.raises(ValidationError):
            endpoint.url = "https://elsewhere.example/h"  # type: ignore[misc]
