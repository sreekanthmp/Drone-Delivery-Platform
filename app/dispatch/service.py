"""The dispatch service: the Rule 2 workflow, the high-level drone commands, cancellation.

This is the only module allowed to write flights and drones, and the only one allowed to
call the drone link. Everything it does to an order goes through :class:`OrderService`.

The workflow (Rule 2), each step its own short transaction (no transaction is ever held
open across a network call, and every step re-reads the flight under a lock):

1. pick a suitable drone, claim it (compare-and-set), plan the mission and create the
   planned flight;
2. request permission from the compliance provider;
3. validate the granted permission;
4. upload the mission, which the drone must acknowledge by checksum;
5. only now move the order ``created -> assigned`` (so ``assigned`` means *ready to fly*);
6. start the flight, which runs the permission gate (see the flight state machine).

Two command orderings, chosen so that each fails in its safe direction:

* **start and resume** record the intent first, then send the command. A lost command is
  safe: the flight is aborted, a return-to-home is sent as a precaution, the order failed.
* **pause, return-to-home and land** send the command first, then record it. A lost record
  is safe (the drone has stopped); telemetry can reconcile. If the command cannot be
  confirmed nothing is recorded and the caller gets an error. They are never gated.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

import structlog

from app.compliance.interface import (
    ComplianceProvider,
    ComplianceUnavailableError,
    PermissionRefused,
)
from app.dispatch.assignment import AssignmentPolicy, DroneChoice, Rejection, select_drone
from app.dispatch.exceptions import (
    CommandNotConfirmedError,
    DroneBusyError,
    DroneNotFoundError,
    DuplicateDroneError,
    FlightInProgressError,
    FlightNotFoundError,
    OrderNotDispatchableError,
    StartCommandFailedError,
)
from app.dispatch.link import CommandRejectedError, DroneLink, DroneLinkError
from app.dispatch.mission import MissionPlanParams, plan_mission
from app.dispatch.progress import (
    ORDER_STEP,
    ProgressEvent,
    ProgressOutcome,
    ProgressResult,
    at_or_beyond,
)
from app.domain.exceptions import (
    FlightPermissionError,
    InvalidDroneTransitionError,
    InvalidFlightTransitionError,
    InvalidTransitionError,
    PermissionCheckStaleError,
    TerminalFlightStateError,
    TerminalStateError,
)
from app.domain.flight_state_machine import FlightStateMachine, FlightTransitionResult
from app.domain.models import (
    DenialCode,
    Drone,
    DroneStatus,
    EventSource,
    Flight,
    FlightStatus,
    Location,
    Order,
    OrderStatus,
    PermissionDenial,
    PermissionRequest,
    PermissionStatus,
)
from app.services.exceptions import ConcurrentUpdateError, OrderNotFoundError
from app.services.order_service import OrderService
from app.services.ports import UnitOfWork, UnitOfWorkFactory

logger = structlog.get_logger(__name__)

S = FlightStatus
D = DroneStatus

_ASSIGNED = frozenset({D.ASSIGNED})
_MAX_CLAIM_ATTEMPTS = 8
"""How many times one dispatch re-chooses after losing a claim before it gives up (retryable)."""

_ON_MISSION = frozenset({D.ON_MISSION})


class DispatchOutcome(StrEnum):
    STARTED = "started"
    """Assigned and flying: the order is ``assigned``, the flight ``started``."""

    READY = "ready"
    """Staged: the order is ``assigned`` and the mission is on board, but nothing has started."""

    NO_DRONE_AVAILABLE = "no_drone_available"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_INVALID = "permission_invalid"
    UPLOAD_FAILED = "upload_failed"
    START_FAILED = "start_failed"
    """The order had been assigned, the flight could not start, so the order was failed."""

    CANCELLED_DURING_DISPATCH = "cancelled_during_dispatch"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    outcome: DispatchOutcome
    order: Order
    flight: Flight | None = None
    drone: Drone | None = None
    rejections: Mapping[UUID, tuple[Rejection, ...]] = field(default_factory=dict)
    detail: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome in {DispatchOutcome.STARTED, DispatchOutcome.READY}


@dataclass(frozen=True, slots=True)
class CancelResult:
    order: Order
    flight: Flight | None
    """The live flight that was ended by the cancellation, if there was one."""


class _MissionNotAcknowledged(Exception):
    """The drone echoed back something other than the mission that was sent."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DispatchService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        orders: OrderService,
        compliance: ComplianceProvider,
        link: DroneLink,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], UUID] = uuid4,
        policy: AssignmentPolicy | None = None,
        mission_params: MissionPlanParams | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._orders = orders
        self._compliance = compliance
        self._link = link
        self._clock = clock
        self._ids = id_factory
        self._policy = policy or AssignmentPolicy()
        self._mission_params = mission_params or MissionPlanParams()
        self._flights = FlightStateMachine(clock=clock, id_factory=id_factory)

    # ==========================================================================================
    # Fleet
    # ==========================================================================================

    async def register_drone(
        self,
        *,
        name: str,
        home: Location,
        max_payload_kg: float,
        max_range_km: float,
        battery_percent: float,
    ) -> Drone:
        now = self._clock()
        drone = Drone(
            id=self._ids(),
            name=name,
            status=D.AVAILABLE,
            home=home,
            max_payload_kg=max_payload_kg,
            max_range_km=max_range_km,
            battery_percent=battery_percent,
            created_at=now,
            updated_at=now,
        )
        async with self._uow_factory() as uow:
            if not await uow.drones.add(drone):
                raise DuplicateDroneError(drone.name)
            await uow.commit()
        logger.info("drone_registered", drone_id=str(drone.id), name=drone.name)
        return drone

    async def get_drone(self, drone_id: UUID) -> Drone:
        async with self._uow_factory() as uow:
            drone = await uow.drones.get(drone_id)
        if drone is None:
            raise DroneNotFoundError(drone_id)
        return drone

    async def list_drones(self, *, status: DroneStatus | None = None) -> list[Drone]:
        async with self._uow_factory() as uow:
            return await uow.drones.list_all(status=status)

    async def confirm_docked(self, drone_id: UUID) -> Drone:
        """Operations confirms the drone is back at its dock and may take deliveries again."""
        return await self._set_status_by_hand(drone_id, D.AVAILABLE)

    async def set_maintenance(self, drone_id: UUID) -> Drone:
        """Take a drone out of service (it must not have a live flight)."""
        return await self._set_status_by_hand(drone_id, D.MAINTENANCE)

    async def _set_status_by_hand(self, drone_id: UUID, target: DroneStatus) -> Drone:
        async with self._uow_factory() as uow:
            drone = await uow.drones.get(drone_id, for_update=True)
            if drone is None:
                raise DroneNotFoundError(drone_id)
            live = await uow.flights.get_live_for_drone(drone_id)
            if live is not None:
                raise DroneBusyError(drone_id, live.id)
            if drone.status is target:
                return drone
            after = drone.with_status(target, now=self._clock())  # refuses illegal edges
            await uow.drones.apply_status_change(drone, after)
            await uow.commit()
        logger.info("drone_status_set_by_hand", drone_id=str(drone_id), status=target.value)
        return after

    # ==========================================================================================
    # Reads
    # ==========================================================================================

    async def get_flight(self, flight_id: UUID) -> Flight:
        return await self._require_flight(flight_id)

    async def list_flights_for_order(self, order_id: UUID) -> list[Flight]:
        await self._orders.get_order(order_id)  # raises OrderNotFoundError
        async with self._uow_factory() as uow:
            return await uow.flights.list_for_order(order_id)

    # ==========================================================================================
    # Dispatch (Rule 2)
    # ==========================================================================================

    async def dispatch_order(
        self,
        order_id: UUID,
        *,
        start: bool = True,
        source: EventSource = EventSource.DISPATCH,
    ) -> DispatchResult:
        """Run the Rule 2 workflow for a ``created`` order.

        With ``start=False`` it stops once the order is assigned and the mission is on board
        (``READY``); a later :meth:`start_flight` runs the gate. Every outcome other than
        ``STARTED``/``READY`` leaves the system consistent: see the module docstring and the
        README's outcome table.

        Raises:
            OrderNotFoundError: no such order.
            OrderNotDispatchableError: the order is not ``created``.
            FlightInProgressError: the order already has a live flight.
        """
        reserved = await self._reserve(order_id, source)
        if isinstance(reserved, DispatchResult):
            return reserved
        order, flight, drone = reserved

        try:
            result = await self._run_workflow(order, flight, drone, start=start, source=source)
        except (InvalidFlightTransitionError, ConcurrentUpdateError):
            # The flight was ended underneath us: a cancellation got in between two steps.
            current = await self._flight_or_none(flight.id)
            if current is None or not current.status.is_terminal:
                raise
            result = await self._finish(
                DispatchOutcome.CANCELLED_DURING_DISPATCH,
                order.id,
                flight.id,
                drone.id,
                detail="the order was cancelled while it was being dispatched",
            )
        logger.info(
            "dispatch_outcome",
            order_id=str(order_id),
            flight_id=str(flight.id),
            drone_id=str(drone.id),
            outcome=result.outcome.value,
        )
        return result

    async def _reserve(
        self, order_id: UUID, source: EventSource
    ) -> DispatchResult | tuple[Order, Flight, Drone]:
        """Step 1, one transaction: choose a drone, reserve it, plan the flight."""
        now = self._clock()
        async with self._uow_factory() as uow:
            # Locking the order serialises concurrent dispatch/cancel decisions for it.
            order = await uow.orders.get(order_id, for_update=True)
            if order is None:
                raise OrderNotFoundError(order_id)
            if order.status is not OrderStatus.CREATED:
                raise OrderNotDispatchableError(order.id, order.status)
            live = [f for f in await uow.flights.list_for_order(order.id) if f.status.is_live]
            if live:
                raise FlightInProgressError(order.id, live[0].id)

            claimed = await self._claim_drone(uow, order, now)
            if isinstance(claimed, DispatchResult):
                return claimed
            choice, drone = claimed
            mission = plan_mission(
                order, drone, mission_id=self._ids(), params=self._mission_params
            )
            planned = self._flights.create(
                order_id=order.id,
                drone_id=drone.id,
                mission=mission,
                source=source,
                metadata={
                    "pickup_distance_m": round(choice.pickup_distance_m, 1),
                    "sortie_distance_m": round(choice.sortie_distance_m, 1),
                },
            )
            if not await uow.flights.insert(planned):
                raise FlightInProgressError(order.id)
            await uow.commit()
        logger.info("flight_planned", flight_id=str(planned.flight.id), drone_id=str(drone.id))
        return order, planned.flight, drone

    async def _claim_drone(
        self, uow: UnitOfWork, order: Order, now: datetime
    ) -> DispatchResult | tuple[DroneChoice, Drone]:
        """Choose the best suitable drone and claim it; if another dispatch got it first, re-choose.

        The candidates are read WITHOUT locking and only the chosen drone is claimed, by a
        compare-and-set on its status. (Locking every candidate, as the store can, makes a
        dispatch that is merely *deciding* hide all the drones from the others, which then
        wrongly report "no drone".) Losing a claim means another dispatch took that drone
        meanwhile, so the next read no longer offers it; each lost claim therefore removes one
        drone from the pool and the loop is bounded by the fleet size and a hard cap.
        """
        for _attempt in range(_MAX_CLAIM_ATTEMPTS):
            candidates = await uow.drones.list_available()
            selection = select_drone(order, candidates, self._policy)
            if selection.choice is None:
                return DispatchResult(
                    DispatchOutcome.NO_DRONE_AVAILABLE,
                    order,
                    rejections=selection.rejections,
                    detail="no available drone is suitable for this order",
                )
            drone = selection.choice.drone
            try:
                await uow.drones.apply_status_change(drone, drone.with_status(D.ASSIGNED, now=now))
            except ConcurrentUpdateError:
                logger.info("drone_claim_lost", drone_id=str(drone.id), order_id=str(order.id))
                continue
            return selection.choice, drone
        return DispatchResult(
            DispatchOutcome.NO_DRONE_AVAILABLE,
            order,
            detail="the suitable drones were claimed by other dispatches; try again",
        )

    async def _run_workflow(
        self, order: Order, flight: Flight, drone: Drone, *, start: bool, source: EventSource
    ) -> DispatchResult:
        fid, mission = flight.id, flight.mission

        # 2. request permission --------------------------------------------------------------
        request = PermissionRequest.from_mission(
            flight_id=fid,
            order_id=order.id,
            drone_id=drone.id,
            mission=mission,
            requested_start=self._clock(),
        )
        try:
            decision = await self._compliance.request_permission(request)
        except ComplianceUnavailableError:
            # Fail closed: no answer is no permission.
            decision = PermissionRefused(
                PermissionDenial(
                    DenialCode.PROVIDER_UNAVAILABLE,
                    "the compliance provider is unavailable; failing closed",
                )
            )
        if isinstance(decision, PermissionRefused):
            await self._end_flight(
                fid,
                S.PERMISSION_DENIED,
                source=source,
                denial=decision.denial,
                drone_to=D.AVAILABLE,
                drone_from=_ASSIGNED,
            )
            return await self._finish(
                DispatchOutcome.PERMISSION_DENIED,
                order.id,
                fid,
                drone.id,
                detail=decision.denial.reason,
            )
        permission = decision.permission
        try:
            await self._advance(fid, S.PERMISSION_GRANTED, source=source, permission=permission)
        except FlightPermissionError as exc:  # the provider granted something already expired
            return await self._cancel_unusable(order, flight, drone, source, str(exc))

        # 3. validate the granted permission -------------------------------------------------
        try:
            validation = await self._compliance.validate_permission(permission.permission_id)
        except ComplianceUnavailableError:
            return await self._cancel_unusable(
                order, flight, drone, source, "the compliance provider is unavailable"
            )
        if validation.status is not PermissionStatus.VALID:
            return await self._cancel_unusable(
                order,
                flight,
                drone,
                source,
                f"the permission was {validation.status.value} right after it was granted",
            )

        # 4. upload the mission; the drone must echo the exact checksum ----------------------
        try:
            ack = await self._command(
                "upload", fid, drone.id, lambda: self._link.upload_mission(drone.id, mission)
            )
            if (
                ack.drone_id != drone.id
                or ack.mission_id != mission.mission_id
                or ack.checksum != mission.checksum
            ):
                raise _MissionNotAcknowledged(
                    "the drone acknowledged a different mission than the one sent"
                )
        except (DroneLinkError, _MissionNotAcknowledged) as exc:
            await self._end_flight(
                fid,
                S.UPLOAD_FAILED,
                source=source,
                reason=f"mission upload failed: {exc}",
                drone_to=D.AVAILABLE,
                drone_from=_ASSIGNED,
            )
            return await self._finish(
                DispatchOutcome.UPLOAD_FAILED, order.id, fid, drone.id, detail=str(exc)
            )
        await self._advance(
            fid, S.MISSION_UPLOADED, source=source, acknowledged_checksum=ack.checksum
        )

        # 5. only now assign the order: "assigned" means ready to fly ------------------------
        try:
            await self._orders.transition(
                order.id,
                OrderStatus.ASSIGNED,
                source=source,
                drone_id=drone.id,
                metadata={"flight_id": str(fid)},
            )
        except InvalidTransitionError as exc:  # the order was cancelled (or moved) meanwhile
            await self._end_flight_quietly(
                fid,
                S.CANCELLED,
                source=source,
                reason=f"the order could not be assigned: {exc}",
                drone_to=D.AVAILABLE,
                drone_from=_ASSIGNED,
            )
            return await self._finish(
                DispatchOutcome.CANCELLED_DURING_DISPATCH,
                order.id,
                fid,
                drone.id,
                detail=str(exc),
            )
        if not start:
            return await self._finish(DispatchOutcome.READY, order.id, fid, drone.id)

        # 6. start: the gate -----------------------------------------------------------------
        try:
            await self._start(fid, expected=S.MISSION_UPLOADED, source=source)
        except FlightPermissionError as exc:
            reason = f"the flight could not start: {exc}"
            await self._end_flight_quietly(
                fid,
                S.CANCELLED,
                source=source,
                reason=reason,
                drone_to=D.AVAILABLE,
                drone_from=_ASSIGNED,
            )
            await self._fail_order(order.id, reason, fid, source)
            return await self._finish(
                DispatchOutcome.START_FAILED, order.id, fid, drone.id, detail=reason
            )
        except StartCommandFailedError as exc:
            return await self._finish(
                DispatchOutcome.START_FAILED, order.id, fid, drone.id, detail=str(exc)
            )
        return await self._finish(DispatchOutcome.STARTED, order.id, fid, drone.id)

    async def _cancel_unusable(
        self, order: Order, flight: Flight, drone: Drone, source: EventSource, why: str
    ) -> DispatchResult:
        """A permission was granted but cannot be used: cancel the flight, free the drone."""
        await self._end_flight(
            flight.id,
            S.CANCELLED,
            source=source,
            reason=f"the granted permission was unusable: {why}",
            drone_to=D.AVAILABLE,
            drone_from=_ASSIGNED,
        )
        return await self._finish(
            DispatchOutcome.PERMISSION_INVALID, order.id, flight.id, drone.id, detail=why
        )

    # ==========================================================================================
    # Commands
    # ==========================================================================================

    async def start_flight(
        self, flight_id: UUID, *, source: EventSource = EventSource.OPS
    ) -> Flight:
        """Start a staged flight (``mission_uploaded``). Runs the Rule 2 gate.

        Raises ``FlightPermissionError`` (nothing is persisted) if the gate refuses, and
        ``StartCommandFailedError`` if the drone did not take the command, after the flight
        has been aborted, the drone freed or recalled, and the order failed.
        """
        return await self._start(flight_id, expected=S.MISSION_UPLOADED, source=source)

    async def resume_flight(
        self, flight_id: UUID, *, source: EventSource = EventSource.OPS
    ) -> Flight:
        """Resume a paused flight. Runs the same gate as a first start."""
        return await self._start(flight_id, expected=S.PAUSED, source=source)

    async def _start(
        self, flight_id: UUID, *, expected: FlightStatus, source: EventSource
    ) -> Flight:
        flight = await self._require_flight(flight_id)
        if flight.status is not expected:
            raise InvalidFlightTransitionError(flight.status, S.STARTED)
        assert flight.permission is not None  # guaranteed by the status
        command = "resume" if expected is S.PAUSED else "start"

        # Re-check with the provider right now; failing closed if it cannot be reached.
        try:
            validation = await self._compliance.validate_permission(flight.permission.permission_id)
        except ComplianceUnavailableError as exc:
            raise PermissionCheckStaleError(
                "the compliance provider is unavailable; failing closed"
            ) from exc

        # Record the intent (and run the gate) BEFORE sending anything to the drone.
        async with self._uow_factory() as uow:
            locked = await self._lock_flight(uow, flight_id)
            if locked.status is not expected:
                raise InvalidFlightTransitionError(locked.status, S.STARTED)
            result = self._flights.transition(
                locked,
                S.STARTED,
                source=source,
                validation=validation,
                metadata={"command": command},
            )
            await uow.flights.apply_transition(result)
            if expected is S.MISSION_UPLOADED:
                await self._set_drone(uow, locked.drone_id, D.ON_MISSION, only_from=_ASSIGNED)
            await uow.commit()

        authorization = result.start_authorization
        assert authorization is not None  # the machine always issues one on entering 'started'
        resuming = expected is S.PAUSED
        try:
            await self._command(
                command,
                flight_id,
                flight.drone_id,
                lambda: self._link.start_mission(flight.drone_id, authorization),
            )
        except CommandRejectedError as exc:
            raise await self._undo_failed_start(
                result.flight, source, resuming=resuming, unconfirmed=False, cause=str(exc)
            ) from exc
        except DroneLinkError as exc:
            raise await self._undo_failed_start(
                result.flight, source, resuming=resuming, unconfirmed=True, cause=str(exc)
            ) from exc
        return result.flight

    async def _undo_failed_start(
        self,
        flight: Flight,
        source: EventSource,
        *,
        resuming: bool,
        unconfirmed: bool,
        cause: str,
    ) -> StartCommandFailedError:
        """The intent was recorded but the drone did not take the start/resume: undo safely."""
        if resuming and not unconfirmed:
            # Refused: the drone is still hovering where it paused. Say so, and leave the
            # drone and the order alone; the flight may be resumed again or recalled.
            await self._advance(
                flight.id,
                S.PAUSED,
                source=source,
                metadata={"command": "resume", "resume_refused": cause},
            )
            return StartCommandFailedError(
                flight.id, unconfirmed=False, cause=cause, flight_aborted=False
            )
        if unconfirmed:
            # It may be flying. Recall it as a precaution; failure here is logged, not raised.
            try:
                await self._command(
                    "return_to_home",
                    flight.id,
                    flight.drone_id,
                    lambda: self._link.return_to_home(flight.drone_id),
                )
            except DroneLinkError as exc:
                logger.error(
                    "precautionary_return_to_home_failed",
                    flight_id=str(flight.id),
                    error=str(exc),
                )
        what = "resume" if resuming else "start"
        reason = (
            f"the {what} command was {'not acknowledged' if unconfirmed else 'refused'}: {cause}"
        )
        await self._end_flight_quietly(
            flight.id,
            S.ABORTED,
            source=source,
            reason=reason,
            drone_to=D.RETURNING if unconfirmed else D.AVAILABLE,
            drone_from=_ON_MISSION,
            metadata={"command": what},
        )
        await self._fail_order(flight.order_id, reason, flight.id, source)
        return StartCommandFailedError(flight.id, unconfirmed=unconfirmed, cause=cause)

    async def pause_flight(
        self, flight_id: UUID, *, source: EventSource = EventSource.OPS
    ) -> Flight:
        """Pause a flying mission. Never permission-gated."""
        flight = await self._require_flight(flight_id)
        self._require_edge(flight, S.PAUSED)
        await self._safety_command(
            "pause", flight, lambda: self._link.pause_mission(flight.drone_id)
        )
        return await self._advance(
            flight_id, S.PAUSED, source=source, metadata={"command": "pause"}
        )

    async def return_to_home(
        self,
        flight_id: UUID,
        *,
        source: EventSource = EventSource.OPS,
        reason: str = "operator request",
    ) -> Flight:
        """Recall a flying drone. Never permission-gated. Aborts the flight."""
        flight = await self._require_flight(flight_id)
        self._require_edge(flight, S.ABORTED)
        await self._safety_command(
            "return_to_home", flight, lambda: self._link.return_to_home(flight.drone_id)
        )
        return await self._end_flight(
            flight_id,
            S.ABORTED,
            source=source,
            reason=f"return_to_home: {reason}",
            drone_to=D.RETURNING,
            drone_from=_ON_MISSION,
            metadata={"command": "return_to_home"},
        )

    async def land(
        self,
        flight_id: UUID,
        *,
        source: EventSource = EventSource.OPS,
        reason: str = "operator request",
    ) -> Flight:
        """Land a flying drone where it is. Never permission-gated. Aborts the flight."""
        flight = await self._require_flight(flight_id)
        self._require_edge(flight, S.ABORTED)
        await self._safety_command("land", flight, lambda: self._link.land(flight.drone_id))
        return await self._end_flight(
            flight_id,
            S.ABORTED,
            source=source,
            reason=f"land: {reason}",
            drone_to=D.OFFLINE,  # grounded away from its dock until operations recovers it
            drone_from=_ON_MISSION,
            metadata={"command": "land"},
        )

    # ==========================================================================================
    # Cancellation
    # ==========================================================================================

    async def cancel_order(
        self,
        order_id: UUID,
        *,
        source: EventSource,
        reason: str = "cancelled",
    ) -> CancelResult:
        """Cancel an order, recalling its drone first if it is flying.

        Idempotent for an order that is already cancelled. If the drone cannot be recalled
        the order is deliberately *not* cancelled (``CommandNotConfirmedError``): a
        cancelled order must never leave a drone flying.
        """
        order = await self._orders.get_order(order_id)
        if order.status is OrderStatus.CANCELLED:
            return CancelResult(order, None)
        if order.status.is_terminal:
            raise TerminalStateError(order.status, OrderStatus.CANCELLED)

        ended: Flight | None = None
        for _attempt in range(3):
            live = await self._live_flight_for_order(order_id)
            if live is None:
                break
            try:
                ended = await self._end_for_cancellation(live, source, reason)
            except (InvalidFlightTransitionError, ConcurrentUpdateError):
                continue  # the flight moved underneath us (e.g. it just started): look again
            break
        else:
            raise FlightInProgressError(order_id)

        cancelled = await self._orders.transition(
            order_id, OrderStatus.CANCELLED, source=source, metadata={"reason": reason}
        )
        logger.info("order_cancelled", order_id=str(order_id), reason=reason)
        return CancelResult(cancelled, ended)

    async def _end_for_cancellation(
        self, flight: Flight, source: EventSource, reason: str
    ) -> Flight:
        if flight.status in {S.STARTED, S.PAUSED}:
            # Recall FIRST: only once the drone confirms may the flight be recorded as ended.
            await self._safety_command(
                "return_to_home", flight, lambda: self._link.return_to_home(flight.drone_id)
            )
            return await self._end_flight(
                flight.id,
                S.ABORTED,
                source=source,
                reason=f"order_cancelled: {reason}",
                drone_to=D.RETURNING,
                drone_from=_ON_MISSION,
                metadata={"command": "return_to_home"},
            )
        return await self._end_flight(
            flight.id,
            S.CANCELLED,
            source=source,
            reason=f"order_cancelled: {reason}",
            drone_to=D.AVAILABLE,
            drone_from=_ASSIGNED,
        )

    # ==========================================================================================
    # Progress reported by the drone (telemetry)
    # ==========================================================================================

    async def record_progress(
        self,
        drone_id: UUID,
        flight_id: UUID,
        event: ProgressEvent,
        *,
        occurred_at: datetime | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> ProgressResult:
        """Apply something the drone reports. The only way a telemetry event changes anything.

        Idempotent and strict: a repeat is ``ALREADY_APPLIED``; an event that does not fit
        the current state (wrong order status, a flight that is not flying, another drone's
        flight) is ``REJECTED`` with a reason and changes nothing. It never invents the
        steps a lost event would have recorded, and it never sends a command.
        """
        if event is ProgressEvent.FAULT:
            logger.error(
                "drone_fault",
                drone_id=str(drone_id),
                flight_id=str(flight_id),
                detail=dict(detail or {}),
            )
            return ProgressResult(ProgressOutcome.NOTED, "fault logged")

        flight = await self._flight_or_none(flight_id)
        if flight is None:
            return self._rejected(event, drone_id, flight_id, "unknown flight")
        if flight.drone_id != drone_id:
            return self._rejected(event, drone_id, flight_id, "the flight belongs to another drone")

        if event in ORDER_STEP:
            result = await self._progress_order(flight, event, occurred_at)
        elif event is ProgressEvent.MISSION_COMPLETED:
            result = await self._progress_completion(flight, occurred_at)
        else:
            result = await self._progress_docking(flight)

        if result.outcome is ProgressOutcome.REJECTED:
            return self._rejected(event, drone_id, flight_id, result.reason or "")
        logger.info(
            "progress_recorded",
            progress=event.value,
            outcome=result.outcome.value,
            drone_id=str(drone_id),
            flight_id=str(flight_id),
        )
        return result

    @staticmethod
    def _rejected(
        event: ProgressEvent, drone_id: UUID, flight_id: UUID, reason: str
    ) -> ProgressResult:
        logger.warning(
            "progress_rejected",
            progress=event.value,
            drone_id=str(drone_id),
            flight_id=str(flight_id),
            reason=reason,
        )
        return ProgressResult(ProgressOutcome.REJECTED, reason)

    @staticmethod
    def _progress_metadata(
        flight: Flight, event: ProgressEvent, occurred_at: datetime | None
    ) -> dict[str, Any]:
        """What the audit trail keeps of an event: never the drone's free-form text."""
        metadata: dict[str, Any] = {"flight_id": str(flight.id), "event": event.value}
        if occurred_at is not None:
            metadata["occurred_at"] = occurred_at.isoformat()
        return metadata

    async def _progress_order(
        self, flight: Flight, event: ProgressEvent, occurred_at: datetime | None
    ) -> ProgressResult:
        """Move the flight's order one step along its journey, through the order service."""
        source_status, target = ORDER_STEP[event]
        order = await self._orders.get_order(flight.order_id)
        if at_or_beyond(order.status, target):
            return ProgressResult(ProgressOutcome.ALREADY_APPLIED)
        if flight.status not in {S.STARTED, S.PAUSED}:
            return ProgressResult(
                ProgressOutcome.REJECTED, f"the flight is {flight.status.value}, not flying"
            )
        if order.status is not source_status:
            return ProgressResult(
                ProgressOutcome.REJECTED,
                f"expected the order to be {source_status.value}, but it is {order.status.value}",
            )
        try:
            await self._orders.transition(
                order.id,
                target,
                source=EventSource.TELEMETRY,
                metadata=self._progress_metadata(flight, event, occurred_at),
            )
        except (InvalidTransitionError, ConcurrentUpdateError):
            # Someone moved the order meanwhile (a duplicate of this event, a cancellation).
            current = await self._orders.get_order(order.id)
            if at_or_beyond(current.status, target):
                return ProgressResult(ProgressOutcome.ALREADY_APPLIED)
            return ProgressResult(ProgressOutcome.REJECTED, f"the order is {current.status.value}")
        return ProgressResult(ProgressOutcome.APPLIED)

    async def _progress_completion(
        self, flight: Flight, occurred_at: datetime | None
    ) -> ProgressResult:
        """The drone landed at its dock having finished the mission: end the flight, free it."""
        if flight.status is S.COMPLETED:
            return ProgressResult(ProgressOutcome.ALREADY_APPLIED)
        if flight.status is not S.STARTED:
            return ProgressResult(
                ProgressOutcome.REJECTED, f"the flight is {flight.status.value}, not flying"
            )
        try:
            await self._end_flight(
                flight.id,
                S.COMPLETED,
                source=EventSource.TELEMETRY,
                drone_to=D.AVAILABLE,
                drone_from=_ON_MISSION,
                metadata=self._progress_metadata(
                    flight, ProgressEvent.MISSION_COMPLETED, occurred_at
                ),
            )
        except (InvalidFlightTransitionError, ConcurrentUpdateError):
            current = await self._flight_or_none(flight.id)
            if current is not None and current.status is S.COMPLETED:
                return ProgressResult(ProgressOutcome.ALREADY_APPLIED)
            return ProgressResult(
                ProgressOutcome.REJECTED,
                f"the flight is {current.status.value if current else 'gone'}",
            )
        order = await self._orders.get_order(flight.order_id)
        if order.status is not OrderStatus.DELIVERED:
            # The drone says it finished but the delivery was never reported. Do not guess.
            logger.warning(
                "mission_completed_without_delivery",
                order_id=str(order.id),
                order_status=order.status.value,
                flight_id=str(flight.id),
            )
        return ProgressResult(ProgressOutcome.APPLIED)

    async def _progress_docking(self, flight: Flight) -> ProgressResult:
        """A recalled or grounded drone reports it is back at its dock: it may fly again."""
        if flight.status.is_live:
            return ProgressResult(
                ProgressOutcome.REJECTED,
                f"the drone reports docked but its flight is {flight.status.value}",
            )
        drone = await self.get_drone(flight.drone_id)
        if drone.status is D.AVAILABLE:
            return ProgressResult(ProgressOutcome.ALREADY_APPLIED)
        if drone.status not in {D.RETURNING, D.OFFLINE}:
            return ProgressResult(
                ProgressOutcome.REJECTED, f"the drone is {drone.status.value}, not returning"
            )
        try:
            await self.confirm_docked(drone.id)
        except (DroneBusyError, InvalidDroneTransitionError, ConcurrentUpdateError) as error:
            return ProgressResult(ProgressOutcome.REJECTED, str(error))
        return ProgressResult(ProgressOutcome.APPLIED)

    # ==========================================================================================
    # Internals
    # ==========================================================================================

    async def _require_flight(self, flight_id: UUID) -> Flight:
        flight = await self._flight_or_none(flight_id)
        if flight is None:
            raise FlightNotFoundError(flight_id)
        return flight

    async def _flight_or_none(self, flight_id: UUID) -> Flight | None:
        async with self._uow_factory() as uow:
            return await uow.flights.get(flight_id)

    async def _lock_flight(self, uow: UnitOfWork, flight_id: UUID) -> Flight:
        flight = await uow.flights.get(flight_id, for_update=True)
        if flight is None:
            raise FlightNotFoundError(flight_id)
        return flight

    async def _live_flight_for_order(self, order_id: UUID) -> Flight | None:
        async with self._uow_factory() as uow:
            live = [f for f in await uow.flights.list_for_order(order_id) if f.status.is_live]
        return live[0] if live else None

    def _require_edge(self, flight: Flight, target: FlightStatus) -> None:
        if self._flights.can_transition(flight.status, target):
            return
        if flight.status.is_terminal:
            raise TerminalFlightStateError(flight.status, target)
        raise InvalidFlightTransitionError(flight.status, target)

    async def _advance(
        self,
        flight_id: UUID,
        target: FlightStatus,
        *,
        source: EventSource,
        permission: Any = None,
        acknowledged_checksum: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Flight:
        """Move a flight one step, re-reading it under a lock, and store it."""
        async with self._uow_factory() as uow:
            flight = await self._lock_flight(uow, flight_id)
            result = self._flights.transition(
                flight,
                target,
                source=source,
                permission=permission,
                acknowledged_checksum=acknowledged_checksum,
                metadata=metadata,
            )
            await uow.flights.apply_transition(result)
            await uow.commit()
        return result.flight

    async def _end_flight(
        self,
        flight_id: UUID,
        target: FlightStatus,
        *,
        source: EventSource,
        reason: str | None = None,
        denial: PermissionDenial | None = None,
        drone_to: DroneStatus | None = None,
        drone_from: frozenset[DroneStatus] = frozenset(),
        metadata: Mapping[str, Any] | None = None,
    ) -> Flight:
        """End a flight and, in the same transaction, move its drone."""
        async with self._uow_factory() as uow:
            flight = await self._lock_flight(uow, flight_id)
            result: FlightTransitionResult = self._flights.transition(
                flight, target, source=source, reason=reason, denial=denial, metadata=metadata
            )
            await uow.flights.apply_transition(result)
            if drone_to is not None:
                await self._set_drone(uow, flight.drone_id, drone_to, only_from=drone_from)
            await uow.commit()
        return result.flight

    async def _end_flight_quietly(
        self, flight_id: UUID, target: FlightStatus, **kwargs: Any
    ) -> None:
        """Like :meth:`_end_flight`, but a flight that already ended is not an error."""
        try:
            await self._end_flight(flight_id, target, **kwargs)
        except (InvalidFlightTransitionError, ConcurrentUpdateError):
            logger.info("flight_already_ended", flight_id=str(flight_id), wanted=target.value)

    async def _set_drone(
        self, uow: UnitOfWork, drone_id: UUID, to: DroneStatus, *, only_from: frozenset[DroneStatus]
    ) -> None:
        """Move a drone, but only from the statuses the caller expects (never surprise it)."""
        drone = await uow.drones.get(drone_id, for_update=True)
        if drone is None or drone.status not in only_from or drone.status is to:
            return
        await uow.drones.apply_status_change(drone, drone.with_status(to, now=self._clock()))

    async def _fail_order(
        self, order_id: UUID, reason: str, flight_id: UUID, source: EventSource
    ) -> None:
        """An order that was assigned cannot go back: fail it (a no-op if it already ended)."""
        try:
            await self._orders.transition(
                order_id,
                OrderStatus.FAILED,
                source=source,
                metadata={"reason": reason, "flight_id": str(flight_id)},
            )
        except InvalidTransitionError:
            logger.info("order_already_ended", order_id=str(order_id))

    async def _command[T](
        self,
        name: str,
        flight_id: UUID,
        drone_id: UUID,
        call: Callable[[], Awaitable[T]],
    ) -> T:
        """Send one drone-link operation, logging the attempt and its result."""
        try:
            result = await call()
        except DroneLinkError as exc:
            logger.warning(
                "drone_command_failed",
                command=name,
                flight_id=str(flight_id),
                drone_id=str(drone_id),
                error=type(exc).__name__,
                detail=str(exc),
            )
            raise
        logger.info(
            "drone_command_acknowledged",
            command=name,
            flight_id=str(flight_id),
            drone_id=str(drone_id),
        )
        return result

    async def _safety_command(
        self, name: str, flight: Flight, call: Callable[[], Awaitable[None]]
    ) -> None:
        """Send a pause/return-to-home/land. If it is not confirmed, record nothing."""
        try:
            await self._command(name, flight.id, flight.drone_id, call)
        except DroneLinkError as exc:
            raise CommandNotConfirmedError(name, flight.id, str(exc)) from exc

    async def _finish(
        self,
        outcome: DispatchOutcome,
        order_id: UUID,
        flight_id: UUID,
        drone_id: UUID,
        *,
        detail: str | None = None,
    ) -> DispatchResult:
        """Build a result from what is actually stored, not from what we think we wrote."""
        order = await self._orders.get_order(order_id)
        flight = await self._flight_or_none(flight_id)
        async with self._uow_factory() as uow:
            drone = await uow.drones.get(drone_id)
        return DispatchResult(outcome, order, flight, drone, detail=detail)
