"""Domain exceptions.

Every error raised by the domain layer derives from :class:`DomainError`, so
callers at the edges (API, adapters, workers) can translate them explicitly
instead of catching broad exceptions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.models.order import OrderStatus


class DomainError(Exception):
    """Base class for every error raised by the domain layer."""


class OrderValidationError(DomainError):
    """An order or one of its value objects violates a business invariant."""


class AuditEventValidationError(DomainError):
    """An audit event is malformed or internally inconsistent."""


class InvalidTransitionError(DomainError):
    """The requested order status change is not allowed by the state machine."""

    def __init__(
        self,
        current: OrderStatus,
        target: OrderStatus,
        message: str | None = None,
    ) -> None:
        self.current = current
        self.target = target
        super().__init__(message or f"Invalid order transition: {current} -> {target}")


class TerminalStateError(InvalidTransitionError):
    """The order is already in a terminal state and cannot change any more."""

    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(
            current,
            target,
            f"Order is in terminal state '{current}' and cannot move to '{target}'",
        )


class MissingTransitionContextError(DomainError):
    """A transition is valid but the caller did not supply data it requires."""


class InconsistentTransitionResultError(DomainError):
    """A transition result pairs an order with an audit event that does not match it."""
