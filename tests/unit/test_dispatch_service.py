"""DispatchService, every outcome and failure mode, on the in-memory unit of work.

The service is the only code allowed to write flights and drones and to call the drone
link, so these tests pin down what each success and each failure leaves behind: the order,
the flight, the drone, the audit trail and exactly which commands reached a drone (Rule 1
and Rule 2). A parallel dual-backend suite in ``tests/integration`` runs the main paths
against PostgreSQL too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any, Self
from uuid import UUID

import pytest

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
from app.dispatch.link import CommandRejectedError, NoAcknowledgementError
from app.dispatch.service import DispatchOutcome, DispatchResult
from app.domain.exceptions import (
    InvalidDroneTransitionError,
    InvalidFlightTransitionError,
    PermissionCheckStaleError,
    PermissionExpiredError,
    PermissionRevokedError,
    TerminalFlightStateError,
    TerminalStateError,
)
from app.domain.flight_state_machine import FlightStateMachine
from app.domain.models import (
    DenialCode,
    DroneStatus,
    EventSource,
    FlightStatus,
    OrderStatus,
)
from app.services.exceptions import ConcurrentUpdateError, OrderNotFoundError
from tests.conftest import FakeClock, build_mission
from tests.dispatch_rig import Rig, assert_rule2, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

O = DispatchOutcome  # noqa: E741
S = FlightStatus
D = DroneStatus


@pytest.fixture
def database() -> InMemoryDatabase:
    return InMemoryDatabase()


@pytest.fixture
def rig(database: InMemoryDatabase, clock: FakeClock) -> Rig:
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


async def dispatch_one(rig: Rig, **kwargs: Any) -> tuple[DispatchResult, UUID]:
    """One drone, one order, one dispatch. Returns the result and the drone's id."""
    drone = await rig.add_drone()
    order = await rig.new_order()
    return await rig.dispatch.dispatch_order(order.id, **kwargs), drone.id


# =============================================================================================
# The happy path
# =============================================================================================


class TestDispatchHappyPath:
    async def test_assigns_uploads_and_starts(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.STARTED
        assert result.succeeded
        assert result.order.status is OrderStatus.ASSIGNED
        assert result.order.drone_id == drone_id
        assert result.flight is not None
        assert result.flight.status is S.STARTED
        assert result.drone is not None
        assert result.drone.status is D.ON_MISSION

    async def test_the_audit_trail_shows_the_rule_2_order_of_events(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        # permission, then upload, THEN the order is assigned, THEN the flight starts.
        assert await rig.steps(result.order.id) == [
            ("order created", "created"),
            ("flight created", "planned"),
            ("flight", "permission_granted"),
            ("flight", "mission_uploaded"),
            ("order", "assigned"),
            ("flight", "started"),
        ]

    async def test_only_an_upload_and_a_start_reach_the_drone(self, rig: Rig) -> None:
        await dispatch_one(rig)

        assert rig.commands() == ["upload", "start"]

    async def test_the_drone_holds_exactly_the_mission_the_flight_recorded(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)

        assert result.flight is not None
        assert rig.link.on_board[drone_id].checksum == result.flight.mission.checksum
        assert result.flight.acknowledged_checksum == result.flight.mission.checksum

    async def test_permission_is_asked_for_the_planned_route(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)

        assert result.flight is not None
        (request,) = rig.compliance.requests
        assert request.flight_id == result.flight.id
        assert request.order_id == result.order.id
        assert request.drone_id == drone_id
        assert request.route == result.flight.mission.route
        assert request.estimated_duration_s == result.flight.mission.estimated_duration_s

    async def test_the_flight_carries_the_granted_permission(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        assert result.flight is not None
        assert result.flight.permission is not None
        assert result.flight.permission.permission_id == rig.compliance.issued[0].permission_id

    async def test_the_mission_is_a_full_sortie_from_the_drones_dock(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        assert result.flight is not None
        kinds = [w.kind.value for w in result.flight.mission.waypoints]
        assert kinds == ["takeoff", "pickup", "dropoff", "return", "land"]

    async def test_the_nearest_suitable_drone_is_chosen(self, rig: Rig) -> None:
        far = await rig.add_drone("far", home=build_mission().waypoints[2].location)
        near = await rig.add_drone("near")
        order = await rig.new_order()

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.drone is not None
        assert result.drone.id == near.id != far.id

    async def test_it_is_logged_in_the_flights_for_the_order(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        (flight,) = await rig.dispatch.list_flights_for_order(result.order.id)
        assert result.flight == flight

    async def test_the_dispatch_source_is_recorded_on_every_event(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        sources = {e.source for e in await rig.trail(result.order.id) if e.flight_id is not None}
        assert sources == {EventSource.DISPATCH}


class TestStagedDispatch:
    async def test_start_false_stops_with_the_mission_on_board(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig, start=False)

        assert result.outcome is O.READY
        assert result.succeeded
        assert result.order.status is OrderStatus.ASSIGNED
        assert result.flight is not None
        assert result.flight.status is S.MISSION_UPLOADED
        assert result.drone is not None
        assert result.drone.status is D.ASSIGNED
        assert rig.commands() == ["upload"]
        assert drone_id in rig.link.on_board

    async def test_a_staged_flight_can_then_be_started(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig, start=False)
        assert result.flight is not None

        started = await rig.dispatch.start_flight(result.flight.id)

        assert started.status is S.STARTED
        assert (await rig.drone(drone_id)).status is D.ON_MISSION
        assert rig.commands() == ["upload", "start"]
        await assert_rule2(rig)

    async def test_starting_twice_is_refused_and_sends_nothing(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig, start=False)
        assert result.flight is not None
        await rig.dispatch.start_flight(result.flight.id)

        with pytest.raises(InvalidFlightTransitionError):
            await rig.dispatch.start_flight(result.flight.id)

        assert rig.commands() == ["upload", "start"]

    async def test_start_of_an_unknown_flight(self, rig: Rig) -> None:
        with pytest.raises(FlightNotFoundError):
            await rig.dispatch.start_flight(UUID(int=99))


# =============================================================================================
# Outcomes that leave the order retryable
# =============================================================================================


class TestNoDrone:
    async def test_without_any_drone_nothing_happens(self, rig: Rig) -> None:
        order = await rig.new_order()

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.NO_DRONE_AVAILABLE
        assert not result.succeeded
        assert result.order.status is OrderStatus.CREATED
        assert result.flight is None
        assert await rig.flights(order.id) == []
        assert rig.compliance.requests == []  # permission is never asked for without a drone
        assert rig.commands() == []

    async def test_it_explains_why_each_drone_was_rejected(self, rig: Rig) -> None:
        drone = await rig.add_drone(max_payload_kg=1.0)
        order = await rig.new_order(weight_kg=2.5)

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.NO_DRONE_AVAILABLE
        assert [r.value for r in result.rejections[drone.id]] == ["payload_too_heavy"]
        assert (await rig.drone(drone.id)).status is D.AVAILABLE

    async def test_a_busy_drone_is_not_a_candidate(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        first = await rig.new_order()
        second = await rig.new_order()
        assert (await rig.dispatch.dispatch_order(first.id)).outcome is O.STARTED

        result = await rig.dispatch.dispatch_order(second.id)

        assert result.outcome is O.NO_DRONE_AVAILABLE
        assert result.order.status is OrderStatus.CREATED
        assert rig.link.operations("upload") == [drone.id]  # the second order sent nothing
        await assert_rule2(rig)

    async def test_the_order_can_be_dispatched_once_a_drone_is_free(self, rig: Rig) -> None:
        order = await rig.new_order()
        assert (await rig.dispatch.dispatch_order(order.id)).outcome is O.NO_DRONE_AVAILABLE
        await rig.add_drone()

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.STARTED


class _RiggedUow(InMemoryUnitOfWork):
    """An in-memory unit of work that lets a test tamper with each transaction as it opens.

    (A subclass, not an instance patch: ``async with`` looks ``__aenter__`` up on the type.)
    """

    def __init__(
        self, database: InMemoryDatabase, rig: Callable[[InMemoryUnitOfWork], None]
    ) -> None:
        super().__init__(database)
        self._rig = rig

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self._rig(self)
        return self


def _claims_lose(
    uow: InMemoryUnitOfWork,
    database: InMemoryDatabase,
    lost: list[UUID],
    *,
    rival_takes_it: bool,
    only_first: bool,
) -> None:
    """Make this transaction lose its claim on a drone, as if another dispatch got there first.

    With ``rival_takes_it`` the rival's change is *committed* (the database) and visible to
    this transaction's next read, as it would be under READ COMMITTED. Otherwise the claim
    just keeps failing. Each lost drone id is appended to ``lost``.
    """
    claim = uow.drones.apply_status_change

    async def rivalled(before: Any, after: Any) -> None:
        if after.status is D.ASSIGNED and not (only_first and lost):
            lost.append(before.id)
            if rival_takes_it:
                taken = replace(before, status=D.ASSIGNED)
                database.drones[before.id] = taken
                uow.drones._drones[before.id] = taken
            else:
                raise ConcurrentUpdateError("someone else got it")
        await claim(before, after)

    uow.drones.apply_status_change = rivalled  # type: ignore[method-assign]


class TestLosingADroneClaim:
    """Another dispatch can take the chosen drone between choosing it and claiming it."""

    async def test_it_chooses_again_and_gets_the_next_best_drone(
        self, rig: Rig, database: InMemoryDatabase
    ) -> None:
        near = await rig.add_drone("near")
        far = await rig.add_drone("far", home=build_mission().waypoints[2].location)
        order = await rig.new_order()
        lost: list[UUID] = []
        rig.dispatch._uow_factory = lambda: _RiggedUow(
            database,
            lambda uow: _claims_lose(uow, database, lost, rival_takes_it=True, only_first=True),
        )

        result = await rig.dispatch.dispatch_order(order.id)

        assert lost == [near.id]
        assert result.outcome is O.STARTED
        assert result.drone is not None
        assert result.drone.id == far.id
        assert (await rig.drone(near.id)).status is D.ASSIGNED  # the rival's, untouched
        await assert_rule2(rig)

    async def test_when_the_only_drone_was_taken_nothing_is_dispatched(
        self, rig: Rig, database: InMemoryDatabase
    ) -> None:
        only = await rig.add_drone("only")
        order = await rig.new_order()
        lost: list[UUID] = []
        rig.dispatch._uow_factory = lambda: _RiggedUow(
            database,
            lambda uow: _claims_lose(uow, database, lost, rival_takes_it=True, only_first=True),
        )

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.NO_DRONE_AVAILABLE
        assert result.order.status is OrderStatus.CREATED
        assert result.flight is None
        assert rig.compliance.requests == []
        assert rig.commands() == []
        assert lost == [only.id]
        assert (await rig.drone(only.id)).status is D.ASSIGNED  # held by the rival

    async def test_endless_contention_gives_up_with_a_retryable_refusal(
        self, rig: Rig, database: InMemoryDatabase
    ) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()
        factory = rig.dispatch._uow_factory
        rig.dispatch._uow_factory = lambda: _RiggedUow(
            database,
            lambda uow: _claims_lose(uow, database, [], rival_takes_it=False, only_first=False),
        )

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.NO_DRONE_AVAILABLE
        assert result.detail is not None
        assert "try again" in result.detail
        assert result.order.status is OrderStatus.CREATED
        assert rig.compliance.requests == []

        rig.dispatch._uow_factory = factory  # contention over: the same order goes through
        assert (await rig.drone(drone.id)).status is D.AVAILABLE
        assert (await rig.dispatch.dispatch_order(order.id)).outcome is O.STARTED


class TestPermissionDenied:
    async def test_a_denial_releases_the_drone_and_keeps_the_order_dispatchable(
        self, rig: Rig
    ) -> None:
        rig.compliance.close_airspace("airspace closed for an event")
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.PERMISSION_DENIED
        assert result.detail == "airspace closed for an event"
        assert result.order.status is OrderStatus.CREATED
        assert result.flight is not None
        assert result.flight.status is S.PERMISSION_DENIED
        assert result.flight.denial is not None
        assert result.flight.denial.code is DenialCode.AIRSPACE_CLOSED
        assert result.drone is not None
        assert result.drone.id == drone_id
        assert result.drone.status is D.AVAILABLE
        assert rig.commands() == []  # nothing was uploaded, nothing started
        await assert_rule2(rig)

    async def test_the_denial_is_in_the_audit_trail(self, rig: Rig) -> None:
        rig.compliance.close_airspace("closed")
        result, _ = await dispatch_one(rig)

        assert await rig.steps(result.order.id) == [
            ("order created", "created"),
            ("flight created", "planned"),
            ("flight", "permission_denied"),
        ]

    async def test_an_outage_is_a_denial_never_permission(self, rig: Rig) -> None:
        rig.compliance.set_outage(True)
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.PERMISSION_DENIED
        assert result.flight is not None
        assert result.flight.denial is not None
        assert result.flight.denial.code is DenialCode.PROVIDER_UNAVAILABLE
        assert rig.commands() == []
        await assert_rule2(rig)

    async def test_a_no_fly_zone_route_is_denied(self, database: InMemoryDatabase) -> None:
        from app.compliance.mock_digital_sky import NoFlyZone
        from tests.conftest import PICKUP

        rig = build_rig(lambda: InMemoryUnitOfWork(database), clock=FakeClock())
        rig.compliance._zones = (NoFlyZone("stadium", PICKUP, 500.0),)
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.PERMISSION_DENIED
        assert result.flight is not None
        assert result.flight.denial is not None
        assert result.flight.denial.code is DenialCode.NO_FLY_ZONE

    async def test_redispatch_after_a_denial_makes_a_new_flight(
        self, rig: Rig, clock: FakeClock
    ) -> None:
        rig.compliance.close_airspace("closed")
        result, _ = await dispatch_one(rig)
        rig.compliance.close_airspace(None)
        clock.advance(seconds=1)  # flights are listed oldest first

        again = await rig.dispatch.dispatch_order(result.order.id)

        assert again.outcome is O.STARTED
        flights = await rig.flights(result.order.id)
        assert [f.status for f in flights] == [S.PERMISSION_DENIED, S.STARTED]  # history is kept
        await assert_rule2(rig)


class TestPermissionInvalidAfterGrant:
    async def test_revoked_right_after_the_grant(self, rig: Rig) -> None:
        rig.compliance.on_validate = lambda n, pid: rig.compliance.revoke(pid) if n == 1 else None
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.PERMISSION_INVALID
        assert result.order.status is OrderStatus.CREATED
        assert result.flight is not None
        assert result.flight.status is S.CANCELLED
        assert result.drone is not None
        assert result.drone.status is D.AVAILABLE
        assert rig.commands() == []  # no mission was uploaded for a revoked permission
        await assert_rule2(rig)

    async def test_an_outage_while_validating(self, rig: Rig) -> None:
        rig.compliance.on_validate = lambda n, _: rig.compliance.set_outage(True)
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.PERMISSION_INVALID
        assert result.flight is not None
        assert result.flight.status is S.CANCELLED
        assert rig.commands() == []


class TestUploadFailure:
    @pytest.mark.parametrize(
        "error",
        [CommandRejectedError("storage full"), NoAcknowledgementError("link down")],
        ids=["rejected", "unanswered"],
    )
    async def test_a_failed_upload_releases_the_drone_and_keeps_the_order_dispatchable(
        self, rig: Rig, error: Exception
    ) -> None:
        rig.link.set_failure("upload", error)  # type: ignore[arg-type]
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.UPLOAD_FAILED
        assert result.order.status is OrderStatus.CREATED
        assert result.flight is not None
        assert result.flight.status is S.UPLOAD_FAILED
        assert result.flight.failure_reason is not None
        assert str(error) in result.flight.failure_reason
        assert result.drone is not None
        assert result.drone.status is D.AVAILABLE
        assert "start" not in rig.commands()
        await assert_rule2(rig)

    async def test_an_acknowledgement_of_a_different_mission_is_a_failed_upload(
        self, rig: Rig
    ) -> None:
        rig.link.corrupt_next_ack()
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.UPLOAD_FAILED
        assert result.flight is not None
        assert result.flight.status is S.UPLOAD_FAILED
        assert result.flight.acknowledged_checksum is None
        assert "start" not in rig.commands()
        await assert_rule2(rig)

    async def test_the_order_is_not_assigned_when_the_upload_failed(self, rig: Rig) -> None:
        rig.link.set_failure("upload", CommandRejectedError("no"))
        result, _ = await dispatch_one(rig)

        assert await rig.steps(result.order.id) == [
            ("order created", "created"),
            ("flight created", "planned"),
            ("flight", "permission_granted"),
            ("flight", "upload_failed"),
        ]

    async def test_the_retry_after_a_failed_upload_succeeds(self, rig: Rig) -> None:
        rig.link.set_failure("upload", NoAcknowledgementError("link down"))
        result, _ = await dispatch_one(rig)
        rig.link.set_failure("upload", None)

        again = await rig.dispatch.dispatch_order(result.order.id)

        assert again.outcome is O.STARTED


# =============================================================================================
# The start gate (Rule 2) during dispatch
# =============================================================================================


class TestStartRefusedByTheGate:
    async def test_a_revocation_between_upload_and_start_stops_the_flight(self, rig: Rig) -> None:
        # validation 1 = right after the grant; validation 2 = the re-check that feeds the gate
        rig.compliance.on_validate = lambda n, pid: rig.compliance.revoke(pid) if n == 2 else None
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.order.status is OrderStatus.FAILED  # it had been assigned
        assert result.flight is not None
        assert result.flight.status is S.CANCELLED
        assert result.drone is not None
        assert result.drone.status is D.AVAILABLE
        assert rig.commands() == ["upload"]  # the mission is on board but never started
        await assert_rule2(rig)

    async def test_an_outage_at_the_gate_fails_closed(self, rig: Rig) -> None:
        rig.compliance.on_validate = lambda n, _: rig.compliance.set_outage(n >= 2)
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.order.status is OrderStatus.FAILED
        assert rig.commands() == ["upload"]
        await assert_rule2(rig)

    async def test_a_permission_too_short_for_the_mission_is_refused(
        self, database: InMemoryDatabase
    ) -> None:
        rig = build_rig(
            lambda: InMemoryUnitOfWork(database),
            clock=FakeClock(),
            validity=timedelta(minutes=5),  # a sortie is ~22 minutes
        )
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.order.status is OrderStatus.FAILED
        assert "start" not in rig.commands()
        await assert_rule2(rig)

    async def test_the_failure_is_in_the_audit_trail(self, rig: Rig) -> None:
        rig.compliance.on_validate = lambda n, pid: rig.compliance.revoke(pid) if n == 2 else None
        result, _ = await dispatch_one(rig)

        assert await rig.steps(result.order.id) == [
            ("order created", "created"),
            ("flight created", "planned"),
            ("flight", "permission_granted"),
            ("flight", "mission_uploaded"),
            ("order", "assigned"),
            ("flight", "cancelled"),
            ("order", "failed"),
        ]

    async def test_a_staged_flight_is_refused_by_the_gate_without_changing_anything(
        self, rig: Rig
    ) -> None:
        result, drone_id = await dispatch_one(rig, start=False)
        assert result.flight is not None
        rig.compliance.revoke(rig.compliance.issued[0].permission_id)

        with pytest.raises(PermissionRevokedError):
            await rig.dispatch.start_flight(result.flight.id)

        # Refused by the gate: nothing persisted, the flight is still staged, nothing sent.
        assert (await rig.dispatch.get_flight(result.flight.id)).status is S.MISSION_UPLOADED
        assert (await rig.drone(drone_id)).status is D.ASSIGNED
        assert (await rig.order(result.order.id)).status is OrderStatus.ASSIGNED
        assert rig.commands() == ["upload"]

    async def test_an_outage_when_starting_a_staged_flight_fails_closed(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig, start=False)
        assert result.flight is not None
        rig.compliance.set_outage(True)

        with pytest.raises(PermissionCheckStaleError):
            await rig.dispatch.start_flight(result.flight.id)

        assert rig.commands() == ["upload"]

    async def test_a_stale_permission_expires_before_a_late_start(
        self, rig: Rig, clock: FakeClock
    ) -> None:
        result, _ = await dispatch_one(rig, start=False)
        assert result.flight is not None
        clock.advance(hours=2)  # the permission (1 hour) has long expired

        with pytest.raises(PermissionExpiredError):
            await rig.dispatch.start_flight(result.flight.id)

        assert rig.commands() == ["upload"]
        await assert_rule2(rig)


# =============================================================================================
# The start command itself failing
# =============================================================================================


class TestStartCommandFailure:
    async def test_a_refused_start_aborts_the_flight_frees_the_drone_and_fails_the_order(
        self, rig: Rig
    ) -> None:
        rig.link.set_failure("start", CommandRejectedError("motors not armed"))
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.order.status is OrderStatus.FAILED
        assert result.flight is not None
        assert result.flight.status is S.ABORTED
        assert result.drone is not None
        assert result.drone.status is D.AVAILABLE
        # It definitely did not act, so no recall is sent.
        assert rig.commands() == ["upload"]
        await assert_rule2(rig)

    async def test_an_unanswered_start_recalls_the_drone_as_a_precaution(self, rig: Rig) -> None:
        # The drone may have started: the command arrived but the acknowledgement was lost.
        rig.link.set_failure("start", NoAcknowledgementError("timed out"), executed=True)
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.order.status is OrderStatus.FAILED
        assert result.flight is not None
        assert result.flight.status is S.ABORTED
        assert result.drone is not None
        assert result.drone.status is D.RETURNING
        assert rig.commands() == ["upload", "start", "return_to_home"]
        await assert_rule2(rig)

    async def test_a_failed_precautionary_recall_does_not_hide_the_failure(self, rig: Rig) -> None:
        rig.link.set_failure("start", NoAcknowledgementError("timed out"), executed=True)
        rig.link.set_failure("return_to_home", NoAcknowledgementError("still down"))
        result, _ = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.flight is not None
        assert result.flight.status is S.ABORTED
        assert result.drone is not None
        assert result.drone.status is D.RETURNING  # nothing says it is safe on the ground

    async def test_an_explicit_start_raises_after_cleaning_up(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig, start=False)
        assert result.flight is not None
        rig.link.set_failure("start", CommandRejectedError("busy"))

        with pytest.raises(StartCommandFailedError) as excinfo:
            await rig.dispatch.start_flight(result.flight.id)

        assert excinfo.value.flight_id == result.flight.id
        assert excinfo.value.unconfirmed is False
        assert excinfo.value.flight_aborted is True
        assert (await rig.dispatch.get_flight(result.flight.id)).status is S.ABORTED
        assert (await rig.drone(drone_id)).status is D.AVAILABLE
        assert (await rig.order(result.order.id)).status is OrderStatus.FAILED

    async def test_an_explicit_unconfirmed_start_says_so(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig, start=False)
        assert result.flight is not None
        rig.link.set_failure("start", NoAcknowledgementError("timed out"), executed=True)

        with pytest.raises(StartCommandFailedError) as excinfo:
            await rig.dispatch.start_flight(result.flight.id)

        assert excinfo.value.unconfirmed is True
        assert (await rig.drone(drone_id)).status is D.RETURNING
        assert rig.commands() == ["upload", "start", "return_to_home"]

    async def test_the_intent_is_recorded_before_the_command_is_sent(self, rig: Rig) -> None:
        """If the process dies with the command in flight, the record already says started."""
        seen: list[FlightStatus] = []

        original = rig.link.start_mission

        async def spy(drone_id: UUID, authorization: Any) -> None:
            seen.append((await rig.dispatch.get_flight(authorization.flight_id)).status)
            await original(drone_id, authorization)

        rig.link.start_mission = spy  # type: ignore[method-assign]
        await dispatch_one(rig)

        assert seen == [S.STARTED]


# =============================================================================================
# Pause, resume, return-to-home, land
# =============================================================================================


async def flying(rig: Rig) -> tuple[UUID, UUID, UUID]:
    """A started flight: ``(order_id, flight_id, drone_id)``."""
    result, drone_id = await dispatch_one(rig)
    assert result.flight is not None
    return result.order.id, result.flight.id, drone_id


class TestPauseAndResume:
    async def test_pause_sends_the_command_then_records_it(self, rig: Rig) -> None:
        order_id, flight_id, drone_id = await flying(rig)

        paused = await rig.dispatch.pause_flight(flight_id)

        assert paused.status is S.PAUSED
        assert rig.commands() == ["upload", "start", "pause"]
        assert (await rig.drone(drone_id)).status is D.ON_MISSION  # hovering, still on mission
        assert (await rig.order(order_id)).status is OrderStatus.ASSIGNED

    async def test_an_unconfirmed_pause_records_nothing(self, rig: Rig) -> None:
        _, flight_id, _ = await flying(rig)
        rig.link.set_failure("pause", NoAcknowledgementError("no answer"))

        with pytest.raises(CommandNotConfirmedError) as excinfo:
            await rig.dispatch.pause_flight(flight_id)

        assert excinfo.value.command == "pause"
        assert (await rig.dispatch.get_flight(flight_id)).status is S.STARTED
        assert rig.commands() == ["upload", "start"]

    async def test_a_pause_of_a_flight_that_is_not_flying_sends_nothing(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig, start=False)
        assert result.flight is not None

        with pytest.raises(InvalidFlightTransitionError):
            await rig.dispatch.pause_flight(result.flight.id)

        assert rig.commands() == ["upload"]

    async def test_resume_runs_the_gate_again_and_sends_a_start(self, rig: Rig) -> None:
        _, flight_id, _ = await flying(rig)
        await rig.dispatch.pause_flight(flight_id)

        resumed = await rig.dispatch.resume_flight(flight_id)

        assert resumed.status is S.STARTED
        assert rig.commands() == ["upload", "start", "pause", "start"]
        await assert_rule2(rig)

    async def test_a_revoked_permission_blocks_the_resume_but_not_a_recall(self, rig: Rig) -> None:
        _, flight_id, _ = await flying(rig)
        await rig.dispatch.pause_flight(flight_id)
        rig.compliance.revoke(rig.compliance.issued[0].permission_id)

        with pytest.raises(PermissionRevokedError):
            await rig.dispatch.resume_flight(flight_id)
        assert (await rig.dispatch.get_flight(flight_id)).status is S.PAUSED
        assert rig.commands() == ["upload", "start", "pause"]  # no second start

        # Safety is never gated: the revoked drone can still be brought home.
        aborted = await rig.dispatch.return_to_home(flight_id)
        assert aborted.status is S.ABORTED
        assert rig.commands()[-1] == "return_to_home"
        await assert_rule2(rig)

    async def test_a_refused_resume_leaves_the_drone_paused_and_the_order_alive(
        self, rig: Rig
    ) -> None:
        order_id, flight_id, drone_id = await flying(rig)
        await rig.dispatch.pause_flight(flight_id)
        rig.link.set_failure("start", CommandRejectedError("not ready"))

        with pytest.raises(StartCommandFailedError) as excinfo:
            await rig.dispatch.resume_flight(flight_id)

        assert excinfo.value.flight_aborted is False
        flight = await rig.dispatch.get_flight(flight_id)
        assert flight.status is S.PAUSED  # still hovering; the record says so again
        assert (await rig.drone(drone_id)).status is D.ON_MISSION
        assert (await rig.order(order_id)).status is OrderStatus.ASSIGNED
        assert "return_to_home" not in rig.commands()

        rig.link.set_failure("start", None)  # it can be resumed once the drone is ready
        assert (await rig.dispatch.resume_flight(flight_id)).status is S.STARTED

    async def test_an_unconfirmed_resume_recalls_the_drone_and_ends_the_flight(
        self, rig: Rig
    ) -> None:
        order_id, flight_id, drone_id = await flying(rig)
        await rig.dispatch.pause_flight(flight_id)
        rig.link.set_failure("start", NoAcknowledgementError("timed out"), executed=True)

        with pytest.raises(StartCommandFailedError) as excinfo:
            await rig.dispatch.resume_flight(flight_id)

        assert excinfo.value.unconfirmed is True
        assert excinfo.value.flight_aborted is True
        assert (await rig.dispatch.get_flight(flight_id)).status is S.ABORTED
        assert (await rig.drone(drone_id)).status is D.RETURNING
        assert (await rig.order(order_id)).status is OrderStatus.FAILED
        assert rig.commands()[-1] == "return_to_home"

    async def test_resume_of_a_flight_that_is_not_paused(self, rig: Rig) -> None:
        _, flight_id, _ = await flying(rig)

        with pytest.raises(InvalidFlightTransitionError):
            await rig.dispatch.resume_flight(flight_id)


class TestReturnToHomeAndLand:
    async def test_return_to_home_aborts_the_flight_and_sends_the_drone_home(
        self, rig: Rig
    ) -> None:
        _, flight_id, drone_id = await flying(rig)

        flight = await rig.dispatch.return_to_home(flight_id, reason="storm")

        assert flight.status is S.ABORTED
        assert flight.failure_reason is not None
        assert "storm" in flight.failure_reason
        assert (await rig.drone(drone_id)).status is D.RETURNING
        assert rig.commands() == ["upload", "start", "return_to_home"]

    async def test_land_aborts_the_flight_and_grounds_the_drone(self, rig: Rig) -> None:
        _, flight_id, drone_id = await flying(rig)

        flight = await rig.dispatch.land(flight_id)

        assert flight.status is S.ABORTED
        assert (await rig.drone(drone_id)).status is D.OFFLINE  # away from its dock
        assert rig.commands() == ["upload", "start", "land"]

    @pytest.mark.parametrize("command", ["return_to_home", "land"])
    async def test_an_unconfirmed_safety_command_records_nothing(
        self, rig: Rig, command: str
    ) -> None:
        _, flight_id, drone_id = await flying(rig)
        rig.link.set_failure(command, NoAcknowledgementError("no answer"))  # type: ignore[arg-type]

        with pytest.raises(CommandNotConfirmedError):
            await getattr(rig.dispatch, command)(flight_id)

        assert (await rig.dispatch.get_flight(flight_id)).status is S.STARTED
        assert (await rig.drone(drone_id)).status is D.ON_MISSION
        assert command not in rig.commands()

    @pytest.mark.parametrize("command", ["return_to_home", "land"])
    async def test_they_work_from_a_paused_flight(self, rig: Rig, command: str) -> None:
        _, flight_id, _ = await flying(rig)
        await rig.dispatch.pause_flight(flight_id)

        flight = await getattr(rig.dispatch, command)(flight_id)

        assert flight.status is S.ABORTED

    @pytest.mark.parametrize("command", ["return_to_home", "land"])
    async def test_they_are_never_gated_by_permission(self, rig: Rig, command: str) -> None:
        _, flight_id, _ = await flying(rig)
        validations_before = rig.compliance.validations
        rig.compliance.set_outage(True)  # even with the compliance provider down
        rig.compliance.revoke(rig.compliance.issued[0].permission_id)

        flight = await getattr(rig.dispatch, command)(flight_id)

        assert flight.status is S.ABORTED
        assert rig.compliance.validations == validations_before  # compliance is not consulted

    @pytest.mark.parametrize("command", ["return_to_home", "land", "pause_flight"])
    async def test_a_flight_that_has_not_started_cannot_be_recalled_or_landed(
        self, rig: Rig, command: str
    ) -> None:
        result, _ = await dispatch_one(rig, start=False)
        assert result.flight is not None

        with pytest.raises(InvalidFlightTransitionError):
            await getattr(rig.dispatch, command)(result.flight.id)

        assert rig.commands() == ["upload"]

    @pytest.mark.parametrize("command", ["return_to_home", "land", "pause_flight"])
    async def test_a_finished_flight_is_terminal(self, rig: Rig, command: str) -> None:
        _, flight_id, _ = await flying(rig)
        await rig.dispatch.return_to_home(flight_id)
        sent = list(rig.link.commands)

        with pytest.raises(TerminalFlightStateError):
            await getattr(rig.dispatch, command)(flight_id)

        assert rig.link.commands == sent

    @pytest.mark.parametrize("command", ["return_to_home", "land", "pause_flight"])
    async def test_an_unknown_flight(self, rig: Rig, command: str) -> None:
        with pytest.raises(FlightNotFoundError):
            await getattr(rig.dispatch, command)(UUID(int=404))


# =============================================================================================
# Cancellation
# =============================================================================================


class TestCancelOrder:
    async def test_a_created_order_with_no_flight(self, rig: Rig) -> None:
        order = await rig.new_order()

        result = await rig.dispatch.cancel_order(order.id, source=EventSource.API)

        assert result.order.status is OrderStatus.CANCELLED
        assert result.flight is None
        assert rig.commands() == []

    async def test_a_staged_flight_is_cancelled_and_the_drone_released(self, rig: Rig) -> None:
        dispatched, drone_id = await dispatch_one(rig, start=False)

        result = await rig.dispatch.cancel_order(dispatched.order.id, source=EventSource.API)

        assert result.order.status is OrderStatus.CANCELLED
        assert result.flight is not None
        assert result.flight.status is S.CANCELLED
        assert (await rig.drone(drone_id)).status is D.AVAILABLE
        assert rig.commands() == ["upload"]  # nothing flying, so nothing to recall

    async def test_a_flying_drone_is_recalled_before_the_order_is_cancelled(self, rig: Rig) -> None:
        order_id, flight_id, drone_id = await flying(rig)

        result = await rig.dispatch.cancel_order(order_id, source=EventSource.API, reason="oops")

        assert result.order.status is OrderStatus.CANCELLED
        assert result.flight is not None
        assert result.flight.id == flight_id
        assert result.flight.status is S.ABORTED
        assert (await rig.drone(drone_id)).status is D.RETURNING
        assert rig.commands() == ["upload", "start", "return_to_home"]
        steps = await rig.steps(order_id)
        assert steps[-2:] == [("flight", "aborted"), ("order", "cancelled")]

    async def test_a_paused_drone_is_recalled_too(self, rig: Rig) -> None:
        order_id, flight_id, _ = await flying(rig)
        await rig.dispatch.pause_flight(flight_id)

        result = await rig.dispatch.cancel_order(order_id, source=EventSource.API)

        assert result.order.status is OrderStatus.CANCELLED
        assert rig.commands()[-1] == "return_to_home"

    async def test_if_the_recall_cannot_be_confirmed_the_order_is_not_cancelled(
        self, rig: Rig
    ) -> None:
        order_id, flight_id, drone_id = await flying(rig)
        rig.link.set_failure("return_to_home", NoAcknowledgementError("no answer"))

        with pytest.raises(CommandNotConfirmedError):
            await rig.dispatch.cancel_order(order_id, source=EventSource.API)

        # Nothing changed: the drone is still flying, so the order must still be live.
        assert (await rig.order(order_id)).status is OrderStatus.ASSIGNED
        assert (await rig.dispatch.get_flight(flight_id)).status is S.STARTED
        assert (await rig.drone(drone_id)).status is D.ON_MISSION

        rig.link.set_failure("return_to_home", None)  # retryable
        result = await rig.dispatch.cancel_order(order_id, source=EventSource.API)
        assert result.order.status is OrderStatus.CANCELLED

    async def test_cancelling_twice_is_idempotent(self, rig: Rig) -> None:
        order_id, _, _ = await flying(rig)
        await rig.dispatch.cancel_order(order_id, source=EventSource.API)
        sent = list(rig.link.commands)
        events = len(await rig.trail(order_id))

        again = await rig.dispatch.cancel_order(order_id, source=EventSource.API)

        assert again.order.status is OrderStatus.CANCELLED
        assert again.flight is None
        assert rig.link.commands == sent
        assert len(await rig.trail(order_id)) == events

    async def test_a_cancelled_order_can_not_be_dispatched(self, rig: Rig) -> None:
        await rig.add_drone()
        order = await rig.new_order()
        await rig.dispatch.cancel_order(order.id, source=EventSource.API)

        with pytest.raises(OrderNotDispatchableError):
            await rig.dispatch.dispatch_order(order.id)

    async def test_a_failed_order_can_not_be_cancelled(self, rig: Rig) -> None:
        rig.link.set_failure("start", CommandRejectedError("no"))
        result, _ = await dispatch_one(rig)
        assert result.order.status is OrderStatus.FAILED

        with pytest.raises(TerminalStateError):
            await rig.dispatch.cancel_order(result.order.id, source=EventSource.API)

    async def test_an_unknown_order(self, rig: Rig) -> None:
        with pytest.raises(OrderNotFoundError):
            await rig.dispatch.cancel_order(UUID(int=404), source=EventSource.API)

    async def test_cancelling_during_the_upload_wins(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()

        async def cancel() -> None:
            await rig.dispatch.cancel_order(order.id, source=EventSource.API)

        rig.link.on_upload = cancel

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.CANCELLED_DURING_DISPATCH
        assert result.order.status is OrderStatus.CANCELLED
        assert result.flight is not None
        assert result.flight.status is S.CANCELLED
        assert (await rig.drone(drone.id)).status is D.AVAILABLE
        assert "start" not in rig.commands()
        await assert_rule2(rig)

    async def test_cancelling_just_before_the_order_is_assigned_wins(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()

        async def cancel() -> None:
            await rig.dispatch.cancel_order(order.id, source=EventSource.API)

        rig.orders.before_assign = cancel

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.CANCELLED_DURING_DISPATCH
        assert result.order.status is OrderStatus.CANCELLED
        assert result.flight is not None
        assert result.flight.status is S.CANCELLED
        assert (await rig.drone(drone.id)).status is D.AVAILABLE
        assert "start" not in rig.commands()
        await assert_rule2(rig)


# =============================================================================================
# Dispatch preconditions
# =============================================================================================


class TestDispatchPreconditions:
    async def test_an_unknown_order(self, rig: Rig) -> None:
        with pytest.raises(OrderNotFoundError):
            await rig.dispatch.dispatch_order(UUID(int=404))

    async def test_an_order_that_is_not_created(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        with pytest.raises(OrderNotDispatchableError) as excinfo:
            await rig.dispatch.dispatch_order(result.order.id)

        assert excinfo.value.status is OrderStatus.ASSIGNED

    async def test_an_order_that_already_has_a_live_flight(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()
        planned = FlightStateMachine(clock=rig.clock).create(
            order_id=order.id,
            drone_id=drone.id,
            mission=build_mission(),
            source=EventSource.DISPATCH,
        )
        async with rig.uow_factory() as uow:
            assert await uow.flights.insert(planned)
            await uow.commit()

        with pytest.raises(FlightInProgressError) as excinfo:
            await rig.dispatch.dispatch_order(order.id)

        assert excinfo.value.flight_id == planned.flight.id
        assert rig.compliance.requests == []

    async def test_a_second_dispatch_after_the_first_is_refused(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig, start=False)

        with pytest.raises(OrderNotDispatchableError):
            await rig.dispatch.dispatch_order(result.order.id)


# =============================================================================================
# The fleet
# =============================================================================================


class TestFleet:
    async def test_register_and_read_back(self, rig: Rig) -> None:
        drone = await rig.add_drone("alpha")

        assert drone.status is D.AVAILABLE
        assert await rig.dispatch.get_drone(drone.id) == drone
        assert await rig.dispatch.list_drones() == [drone]

    async def test_a_duplicate_name_is_refused(self, rig: Rig) -> None:
        await rig.add_drone("alpha")

        with pytest.raises(DuplicateDroneError):
            await rig.add_drone("alpha")

    async def test_an_unknown_drone(self, rig: Rig) -> None:
        with pytest.raises(DroneNotFoundError):
            await rig.dispatch.get_drone(UUID(int=404))

    async def test_list_can_filter_by_status(self, rig: Rig) -> None:
        a = await rig.add_drone("a")
        b = await rig.add_drone("b")
        await rig.dispatch.set_maintenance(b.id)

        assert [d.id for d in await rig.dispatch.list_drones(status=D.AVAILABLE)] == [a.id]
        assert [d.id for d in await rig.dispatch.list_drones(status=D.MAINTENANCE)] == [b.id]

    async def test_maintenance_takes_a_drone_out_of_dispatch(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        await rig.dispatch.set_maintenance(drone.id)
        order = await rig.new_order()

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.NO_DRONE_AVAILABLE

    async def test_docked_returns_a_drone_to_service(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        await rig.dispatch.set_maintenance(drone.id)

        docked = await rig.dispatch.confirm_docked(drone.id)

        assert docked.status is D.AVAILABLE

    async def test_docked_is_idempotent(self, rig: Rig) -> None:
        drone = await rig.add_drone()

        assert (await rig.dispatch.confirm_docked(drone.id)).status is D.AVAILABLE

    async def test_a_recalled_drone_is_docked_by_hand(self, rig: Rig) -> None:
        _, flight_id, drone_id = await flying(rig)
        await rig.dispatch.return_to_home(flight_id)

        docked = await rig.dispatch.confirm_docked(drone_id)

        assert docked.status is D.AVAILABLE

    async def test_a_drone_with_a_live_flight_cannot_be_changed_by_hand(self, rig: Rig) -> None:
        _, _, drone_id = await flying(rig)

        with pytest.raises(DroneBusyError):
            await rig.dispatch.confirm_docked(drone_id)
        with pytest.raises(DroneBusyError):
            await rig.dispatch.set_maintenance(drone_id)

        assert (await rig.drone(drone_id)).status is D.ON_MISSION

    async def test_an_illegal_status_change_is_refused(self, rig: Rig) -> None:
        _, flight_id, drone_id = await flying(rig)
        await rig.dispatch.return_to_home(flight_id)  # the drone is now returning

        with pytest.raises(InvalidDroneTransitionError):
            await rig.dispatch.set_maintenance(drone_id)

    async def test_status_changes_for_an_unknown_drone(self, rig: Rig) -> None:
        with pytest.raises(DroneNotFoundError):
            await rig.dispatch.confirm_docked(UUID(int=404))
        with pytest.raises(DroneNotFoundError):
            await rig.dispatch.set_maintenance(UUID(int=404))


class TestReads:
    async def test_flights_of_an_unknown_order(self, rig: Rig) -> None:
        with pytest.raises(OrderNotFoundError):
            await rig.dispatch.list_flights_for_order(UUID(int=404))

    async def test_an_unknown_flight(self, rig: Rig) -> None:
        with pytest.raises(FlightNotFoundError):
            await rig.dispatch.get_flight(UUID(int=404))


# =============================================================================================
# Rule 2 as a property of every scenario
# =============================================================================================


def _revoke_on(n: int, rig: Rig) -> Callable[[int, str], None]:
    return lambda count, pid: rig.compliance.revoke(pid) if count == n else None


Scenario = tuple[str, Callable[[Rig], Awaitable[None]], DispatchOutcome, int]


async def _noop(rig: Rig) -> None:
    pass


async def _airspace_closed(rig: Rig) -> None:
    rig.compliance.close_airspace("closed")


async def _outage_at_request(rig: Rig) -> None:
    rig.compliance.set_outage(True)


async def _revoked_after_grant(rig: Rig) -> None:
    rig.compliance.on_validate = _revoke_on(1, rig)


async def _outage_after_grant(rig: Rig) -> None:
    rig.compliance.on_validate = lambda n, _: rig.compliance.set_outage(True)


async def _revoked_at_gate(rig: Rig) -> None:
    rig.compliance.on_validate = _revoke_on(2, rig)


async def _outage_at_gate(rig: Rig) -> None:
    rig.compliance.on_validate = lambda n, _: rig.compliance.set_outage(n >= 2)


async def _upload_rejected(rig: Rig) -> None:
    rig.link.set_failure("upload", CommandRejectedError("no"))


async def _upload_unanswered(rig: Rig) -> None:
    rig.link.set_failure("upload", NoAcknowledgementError("no"))


async def _upload_wrong_ack(rig: Rig) -> None:
    rig.link.corrupt_next_ack()


async def _start_rejected(rig: Rig) -> None:
    rig.link.set_failure("start", CommandRejectedError("no"))


async def _cancelled_mid_upload(rig: Rig) -> None:
    async def cancel() -> None:
        await rig.dispatch.cancel_order(rig.created[-1].id, source=EventSource.API)

    rig.link.on_upload = cancel


SCENARIOS: list[Scenario] = [
    ("happy path", _noop, O.STARTED, 1),
    ("airspace closed", _airspace_closed, O.PERMISSION_DENIED, 0),
    ("outage at the request", _outage_at_request, O.PERMISSION_DENIED, 0),
    ("revoked after the grant", _revoked_after_grant, O.PERMISSION_INVALID, 0),
    ("outage after the grant", _outage_after_grant, O.PERMISSION_INVALID, 0),
    ("revoked at the gate", _revoked_at_gate, O.START_FAILED, 0),
    ("outage at the gate", _outage_at_gate, O.START_FAILED, 0),
    ("upload rejected", _upload_rejected, O.UPLOAD_FAILED, 0),
    ("upload unanswered", _upload_unanswered, O.UPLOAD_FAILED, 0),
    ("upload acknowledged wrongly", _upload_wrong_ack, O.UPLOAD_FAILED, 0),
    ("start refused by the drone", _start_rejected, O.START_FAILED, 0),
    ("cancelled mid-upload", _cancelled_mid_upload, O.CANCELLED_DURING_DISPATCH, 0),
]


class TestRule2OnEveryPath:
    """No failure path may ever deliver a start command to a drone."""

    @pytest.mark.parametrize(
        ("setup", "outcome", "starts"),
        [pytest.param(s, o, n, id=name) for name, s, o, n in SCENARIOS],
    )
    async def test_a_start_only_follows_a_successful_gate(
        self,
        rig: Rig,
        setup: Callable[[Rig], Awaitable[None]],
        outcome: DispatchOutcome,
        starts: int,
    ) -> None:
        await setup(rig)
        drone = await rig.add_drone()
        order = await rig.new_order()

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is outcome
        assert len(rig.link.operations("start")) == starts
        await assert_rule2(rig)
        # and the drone is never left reserved by a dispatch that did not end in a flight
        if not result.succeeded:
            assert (await rig.drone(drone.id)).status is D.AVAILABLE
