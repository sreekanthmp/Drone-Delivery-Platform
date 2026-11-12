"""Adapter registry."""

from __future__ import annotations

import pytest

from app.adapters.mock_client import MockClientAdapter
from app.adapters.registry import AdapterRegistry, UnknownClientError, default_registry
from app.adapters.swiggy import SwiggyAdapter
from app.adapters.zomato import ZomatoAdapter


def test_default_registry_has_the_three_integrations() -> None:
    registry = default_registry()

    assert registry.client_ids() == ["mock", "swiggy", "zomato"]
    assert isinstance(registry.get("mock"), MockClientAdapter)
    assert isinstance(registry.get("swiggy"), SwiggyAdapter)
    assert isinstance(registry.get("zomato"), ZomatoAdapter)


def test_unknown_clients_raise() -> None:
    with pytest.raises(UnknownClientError) as exc_info:
        default_registry().get("nobody")

    assert exc_info.value.client_id == "nobody"
    assert "nobody" in str(exc_info.value)


@pytest.mark.parametrize("client_id", ["", "Swiggy", "SWIGGY", " swiggy", "swiggy ", "mock/x"])
def test_lookup_is_exact(client_id: str) -> None:
    with pytest.raises(UnknownClientError):
        default_registry().get(client_id)


def test_registering_a_second_adapter_for_the_same_client_is_refused() -> None:
    registry = AdapterRegistry([MockClientAdapter()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(MockClientAdapter())


def test_an_empty_registry_knows_nobody() -> None:
    registry = AdapterRegistry()
    assert registry.client_ids() == []
    with pytest.raises(UnknownClientError):
        registry.get("mock")


def test_registries_are_independent() -> None:
    first, second = AdapterRegistry([MockClientAdapter()]), AdapterRegistry()
    assert first.client_ids() == ["mock"]
    assert second.client_ids() == []
