"""Errors raised by the dispatch layer.

Domain rule violations (an invalid flight transition, a refused start permission) stay
:class:`app.domain.exceptions.DomainError` subclasses and pass through untouched.
"""

from __future__ import annotations

from uuid import UUID

from app.domain.models import OrderStatus
from app.services.exceptions import ServiceError


class DispatchError(ServiceError):
    """Base class for dispatch-layer errors."""


class DroneNotFoundError(DispatchError):
    def __init__(self, drone_id: UUID) -> None:
        self.drone_id = drone_id
        super().__init__(f"Drone {drone_id} not found")


class FlightNotFoundError(DispatchError):
    def __init__(self, flight_id: UUID) -> None:
        self.flight_id = flight_id
        super().__init__(f"Flight {flight_id} not found")


class DuplicateDroneError(DispatchError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"A drone named '{name}' is already registered")


class OrderNotDispatchableError(DispatchError):
    """Only an order that is still ``created`` can be dispatched."""

    def __init__(self, order_id: UUID, status: OrderStatus) -> None:
        self.order_id = order_id
        self.status = status
        super().__init__(f"Order {order_id} is '{status}' and cannot be dispatched")


class FlightInProgressError(DispatchError):
    """The order (or the chosen drone) already has a live flight."""

    def __init__(self, order_id: UUID, flight_id: UUID | None = None) -> None:
        self.order_id = order_id
        self.flight_id = flight_id
        detail = f" (flight {flight_id})" if flight_id is not None else ""
        super().__init__(f"Order {order_id} already has a live flight{detail}")


class DroneBusyError(DispatchError):
    """The drone has a live flight, so its status cannot be changed by hand."""

    def __init__(self, drone_id: UUID, flight_id: UUID) -> None:
        self.drone_id = drone_id
        self.flight_id = flight_id
        super().__init__(f"Drone {drone_id} is busy with live flight {flight_id}")


class CommandNotConfirmedError(DispatchError):
    """A safety command (pause, return-to-home, land) could not be confirmed by the drone.

    Nothing was recorded; the caller should retry. For a cancellation this means the drone
    could not be recalled, so the order was deliberately *not* cancelled.
    """

    def __init__(self, command: str, flight_id: UUID, cause: str) -> None:
        self.command = command
        self.flight_id = flight_id
        super().__init__(f"The drone did not confirm '{command}' for flight {flight_id}: {cause}")


class StartCommandFailedError(DispatchError):
    """The start (or resume) command failed after the intent was recorded.

    ``unconfirmed`` is True when the drone may nonetheless have acted (a return-to-home was
    sent as a precaution). ``flight_aborted`` says what was done about it:

    * a failed *start*, or an unconfirmed resume: the flight was aborted, the drone released
      or recalled, and the order failed;
    * a *refused* resume: the drone never left its hover, so the flight was put back to
      ``paused`` (``flight_aborted`` is False) and may be resumed again or recalled.
    """

    def __init__(
        self,
        flight_id: UUID,
        *,
        unconfirmed: bool,
        cause: str,
        flight_aborted: bool = True,
    ) -> None:
        self.flight_id = flight_id
        self.unconfirmed = unconfirmed
        self.flight_aborted = flight_aborted
        how = "was not acknowledged" if unconfirmed else "was refused"
        super().__init__(f"The start command for flight {flight_id} {how}: {cause}")
