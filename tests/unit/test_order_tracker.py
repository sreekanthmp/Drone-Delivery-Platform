"""The order tracker: what a stream believes about one order, its flight and its drone."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.api.ws.tracker import PARTNER_VISIBLE, PLATFORM_VISIBLE, OrderTracker
from app.domain.models import LIVE_FLIGHT_STATES, EventSource, OrderStatus
from app.protocol.messages import DronePhase
from app.realtime.channels import drone_channel, order_channel
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import DroneState, Envelope
from app.realtime.notifier import audit_envelope
from tests.conftest import FakeClock
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

S = OrderStatus


@pytest.fixture
def rig(clock: FakeClock) -> Rig:
    database = InMemoryDatabase()
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


@pytest.fixture
def live() -> InMemoryRealtime:
    return InMemoryRealtime(clock=lambda: datetime.now(UTC))


def sample_for(drone_id: UUID, flight_id: UUID | None, **extra: float) -> DroneState:
    now = datetime.now(UTC)
    return DroneState(
        drone_id=drone_id,
        latitude=extra.get("latitude", 12.95),
        longitude=77.6,
        altitude_m=50.0,
        battery_percent=80.0,
        phase=DronePhase.FLYING,
        flight_id=flight_id,
        sent_at=now,
        received_at=now,
    )


def envelope(state: DroneState) -> Envelope:
    return Envelope(kind="drone_state", droppable=True, data=state.model_dump(mode="json"))


class Flow:
    """An order that has been dispatched, with the audit envelopes its changes produced."""

    def __init__(self, rig: Rig, order_id: UUID, flight_id: UUID, drone_id: UUID) -> None:
        self.rig, self.order_id, self.flight_id, self.drone_id = rig, order_id, flight_id, drone_id

    async def audit(self, new_state: str) -> Envelope:
        for event in await self.rig.trail(self.order_id):
            if event.new_state.value == new_state:
                return audit_envelope(event)
        raise AssertionError(f"no {new_state} event")


async def started(rig: Rig) -> Flow:
    drone = await rig.add_drone()
    order = await rig.new_order()
    result = await rig.dispatch.dispatch_order(order.id)
    assert result.flight is not None
    return Flow(rig, order.id, result.flight.id, drone.id)


async def fresh_tracker(
    rig: Rig, live: InMemoryRealtime, flow: Flow, *, visible: frozenset[OrderStatus]
) -> OrderTracker:
    order = await rig.order(flow.order_id)
    flights = await rig.flights(flow.order_id)
    live_flight = next((f.id for f in flights if f.status in LIVE_FLIGHT_STATES), None)
    return await OrderTracker.load(rig.orders, order, live_flight, live, visible=visible)


class TestLoading:
    async def test_it_starts_with_the_current_live_flight_and_its_latest_sample(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)
        await live.set_state(sample_for(flow.drone_id, flow.flight_id))

        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)

        assert tracker.flight_id == flow.flight_id
        assert tracker.state is not None
        assert tracker.state.flight_id == flow.flight_id

    async def test_a_sample_for_another_flight_is_not_adopted(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)
        await live.set_state(sample_for(flow.drone_id, uuid4()))  # the drone's previous flight

        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)

        assert tracker.state is None

    async def test_no_live_flight_means_no_position_source(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        order = await rig.new_order()
        tracker = await OrderTracker.load(rig.orders, order, None, live, visible=PLATFORM_VISIBLE)

        assert tracker.state is None
        assert tracker.channels() == [order_channel(order.id)]

    async def test_it_listens_to_the_orders_channel_and_its_drones(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)

        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)

        assert tracker.channels() == [order_channel(flow.order_id), drone_channel(flow.drone_id)]


class TestPositionsAreScopedToThisOrdersFlight:
    async def test_a_matching_sample_is_a_position(self, rig: Rig, live: InMemoryRealtime) -> None:
        flow = await started(rig)
        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)

        change = await tracker.apply(
            drone_channel(flow.drone_id), envelope(sample_for(flow.drone_id, flow.flight_id))
        )

        assert change.position_changed
        assert tracker.state is not None
        assert tracker.position is not None

    @pytest.mark.parametrize("who", ["another flight", "no flight", "another drone"])
    async def test_anything_else_is_ignored(
        self, rig: Rig, live: InMemoryRealtime, who: str
    ) -> None:
        flow = await started(rig)
        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)
        sample = {
            "another flight": sample_for(flow.drone_id, uuid4()),
            "no flight": sample_for(flow.drone_id, None),
            "another drone": sample_for(uuid4(), flow.flight_id),
        }[who]

        change = await tracker.apply("ch:x", envelope(sample))

        assert not change.position_changed
        assert tracker.state is None

    async def test_after_the_flight_ends_a_late_sample_is_not_this_orders_position(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)
        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)
        await tracker.apply("ch:x", envelope(sample_for(flow.drone_id, flow.flight_id)))
        await rig.dispatch.return_to_home(flow.flight_id)  # the flight is aborted
        aborted = await flow.audit("aborted")

        await tracker.apply(order_channel(flow.order_id), aborted)
        late = await tracker.apply("ch:x", envelope(sample_for(flow.drone_id, flow.flight_id)))

        assert tracker.flight_id is None
        assert tracker.state is None
        assert not late.position_changed


class TestVisibility:
    async def test_a_partner_sees_the_drone_only_once_it_carries_the_package(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)  # the order is 'assigned'
        platform = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)
        partner = await fresh_tracker(rig, live, flow, visible=PARTNER_VISIBLE)
        for tracker in (platform, partner):
            await tracker.apply("ch:x", envelope(sample_for(flow.drone_id, flow.flight_id)))

        assert platform.position is not None
        assert partner.position is None  # a sample is held, but not shown yet

        await rig.orders.transition(flow.order_id, S.PICKED_UP, source=EventSource.TELEMETRY)
        await partner.apply(order_channel(flow.order_id), await flow.audit("picked_up"))

        assert partner.position is not None  # and it shows at once

    def test_the_visibility_sets(self) -> None:
        assert S.ASSIGNED in PLATFORM_VISIBLE
        assert S.ASSIGNED not in PARTNER_VISIBLE
        assert {S.PICKED_UP, S.IN_FLIGHT, S.ARRIVING} == PARTNER_VISIBLE
        for finished in (S.DELIVERED, S.CANCELLED, S.FAILED, S.RETURNED, S.CREATED):
            assert finished not in PLATFORM_VISIBLE
            assert finished not in PARTNER_VISIBLE


class TestFollowingTheDrone:
    async def test_learning_the_drone_adopts_its_latest_sample_at_once(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        """A sample published before the stream subscribed is gone: fetch the latest instead."""
        drone = await rig.add_drone()
        order = await rig.new_order()
        tracker = await OrderTracker.load(rig.orders, order, None, live, visible=PLATFORM_VISIBLE)
        result = await rig.dispatch.dispatch_order(order.id)
        assert result.flight is not None
        await live.set_state(sample_for(drone.id, result.flight.id, latitude=12.88))

        for step in ("planned", "permission_granted", "mission_uploaded"):
            await tracker.apply(
                order_channel(order.id),
                await Flow(rig, order.id, result.flight.id, drone.id).audit(step),
            )
        change = await tracker.apply(
            order_channel(order.id),
            await Flow(rig, order.id, result.flight.id, drone.id).audit("assigned"),
        )

        assert change.new_channel == drone_channel(drone.id)
        assert change.position_changed
        assert tracker.state is not None
        assert tracker.state.latitude == 12.88

    async def test_an_unreachable_live_view_does_not_break_the_stream(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()
        tracker = await OrderTracker.load(rig.orders, order, None, live, visible=PLATFORM_VISIBLE)
        result = await rig.dispatch.dispatch_order(order.id)
        assert result.flight is not None
        flow = Flow(rig, order.id, result.flight.id, drone.id)
        for step in ("planned", "permission_granted", "mission_uploaded"):
            await tracker.apply(order_channel(order.id), await flow.audit(step))
        live.broken = True

        change = await tracker.apply(order_channel(order.id), await flow.audit("assigned"))

        assert change.new_channel == drone_channel(drone.id)
        assert not change.position_changed  # the next sample will do


class TestStatusIsAlwaysReadFromTheDatabase:
    async def test_a_stale_event_cannot_make_the_tracker_lag_behind_the_truth(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)
        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)
        await rig.orders.transition(flow.order_id, S.PICKED_UP, source=EventSource.TELEMETRY)

        change = await tracker.apply(order_channel(flow.order_id), await flow.audit("assigned"))

        assert change.status_changed
        assert tracker.order.status is S.PICKED_UP  # not 'assigned', as the event said

    async def test_an_event_about_another_order_is_ignored(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)
        other = await rig.new_order()
        other_event = audit_envelope((await rig.trail(other.id))[0])
        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)

        change = await tracker.apply(order_channel(other.id), other_event)

        assert change.audit is None
        assert not change.status_changed

    async def test_other_kinds_of_message_are_ignored(
        self, rig: Rig, live: InMemoryRealtime
    ) -> None:
        flow = await started(rig)
        tracker = await fresh_tracker(rig, live, flow, visible=PLATFORM_VISIBLE)

        change = await tracker.apply(
            "ch:fleet", Envelope(kind="alert", droppable=False, data={"kind": "link_lost"})
        )

        assert change.audit is None
        assert not change.position_changed
