"""Domain exceptions.

Every error raised by the domain layer derives from :class:`DomainError`, so
callers at the edges (API, adapters, workers) can translate them explicitly
instead of catching broad exceptions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.models.drone import DroneStatus
    from app.domain.models.flight import FlightStatus
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


class TrackingValidationError(DomainError):
    """A tracking position is malformed."""


# --- drones --------------------------------------------------------------------------------


class DroneValidationError(DomainError):
    """A drone violates a business invariant."""


class InvalidDroneTransitionError(DomainError):
    """The requested drone status change is not allowed."""

    def __init__(self, current: DroneStatus, target: DroneStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid drone transition: {current} -> {target}")


# --- missions and permissions --------------------------------------------------------------


class MissionValidationError(DomainError):
    """A mission is malformed (it is data for the drone's own flight controller)."""


class PermissionValidationError(DomainError):
    """A flight permission, denial or permission request is malformed."""


class MissionChecksumMismatchError(DomainError):
    """The drone acknowledged a different mission than the one the platform sent."""


# --- flights -------------------------------------------------------------------------------


class FlightValidationError(DomainError):
    """A flight violates a business invariant, or a transition was given wrong context."""


class InvalidFlightTransitionError(DomainError):
    """The requested flight status change is not allowed by the flight state machine."""

    def __init__(
        self,
        current: FlightStatus,
        target: FlightStatus,
        message: str | None = None,
    ) -> None:
        self.current = current
        self.target = target
        super().__init__(message or f"Invalid flight transition: {current} -> {target}")


class TerminalFlightStateError(InvalidFlightTransitionError):
    """The flight has already finished and cannot change any more."""

    def __init__(self, current: FlightStatus, target: FlightStatus) -> None:
        super().__init__(
            current,
            target,
            f"Flight is in terminal state '{current}' and cannot move to '{target}'",
        )


class FlightPermissionError(DomainError):
    """A flight may not start (or resume): its permission is not valid (Rule 2).

    Subclasses say exactly why. Nothing is persisted when one of these is raised.
    """


class PermissionNotGrantedError(FlightPermissionError):
    """The flight has no granted permission."""


class PermissionInvalidError(FlightPermissionError):
    """The compliance provider does not recognise the permission, or answered about another."""


class PermissionRevokedError(FlightPermissionError):
    """The compliance provider has revoked the permission."""


class PermissionExpiredError(FlightPermissionError):
    """The permission's validity period has ended."""


class PermissionWindowTooShortError(FlightPermissionError):
    """The permission is still valid but ends before the mission would finish."""


class PermissionCheckStaleError(FlightPermissionError):
    """The compliance re-check is too old (or missing) to rely on."""


class WebhookDeliveryError(DomainError):
    """A webhook delivery is malformed, or the requested change is not allowed for it."""
