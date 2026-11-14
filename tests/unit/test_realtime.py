"""The live view's models, its in-memory implementation, and the audit notifier."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.domain.models import AuditEvent, AuditEventType, EventSource, FlightStatus, OrderStatus
from app.protocol.messages import DronePhase, Telemetry
from app.realtime import channels
from app.realtime.memory import InMemoryRealtime
from app.realtime.models import Alert, AlertKind, DroneState, Envelope, alert_key
from app.realtime.notifier import AuditBroadcaster, audit_envelope
from tests.conftest import DRONE_ID, FLIGHT_ID, ORDER_ID, T0, FakeClock


def state(drone_id: UUID = DRONE_ID, **overrides: Any) -> DroneState:
    fields: dict[str, Any] = {
        "drone_id": drone_id,
        "latitude": 12.97,
        "longitude": 77.59,
        "altitude_m": 60.0,
        "battery_percent": 80.0,
        "phase": DronePhase.FLYING,
        "flight_id": FLIGHT_ID,
        "sent_at": T0,
        "received_at": T0,
    }
    fields.update(overrides)
    return DroneState(**fields)


def alert(kind: AlertKind = AlertKind.LINK_LOST, drone_id: UUID | None = DRONE_ID) -> Alert:
    return Alert(kind=kind, drone_id=drone_id, message="x", raised_at=T0)


class TestModels:
    def test_a_state_is_built_from_a_sample_and_the_receive_time(self) -> None:
        sample = Telemetry(
            sent_at=T0,
            latitude=1.0,
            longitude=2.0,
            altitude_m=3.0,
            battery_percent=4.0,
            phase=DronePhase.PAUSED,
            flight_id=FLIGHT_ID,
        )

        built = DroneState.from_telemetry(DRONE_ID, sample, received_at=T0 + timedelta(seconds=2))

        assert (built.latitude, built.longitude, built.altitude_m) == (1.0, 2.0, 3.0)
        assert built.battery_percent == 4.0
        assert built.phase is DronePhase.PAUSED
        assert built.flight_id == FLIGHT_ID
        assert built.sent_at == T0
        assert built.received_at == T0 + timedelta(seconds=2)

    def test_a_state_round_trips_through_json(self) -> None:
        assert DroneState.model_validate_json(state().model_dump_json()) == state()

    def test_naive_times_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            state(received_at=T0.replace(tzinfo=None))

    def test_alert_keys_distinguish_kind_and_drone(self) -> None:
        assert alert().key == f"link_lost:{DRONE_ID}"
        assert alert(drone_id=None).key == "link_lost:-"
        assert alert_key(AlertKind.LOW_BATTERY, DRONE_ID) != alert().key

    def test_resolving_stamps_the_time_without_changing_the_rest(self) -> None:
        resolved = alert().resolved(T0 + timedelta(seconds=9))

        assert resolved.resolved_at == T0 + timedelta(seconds=9)
        assert resolved.kind is AlertKind.LINK_LOST
        assert alert().resolved_at is None

    def test_an_envelope_round_trips(self) -> None:
        envelope = Envelope(kind="drone_state", droppable=True, data={"a": 1})

        assert Envelope.decode(envelope.encode()) == envelope

    @pytest.mark.parametrize(
        "raw", ["", "not json", "{}", '{"kind": "weird", "droppable": true, "data": {}}']
    )
    def test_a_bad_envelope_is_refused(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            Envelope.decode(raw)

    def test_channel_names(self) -> None:
        assert channels.drone_channel(DRONE_ID) == f"ch:drone:{DRONE_ID}"
        assert channels.order_channel(ORDER_ID) == f"ch:order:{ORDER_ID}"
        assert channels.FLEET == "ch:fleet"
        assert channels.state_key(DRONE_ID) == f"state:{DRONE_ID}"


class TestInMemoryState:
    async def test_the_latest_sample_wins_and_expires_when_the_drone_goes_silent(
        self, clock: FakeClock
    ) -> None:
        live = InMemoryRealtime(clock=clock, state_ttl=timedelta(seconds=60))
        await live.set_state(state(battery_percent=90.0, received_at=clock()))
        await live.set_state(state(battery_percent=70.0, received_at=clock()))

        current = await live.get_state(DRONE_ID)
        assert current is not None
        assert current.battery_percent == 70.0
        assert set(await live.all_states()) == {DRONE_ID}

        clock.advance(seconds=61)
        assert await live.get_state(DRONE_ID) is None
        assert await live.all_states() == {}

    async def test_it_can_be_made_to_fail_like_an_unreachable_redis(self) -> None:
        live = InMemoryRealtime()
        live.broken = True

        with pytest.raises(ConnectionError):
            await live.set_state(state())
        with pytest.raises(ConnectionError):
            await live.active()
        await live.publish(
            channels.FLEET, Envelope(kind="alert", droppable=False, data={})
        )  # no raise
        assert live.published == []


class TestAuditBroadcaster:
    def event(self, **overrides: Any) -> AuditEvent:
        fields: dict[str, Any] = {
            "event_id": UUID(int=0xE1),
            "order_id": ORDER_ID,
            "event_type": AuditEventType.FLIGHT_STATUS_CHANGED,
            "previous_state": FlightStatus.PERMISSION_GRANTED,
            "new_state": FlightStatus.MISSION_UPLOADED,
            "occurred_at": T0,
            "source": EventSource.DISPATCH,
            "drone_id": DRONE_ID,
            "flight_id": FLIGHT_ID,
            "metadata": {"secret": "not for the stream"},
        }
        fields.update(overrides)
        return AuditEvent(**fields)

    def test_the_envelope_is_a_small_summary_without_metadata(self) -> None:
        envelope = audit_envelope(self.event())

        assert envelope.kind == "audit"
        assert envelope.droppable is False
        assert envelope.data == {
            "event_type": "flight_status_changed",
            "order_id": str(ORDER_ID),
            "flight_id": str(FLIGHT_ID),
            "drone_id": str(DRONE_ID),
            "previous_state": "permission_granted",
            "new_state": "mission_uploaded",
            "occurred_at": T0.isoformat(),
            "source": "dispatch",
        }

    async def test_a_change_goes_to_the_orders_channel_and_the_fleet_channel(self) -> None:
        live = InMemoryRealtime()

        await AuditBroadcaster(live).notify([self.event()])

        assert [channel for channel, _ in live.published] == [
            channels.order_channel(ORDER_ID),
            channels.FLEET,
        ]
        assert {e.droppable for _, e in live.published} == {False}

    async def test_an_order_event_has_no_flight_or_previous_state(self) -> None:
        created = self.event(
            event_type=AuditEventType.ORDER_CREATED,
            previous_state=None,
            new_state=OrderStatus.CREATED,
            flight_id=None,
            drone_id=None,
        )

        data = audit_envelope(created).data

        assert data["flight_id"] is None
        assert data["previous_state"] is None
        assert data["new_state"] == "created"

    async def test_a_failing_broadcast_never_raises(self) -> None:
        class Exploding:
            async def publish(self, channel: str, envelope: Envelope) -> None:
                raise RuntimeError("redis exploded")

        await AuditBroadcaster(Exploding()).notify([self.event(), self.event()])  # must not raise
