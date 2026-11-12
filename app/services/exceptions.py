"""Errors raised by the application-service layer.

Domain rule violations stay :class:`app.domain.exceptions.DomainError` subclasses
and pass through the services untouched.
"""

from __future__ import annotations

from uuid import UUID


class ServiceError(Exception):
    """Base class for application-service errors."""


class OrderNotFoundError(ServiceError):
    """No order matches the given internal id, or the given ``(client, external id)`` pair."""

    def __init__(
        self,
        order_id: UUID | None = None,
        *,
        client_id: str | None = None,
        external_order_id: str | None = None,
    ) -> None:
        self.order_id = order_id
        self.client_id = client_id
        self.external_order_id = external_order_id
        what = (
            f"Order {order_id}"
            if order_id is not None
            else f"Order {client_id}/{external_order_id}"
        )
        super().__init__(f"{what} not found")


class ConcurrentUpdateError(ServiceError):
    """The order changed underneath us (its stored status was not what we expected).

    Row locking makes this rare; it is the compare-and-set safety net.
    """
