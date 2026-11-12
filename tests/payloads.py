"""Sample partner payloads and small helpers to tweak them (always on deep copies)."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any


def mock_payload() -> dict[str, Any]:
    return {
        "order_id": "ORD-1",
        "pickup": {"lat": 12.9716, "lng": 77.5946},
        "dropoff": {"lat": 12.9352, "lng": 77.6245},
        "package": {"weight_kg": 1.5, "description": "2 meals"},
    }


def swiggy_payload() -> dict[str, Any]:
    return {
        "swiggy_order_id": "SW-100234",
        "restaurant": {"location": {"latitude": 12.9716, "longitude": 77.5946}},
        "customer": {"address": {"latitude": 12.9352, "longitude": 77.6245}},
        "items": [
            {"name": "Biryani", "quantity": 2, "weight_grams": 450},
            {"name": "Raita", "quantity": 1, "weight_grams": 200},
        ],
    }


def zomato_payload() -> dict[str, Any]:
    return {
        "order": {
            "id": 99812,
            "pickup_point": {"coordinates": [77.5946, 12.9716]},
            "drop_point": {"coordinates": [77.6245, 12.9352]},
            "package_weight_grams": 1200,
            "tags": ["fragile", "hot"],
        }
    }


VALID_PAYLOADS: dict[str, Callable[[], dict[str, Any]]] = {
    "mock": mock_payload,
    "swiggy": swiggy_payload,
    "zomato": zomato_payload,
}

# The partner's own id for each sample payload, as it is stored as external_order_id.
EXTERNAL_IDS = {"mock": "ORD-1", "swiggy": "SW-100234", "zomato": "99812"}


def _walk(data: Any, parts: list[str]) -> Any:
    for part in parts:
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def set_path(payload: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Copy of ``payload`` with ``path`` (dotted, list indexes as numbers) set to ``value``."""
    result = deepcopy(payload)
    *parents, last = path.split(".")
    holder = _walk(result, parents)
    if isinstance(holder, list):
        holder[int(last)] = value
    else:
        holder[last] = value
    return result


def drop_path(payload: dict[str, Any], path: str) -> dict[str, Any]:
    """Copy of ``payload`` with ``path`` removed."""
    result = deepcopy(payload)
    *parents, last = path.split(".")
    holder = _walk(result, parents)
    if isinstance(holder, list):
        del holder[int(last)]
    else:
        del holder[last]
    return result
