"""Looks up the adapter for a client id."""

from __future__ import annotations

from collections.abc import Iterable

from app.adapters.base import ClientAdapter
from app.adapters.mock_client import MockClientAdapter
from app.adapters.swiggy import SwiggyAdapter
from app.adapters.zomato import ZomatoAdapter


class UnknownClientError(LookupError):
    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        super().__init__(f"no adapter registered for client '{client_id}'")


class AdapterRegistry:
    def __init__(self, adapters: Iterable[ClientAdapter] = ()) -> None:
        self._adapters: dict[str, ClientAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ClientAdapter) -> None:
        if adapter.client_id in self._adapters:
            raise ValueError(f"an adapter for client '{adapter.client_id}' is already registered")
        self._adapters[adapter.client_id] = adapter

    def get(self, client_id: str) -> ClientAdapter:
        try:
            return self._adapters[client_id]
        except KeyError:
            raise UnknownClientError(client_id) from None

    def client_ids(self) -> list[str]:
        return sorted(self._adapters)


def default_registry() -> AdapterRegistry:
    return AdapterRegistry([MockClientAdapter(), SwiggyAdapter(), ZomatoAdapter()])
