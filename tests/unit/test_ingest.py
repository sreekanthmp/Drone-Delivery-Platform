"""The telemetry ingest: decoding, de-duplication, retries, and what it can never do."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
import structlog

from app.dispatch.progress import ProgressEvent, ProgressOutcome, ProgressResult
from app.domain.models import FlightStatus, OrderStatus
from app.protocol.messages import DroneEvent, EventType
from app.telemetry.ingest import IngestOutcome, TelemetryIngest
from tests.conftest import T0, FakeClock
from tests.dispatch_rig import Rig, build_rig
from tests.fakes import InMemoryDatabase, InMemoryUnitOfWork

I = IngestOutcome  # noqa: E741


@pytest.fixture
def rig(clock: FakeClock) -> Rig:
    database = InMemoryDatabase()
    return build_rig(lambda: InMemoryUnitOfWork(database), clock=clock)


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def payload(flight_id: UUID, seq: int, type_: str = "package_picked_up", **extra: Any) -> bytes:
    return json.dumps(
        {
            "event_id": str(UUID(int=0xE000 + seq)),
            "flight_id": str(flight_id),
            "seq": seq,
            "type": type_,
            "occurred_at": T0.isoformat(),
            **extra,
        }
    ).encode()


async def started(rig: Rig) -> tuple[UUID, UUID, UUID]:
    """``(order_id, flight_id, drone_id)`` of a flying flight."""
    drone = await rig.add_drone()
    order = await rig.new_order()
    result = await rig.dispatch.dispatch_order(order.id)
    assert result.flight is not None
    return order.id, result.flight.id, drone.id


JOURNEY = ["package_picked_up", "departed_pickup", "arriving_at_dropoff", "package_delivered"]


class TestTheJourney:
    async def test_events_drive_an_order_to_delivered_and_free_the_drone(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        order_id, flight_id, drone_id = await started(rig)

        results = [
            await ingest.handle_event(drone_id, payload(flight_id, seq, kind))
            for seq, kind in enumerate([*JOURNEY, "mission_completed"], start=1)
        ]

        assert [r.outcome for r in results] == [I.APPLIED] * 5
        assert (await rig.order(order_id)).status is OrderStatus.DELIVERED
        assert (await rig.dispatch.get_flight(flight_id)).status is FlightStatus.COMPLETED
        assert (await rig.drone(drone_id)).status.value == "available"

    async def test_the_ingest_never_sends_a_command(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        _, flight_id, drone_id = await started(rig)
        sent = list(rig.link.commands)

        for seq, kind in enumerate(JOURNEY, start=1):
            await ingest.handle_event(drone_id, payload(flight_id, seq, kind))

        assert rig.link.commands == sent


class TestRepeats:
    async def test_a_repeat_is_dropped_before_the_service_is_called(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        _, flight_id, drone_id = await started(rig)
        calls: list[ProgressEvent] = []
        original = rig.dispatch.record_progress

        async def spy(*args: Any, **kwargs: Any) -> ProgressResult:
            calls.append(args[2])
            return await original(*args, **kwargs)

        rig.dispatch.record_progress = spy  # type: ignore[method-assign]
        first = await ingest.handle_event(drone_id, payload(flight_id, 1))
        again = await ingest.handle_event(drone_id, payload(flight_id, 1))

        assert (first.outcome, again.outcome) == (I.APPLIED, I.DUPLICATE)
        assert calls == [ProgressEvent.PACKAGE_PICKED_UP]

    async def test_an_older_sequence_number_is_dropped(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        _, flight_id, drone_id = await started(rig)
        await ingest.handle_event(drone_id, payload(flight_id, 1))
        await ingest.handle_event(drone_id, payload(flight_id, 2, "departed_pickup"))

        late = await ingest.handle_event(drone_id, payload(flight_id, 1))

        assert late.outcome is I.DUPLICATE

    async def test_a_gap_is_logged_but_the_event_is_still_considered(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        _, flight_id, drone_id = await started(rig)
        await ingest.handle_event(drone_id, payload(flight_id, 1))

        with structlog.testing.capture_logs() as logs:
            result = await ingest.handle_event(
                drone_id, payload(flight_id, 4, "arriving_at_dropoff")
            )

        assert any(entry["event"] == "event_gap" for entry in logs)
        # event 2 and 3 were lost: the service refuses to invent them
        assert result.outcome is I.REJECTED

    async def test_sequences_are_per_flight(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        _, flight_id, drone_id = await started(rig)
        await ingest.handle_event(drone_id, payload(flight_id, 1))

        other = await ingest.handle_event(drone_id, payload(UUID(int=0xBEEF), 1))

        assert other.outcome is I.REJECTED  # not a duplicate: a different (unknown) flight

    async def test_a_restart_forgets_sequences_but_the_service_still_absorbs_repeats(
        self, rig: Rig
    ) -> None:
        order_id, flight_id, drone_id = await started(rig)
        await TelemetryIngest(rig.dispatch).handle_event(drone_id, payload(flight_id, 1))
        events = len(await rig.trail(order_id))

        after_restart = await TelemetryIngest(rig.dispatch).handle_event(
            drone_id, payload(flight_id, 1)
        )

        assert after_restart.outcome is I.ALREADY_APPLIED
        assert len(await rig.trail(order_id)) == events

    async def test_the_sequence_memory_is_bounded(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch, max_tracked_flights=2)
        _, flight_id, drone_id = await started(rig)
        await ingest.handle_event(drone_id, payload(flight_id, 1))
        for n in range(3):  # three other flights push the first one out
            await ingest.handle_event(drone_id, payload(UUID(int=0xA000 + n), 1))

        again = await ingest.handle_event(drone_id, payload(flight_id, 1))

        assert again.outcome is I.ALREADY_APPLIED  # forgotten here, absorbed by the service


class TestBadInput:
    @pytest.mark.parametrize(
        "raw",
        [b"", b"\xff\xfe", b"null", b"[]", b"{}", b'{"seq": 1}', b"x" * 10_000],
    )
    async def test_garbage_is_invalid_and_never_raises(self, rig: Rig, raw: bytes) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        _, _, drone_id = await started(rig)

        result = await ingest.handle_event(drone_id, raw)

        assert result.outcome is I.INVALID

    async def test_an_unknown_event_type_is_invalid(self, rig: Rig) -> None:
        _, flight_id, drone_id = await started(rig)

        result = await TelemetryIngest(rig.dispatch).handle_event(
            drone_id, payload(flight_id, 1, "teleported")
        )

        assert result.outcome is I.INVALID

    async def test_an_event_for_an_unknown_flight_is_rejected(self, rig: Rig) -> None:
        _, _, drone_id = await started(rig)

        result = await TelemetryIngest(rig.dispatch).handle_event(
            drone_id, payload(UUID(int=404), 1)
        )

        assert result.outcome is I.REJECTED
        assert result.reason == "unknown flight"

    async def test_an_event_from_the_wrong_drone_is_rejected(self, rig: Rig) -> None:
        _, flight_id, _ = await started(rig)
        intruder = await rig.add_drone("intruder")

        result = await TelemetryIngest(rig.dispatch).handle_event(
            intruder.id, payload(flight_id, 1)
        )

        assert result.outcome is I.REJECTED

    async def test_a_fault_is_noted_and_its_detail_is_bounded(self, rig: Rig) -> None:
        _, flight_id, drone_id = await started(rig)
        huge = {"blob": "x" * 5000}

        with structlog.testing.capture_logs() as logs:
            result = await TelemetryIngest(rig.dispatch).handle_event(
                drone_id, payload(flight_id, 1, "fault", detail=huge)
            )

        assert result.outcome is I.NOTED
        fault = next(entry for entry in logs if entry["event"] == "drone_fault")
        assert fault["detail"] == {"truncated": True}


class TestTransientFailures:
    async def test_a_brief_failure_is_retried_with_back_off(self, rig: Rig) -> None:
        sleeps = Sleeps()
        ingest = TelemetryIngest(rig.dispatch, retry_delays=(0.1, 0.2, 0.4), sleep=sleeps)
        order_id, flight_id, drone_id = await started(rig)
        original = rig.dispatch.record_progress
        attempts = {"n": 0}

        async def flaky(*args: Any, **kwargs: Any) -> ProgressResult:
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise ConnectionError("database restarting")
            return await original(*args, **kwargs)

        rig.dispatch.record_progress = flaky  # type: ignore[method-assign]

        result = await ingest.handle_event(drone_id, payload(flight_id, 1))

        assert result.outcome is I.APPLIED
        assert sleeps.delays == [0.1, 0.2]
        assert (await rig.order(order_id)).status is OrderStatus.PICKED_UP

    async def test_a_lasting_failure_loses_the_event_but_not_the_sequence(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch, retry_delays=(0.0, 0.0), sleep=Sleeps())
        order_id, flight_id, drone_id = await started(rig)
        original = rig.dispatch.record_progress

        async def down(*args: Any, **kwargs: Any) -> ProgressResult:
            raise ConnectionError("database is down")

        rig.dispatch.record_progress = down  # type: ignore[method-assign]
        lost = await ingest.handle_event(drone_id, payload(flight_id, 1))
        rig.dispatch.record_progress = original  # type: ignore[method-assign]

        assert lost.outcome is I.FAILED
        # the sequence was not advanced, so a redelivery of the same event is applied normally
        again = await ingest.handle_event(drone_id, payload(flight_id, 1))
        assert again.outcome is I.APPLIED
        assert (await rig.order(order_id)).status is OrderStatus.PICKED_UP

    async def test_it_never_raises(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch, retry_delays=(), sleep=Sleeps())
        _, flight_id, drone_id = await started(rig)

        async def boom(*args: Any, **kwargs: Any) -> ProgressResult:
            raise RuntimeError("unexpected")

        rig.dispatch.record_progress = boom  # type: ignore[method-assign]

        result = await ingest.handle_event(drone_id, payload(flight_id, 1))

        assert result.outcome is I.FAILED


class TestBookkeeping:
    async def test_outcomes_are_counted(self, rig: Rig) -> None:
        ingest = TelemetryIngest(rig.dispatch)
        _, flight_id, drone_id = await started(rig)

        await ingest.handle_event(drone_id, payload(flight_id, 1))
        await ingest.handle_event(drone_id, payload(flight_id, 1))
        await ingest.handle_event(drone_id, b"garbage")

        assert dict(ingest.counts) == {I.APPLIED: 1, I.DUPLICATE: 1, I.INVALID: 1}


class TestTheVocabulariesAgree:
    def test_every_wire_event_type_is_a_progress_event(self) -> None:
        assert {t.value for t in EventType} == {e.value for e in ProgressEvent}

    def test_every_progress_outcome_is_an_ingest_outcome(self) -> None:
        assert {o.value for o in ProgressOutcome} <= {o.value for o in IngestOutcome}

    def test_the_event_model_is_the_one_the_protocol_defines(self) -> None:
        assert DroneEvent.model_fields.keys() >= {"event_id", "flight_id", "seq", "type"}
