"""``DispatchService.record_progress``: what a drone's reports may and may not change.

A drone reports what happened; the service applies it only if it fits, exactly once, and
never sends a command in response. Every event type is exercised against every state it
could meet.
"""

from __future__ import annotations

from itertools import pairwise
from uuid import UUID

import pytest
import structlog

from app.dispatch.progress import (
    FORWARD_PATH,
    ORDER_STEP,
    ProgressEvent,
    ProgressOutcome,
    ProgressResult,
    at_or_beyond,
)
from app.domain.models import (
    AuditEventType,
    DroneStatus,
    EventSource,
    FlightStatus,
    OrderStatus,
)
from tests.conftest import FakeClock
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

P = ProgressEvent
O = ProgressOutcome  # noqa: E741
S = FlightStatus
D = DroneStatus
OS = OrderStatus

JOURNEY = [P.PACKAGE_PICKED_UP, P.DEPARTED_PICKUP, P.ARRIVING_AT_DROPOFF, P.PACKAGE_DELIVERED]


def _skipping_pairs() -> list[tuple[ProgressEvent, int]]:
    """(event, steps already reported) where the event is neither the next step nor a repeat."""
    pairs = []
    for event in JOURNEY:
        source, target = ORDER_STEP[event]
        for reached in range(4):
            status = FORWARD_PATH[1 + reached]  # assigned after 0 steps, picked_up after 1, ...
            if status is not source and not at_or_beyond(status, target):
                pairs.append((event, reached))
    return pairs


SKIPPING_PAIRS = _skipping_pairs()


@pytest.fixture
def rig(clock: FakeClock) -> Rig:
    database = InMemoryDatabase()
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


class Flying:
    """A started flight, and shortcuts to report things about it."""

    def __init__(self, rig: Rig, order_id: UUID, flight_id: UUID, drone_id: UUID) -> None:
        self.rig, self.order_id, self.flight_id, self.drone_id = rig, order_id, flight_id, drone_id

    async def report(self, event: ProgressEvent, **kwargs: object) -> ProgressResult:
        return await self.rig.dispatch.record_progress(
            self.drone_id,
            self.flight_id,
            event,
            **kwargs,  # type: ignore[arg-type]
        )

    async def journey(self, upto: int) -> None:
        for event in JOURNEY[:upto]:
            assert (await self.report(event)).applied, event

    async def order_status(self) -> OrderStatus:
        return (await self.rig.order(self.order_id)).status


async def flying(rig: Rig) -> Flying:
    drone = await rig.add_drone()
    order = await rig.new_order()
    result = await rig.dispatch.dispatch_order(order.id)
    assert result.flight is not None
    return Flying(rig, order.id, result.flight.id, drone.id)


class TestTheOrdersJourney:
    async def test_each_event_moves_the_order_one_step(self, rig: Rig) -> None:
        f = await flying(rig)

        seen = []
        for event in JOURNEY:
            result = await f.report(event)
            assert result.outcome is O.APPLIED, event
            seen.append(await f.order_status())

        assert seen == [OS.PICKED_UP, OS.IN_FLIGHT, OS.ARRIVING, OS.DELIVERED]

    async def test_completion_ends_the_flight_and_frees_the_drone(self, rig: Rig) -> None:
        f = await flying(rig)
        await f.journey(4)

        result = await f.report(P.MISSION_COMPLETED)

        assert result.applied
        assert (await rig.dispatch.get_flight(f.flight_id)).status is S.COMPLETED
        assert (await rig.drone(f.drone_id)).status is D.AVAILABLE
        assert await f.order_status() is OS.DELIVERED

    async def test_the_drone_can_take_the_next_delivery(self, rig: Rig) -> None:
        f = await flying(rig)
        await f.journey(4)
        await f.report(P.MISSION_COMPLETED)
        second = await rig.new_order()

        result = await rig.dispatch.dispatch_order(second.id)

        assert result.succeeded
        assert result.drone is not None
        assert result.drone.id == f.drone_id

    async def test_the_audit_trail_records_every_step_as_telemetry(self, rig: Rig) -> None:
        f = await flying(rig)
        await f.journey(4)
        await f.report(P.MISSION_COMPLETED)

        trail = await rig.trail(f.order_id)

        telemetry = [e for e in trail if e.source is EventSource.TELEMETRY]
        assert [(e.event_type, e.new_state.value) for e in telemetry] == [
            (AuditEventType.ORDER_STATUS_CHANGED, "picked_up"),
            (AuditEventType.ORDER_STATUS_CHANGED, "in_flight"),
            (AuditEventType.ORDER_STATUS_CHANGED, "arriving"),
            (AuditEventType.ORDER_STATUS_CHANGED, "delivered"),
            (AuditEventType.FLIGHT_STATUS_CHANGED, "completed"),
        ]
        assert all(e.metadata["flight_id"] == str(f.flight_id) for e in telemetry)

    async def test_the_audit_keeps_the_event_and_its_time_but_not_free_text(
        self, rig: Rig, clock: FakeClock
    ) -> None:
        f = await flying(rig)

        await f.report(
            P.PACKAGE_PICKED_UP,
            occurred_at=clock(),
            detail={"note": "drone-supplied free text " * 50},
        )

        event = next(e for e in await rig.trail(f.order_id) if e.new_state is OS.PICKED_UP)
        assert dict(event.metadata) == {
            "flight_id": str(f.flight_id),
            "event": "package_picked_up",
            "occurred_at": clock().isoformat(),
        }

    async def test_reporting_never_sends_a_command(self, rig: Rig) -> None:
        f = await flying(rig)
        sent = list(rig.link.commands)

        await f.journey(4)
        await f.report(P.MISSION_COMPLETED)

        assert rig.link.commands == sent


class TestRepeatsAreHarmless:
    @pytest.mark.parametrize("step", range(1, 5))
    async def test_a_repeated_event_is_already_applied_and_changes_nothing(
        self, rig: Rig, step: int
    ) -> None:
        f = await flying(rig)
        await f.journey(step)
        events_before = len(await rig.trail(f.order_id))

        again = await f.report(JOURNEY[step - 1])

        assert again.outcome is O.ALREADY_APPLIED
        assert len(await rig.trail(f.order_id)) == events_before

    async def test_an_earlier_event_after_a_later_one_is_already_applied(self, rig: Rig) -> None:
        f = await flying(rig)
        await f.journey(3)

        assert (await f.report(P.PACKAGE_PICKED_UP)).outcome is O.ALREADY_APPLIED
        assert await f.order_status() is OS.ARRIVING

    async def test_a_repeated_completion(self, rig: Rig) -> None:
        f = await flying(rig)
        await f.journey(4)
        await f.report(P.MISSION_COMPLETED)

        assert (await f.report(P.MISSION_COMPLETED)).outcome is O.ALREADY_APPLIED

    async def test_a_repeated_delivery_after_the_flight_ended_is_still_a_repeat(
        self, rig: Rig
    ) -> None:
        f = await flying(rig)
        await f.journey(4)
        await f.report(P.MISSION_COMPLETED)

        assert (await f.report(P.PACKAGE_DELIVERED)).outcome is O.ALREADY_APPLIED


class TestEventsThatDoNotFit:
    @pytest.mark.parametrize(("event", "reached"), SKIPPING_PAIRS)
    async def test_an_event_that_skips_a_step_is_rejected_and_nothing_changes(
        self, rig: Rig, event: ProgressEvent, reached: int
    ) -> None:
        f = await flying(rig)
        await f.journey(reached)
        source_status, _ = ORDER_STEP[event]
        before = await f.order_status()
        events_before = len(await rig.trail(f.order_id))

        result = await f.report(event)

        assert result.outcome is O.REJECTED
        assert result.reason is not None
        assert source_status.value in result.reason
        assert await f.order_status() is before
        assert len(await rig.trail(f.order_id)) == events_before

    async def test_an_unknown_flight(self, rig: Rig) -> None:
        f = await flying(rig)

        result = await rig.dispatch.record_progress(f.drone_id, UUID(int=404), P.PACKAGE_PICKED_UP)

        assert result.outcome is O.REJECTED
        assert result.reason == "unknown flight"

    async def test_a_flight_of_another_drone(self, rig: Rig) -> None:
        f = await flying(rig)
        other = await rig.add_drone("other")

        result = await rig.dispatch.record_progress(other.id, f.flight_id, P.PACKAGE_PICKED_UP)

        assert result.outcome is O.REJECTED
        assert result.reason == "the flight belongs to another drone"
        assert await f.order_status() is OS.ASSIGNED

    async def test_a_flight_that_has_not_started_is_not_flying(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()
        staged = await rig.dispatch.dispatch_order(order.id, start=False)
        assert staged.flight is not None

        result = await rig.dispatch.record_progress(drone.id, staged.flight.id, P.PACKAGE_PICKED_UP)

        assert result.outcome is O.REJECTED
        assert result.reason is not None
        assert "not flying" in result.reason

    async def test_events_for_a_cancelled_order_are_rejected(self, rig: Rig) -> None:
        f = await flying(rig)
        await f.journey(2)
        await rig.dispatch.cancel_order(f.order_id, source=EventSource.API)

        result = await f.report(P.ARRIVING_AT_DROPOFF)

        assert result.outcome is O.REJECTED
        assert await f.order_status() is OS.CANCELLED

    async def test_a_late_delivery_for_a_cancelled_order_does_not_resurrect_it(
        self, rig: Rig
    ) -> None:
        f = await flying(rig)
        await f.journey(3)
        await rig.dispatch.cancel_order(f.order_id, source=EventSource.API)

        result = await f.report(P.PACKAGE_DELIVERED)

        assert result.outcome is O.REJECTED
        assert await f.order_status() is OS.CANCELLED

    async def test_completion_of_a_paused_flight_is_rejected(self, rig: Rig) -> None:
        f = await flying(rig)
        await f.journey(4)
        await rig.dispatch.pause_flight(f.flight_id)

        result = await f.report(P.MISSION_COMPLETED)

        assert result.outcome is O.REJECTED
        assert (await rig.dispatch.get_flight(f.flight_id)).status is S.PAUSED
        assert (await rig.drone(f.drone_id)).status is D.ON_MISSION

    async def test_completion_of_an_aborted_flight_is_rejected(self, rig: Rig) -> None:
        f = await flying(rig)
        await rig.dispatch.return_to_home(f.flight_id)

        result = await f.report(P.MISSION_COMPLETED)

        assert result.outcome is O.REJECTED
        assert (await rig.drone(f.drone_id)).status is D.RETURNING

    async def test_a_completion_without_a_reported_delivery_is_applied_but_noted(
        self, rig: Rig
    ) -> None:
        f = await flying(rig)

        with structlog.testing.capture_logs() as logs:
            result = await f.report(P.MISSION_COMPLETED)

        assert result.applied
        assert await f.order_status() is OS.ASSIGNED  # nothing was invented
        assert any(entry["event"] == "mission_completed_without_delivery" for entry in logs)

    async def test_events_while_paused_are_accepted(self, rig: Rig) -> None:
        f = await flying(rig)
        await rig.dispatch.pause_flight(f.flight_id)

        assert (await f.report(P.PACKAGE_PICKED_UP)).applied


class TestDocking:
    async def test_a_recalled_drone_that_docks_is_available_again(self, rig: Rig) -> None:
        f = await flying(rig)
        await rig.dispatch.return_to_home(f.flight_id)
        assert (await rig.drone(f.drone_id)).status is D.RETURNING

        result = await f.report(P.DOCKED)

        assert result.applied
        assert (await rig.drone(f.drone_id)).status is D.AVAILABLE

    async def test_a_grounded_drone_that_is_recovered_is_available_again(self, rig: Rig) -> None:
        f = await flying(rig)
        await rig.dispatch.land(f.flight_id)
        assert (await rig.drone(f.drone_id)).status is D.OFFLINE

        assert (await f.report(P.DOCKED)).applied
        assert (await rig.drone(f.drone_id)).status is D.AVAILABLE

    async def test_docked_twice(self, rig: Rig) -> None:
        f = await flying(rig)
        await rig.dispatch.return_to_home(f.flight_id)
        await f.report(P.DOCKED)

        assert (await f.report(P.DOCKED)).outcome is O.ALREADY_APPLIED

    async def test_docked_while_the_flight_is_live_is_rejected(self, rig: Rig) -> None:
        f = await flying(rig)

        result = await f.report(P.DOCKED)

        assert result.outcome is O.REJECTED
        assert (await rig.drone(f.drone_id)).status is D.ON_MISSION

    async def test_docked_does_not_pull_a_drone_out_of_maintenance(self, rig: Rig) -> None:
        f = await flying(rig)
        await rig.dispatch.land(f.flight_id)
        await rig.dispatch.set_maintenance(f.drone_id)

        result = await f.report(P.DOCKED)

        assert result.outcome is O.REJECTED
        assert (await rig.drone(f.drone_id)).status is D.MAINTENANCE


class TestFaults:
    async def test_a_fault_is_logged_and_changes_nothing(self, rig: Rig) -> None:
        f = await flying(rig)
        events_before = len(await rig.trail(f.order_id))

        with structlog.testing.capture_logs() as logs:
            result = await f.report(P.FAULT, detail={"code": "GPS_LOSS"})

        assert result.outcome is O.NOTED
        assert any(
            e["event"] == "drone_fault" and e["detail"] == {"code": "GPS_LOSS"} for e in logs
        )
        assert (await rig.dispatch.get_flight(f.flight_id)).status is S.STARTED
        assert (await rig.drone(f.drone_id)).status is D.ON_MISSION
        assert len(await rig.trail(f.order_id)) == events_before

    async def test_a_fault_never_triggers_a_command(self, rig: Rig) -> None:
        """Decision: faults are alerts only; whether to auto-recall is a policy for later."""
        f = await flying(rig)
        sent = list(rig.link.commands)

        await f.report(P.FAULT, detail={"code": "BATTERY_CRITICAL"})

        assert rig.link.commands == sent

    async def test_a_fault_for_an_unknown_flight_is_still_logged(self, rig: Rig) -> None:
        with structlog.testing.capture_logs() as logs:
            result = await rig.dispatch.record_progress(UUID(int=1), UUID(int=2), P.FAULT)

        assert result.outcome is O.NOTED
        assert any(entry["event"] == "drone_fault" for entry in logs)


class TestTheVocabulary:
    def test_the_skipping_cases_are_all_enumerated(self) -> None:
        # event i can be skipped to from every earlier state: 0+1+2+3 = 6 pairs
        assert len(SKIPPING_PAIRS) == 6

    def test_the_forward_path_is_the_order_lifecycle(self) -> None:
        assert [s.value for s in FORWARD_PATH] == [
            "created",
            "assigned",
            "picked_up",
            "in_flight",
            "arriving",
            "delivered",
        ]

    def test_each_order_event_is_one_step_on_the_forward_path(self) -> None:
        for source, target in ORDER_STEP.values():
            assert FORWARD_PATH.index(target) == FORWARD_PATH.index(source) + 1

    def test_the_steps_chain_from_assigned_to_delivered(self) -> None:
        chain = [ORDER_STEP[event] for event in JOURNEY]
        assert chain[0][0] is OS.ASSIGNED
        assert chain[-1][1] is OS.DELIVERED
        assert all(a[1] is b[0] for a, b in pairwise(chain))

    @pytest.mark.parametrize(
        ("status", "target", "expected"),
        [
            (OS.ASSIGNED, OS.PICKED_UP, False),
            (OS.PICKED_UP, OS.PICKED_UP, True),
            (OS.DELIVERED, OS.PICKED_UP, True),
            (OS.CANCELLED, OS.PICKED_UP, False),
            (OS.FAILED, OS.DELIVERED, False),
            (OS.RETURNED, OS.DELIVERED, False),
        ],
    )
    def test_at_or_beyond(self, status: OrderStatus, target: OrderStatus, expected: bool) -> None:
        assert at_or_beyond(status, target) is expected
