"""FastAPI dependencies. Everything is wired in ``app.main.create_app`` and read from app state."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.adapters.registry import AdapterRegistry
from app.dispatch.service import DispatchService
from app.services.order_service import OrderService
from app.services.webhook_service import WebhookService


def get_order_service(request: Request) -> OrderService:
    service: OrderService = request.app.state.order_service
    return service


def get_dispatch_service(request: Request) -> DispatchService:
    service: DispatchService = request.app.state.dispatch_service
    return service


def get_webhook_service(request: Request) -> WebhookService:
    service: WebhookService = request.app.state.webhook_service
    return service


def get_adapter_registry(request: Request) -> AdapterRegistry:
    registry: AdapterRegistry = request.app.state.adapter_registry
    return registry


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
DispatchServiceDep = Annotated[DispatchService, Depends(get_dispatch_service)]
AdapterRegistryDep = Annotated[AdapterRegistry, Depends(get_adapter_registry)]
WebhookServiceDep = Annotated[WebhookService, Depends(get_webhook_service)]
