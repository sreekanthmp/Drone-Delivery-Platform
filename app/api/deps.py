"""FastAPI dependencies. Everything is wired in ``app.main.create_app`` and read from app state."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.adapters.registry import AdapterRegistry
from app.services.order_service import OrderService


def get_order_service(request: Request) -> OrderService:
    service: OrderService = request.app.state.order_service
    return service


def get_adapter_registry(request: Request) -> AdapterRegistry:
    registry: AdapterRegistry = request.app.state.adapter_registry
    return registry


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
AdapterRegistryDep = Annotated[AdapterRegistry, Depends(get_adapter_registry)]
