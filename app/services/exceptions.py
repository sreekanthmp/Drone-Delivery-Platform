"""Errors raised by the application-service layer.

Domain rule violations stay :class:`app.domain.exceptions.DomainError` subclasses
and pass through the services untouched.
"""

from __future__ import annotations

from uuid import UUID


class ServiceError(Exception):
    """Base class for application-service errors."""


class OrderNotFoundError(ServiceError):
    def __init__(self, order_id: UUID) -> None:
        self.order_id = order_id
        super().__init__(f"Order {order_id} not found")


class ConcurrentUpdateError(ServiceError):
    """The order changed underneath us (its stored status was not what we expected).

    Row locking makes this rare; it is the compare-and-set safety net.
    """
