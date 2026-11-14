"""Signing a webhook request, so a partner can tell it came from this platform.

Optional: a request is signed only when the partner's endpoint has a secret. The signature is
HMAC-SHA256 over ``"<timestamp>.<body>"`` with the shared secret, sent as::

    X-Timestamp: 1767225600
    X-Signature: sha256=<hex>

A partner recomputes it from the raw body and the timestamp header, compares in constant time,
and should reject a timestamp that is too old (a replayed request). The timestamp is fresh on
every attempt; the body, and so the event id, is the same.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Timestamp"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: int, body: bytes, signature: str) -> bool:
    """What a receiver does (kept here so the sender's tests and the demo can use it too)."""
    return hmac.compare_digest(sign(secret, timestamp, body), signature)
