"""DispatchService against BOTH unit-of-work implementations, plus real concurrency.

The in-memory fake and PostgreSQL must agree on every outcome the service can produce. The
Postgres runs also prove the database guards accept everything the service does (the
deferred lifecycle triggers, the DB-side start gate, the one-live-flight indexes) with a
real clock, since the database judges permission validity against its own clock.

The concurrency tests need real transactions and locks, so they run on PostgreSQL only.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from app.dispatch.exceptions import (
    CommandNotConfirmedError,
    DispatchError,
    FlightInProgressError,
    OrderNotDispatchableError,
    StartCommandFailedError,
)
from app.dispatch.link import CommandRejectedError, NoAcknowledgementError
from app.dispatch.service import DispatchOutcome, DispatchResult
from app.domain.exceptions import PermissionRevokedError
from app.domain.models import DroneStatus, EventSource, FlightStatus, OrderStatus
from app.services.ports import UnitOfWorkFactory
from tests.dispatch_rig import Rig, assert_rule2, build_rig

O = DispatchOutcome  # noqa: E741
S = FlightStatus
D = DroneStatus


@pytest.fixture
def rig(uow_factory: UnitOfWorkFactory) -> Rig:
    return build_rig(uow_factory)


async def dispatch_one(rig: Rig, **kwargs: Any) -> tuple[DispatchResult, UUID]:
    drone = await rig.add_drone()
    order = await rig.new_order()
    return await rig.dispatch.dispatch_order(order.id, **kwargs), drone.id


class TestOutcomesAgreeOnBothBackends:
    async def test_the_happy_path(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.STARTED
        assert result.order.status is OrderStatus.ASSIGNED
        assert result.order.drone_id == drone_id
        assert result.flight is not None
        assert result.flight.status is S.STARTED
        assert result.drone is not None
        assert result.drone.status is D.ON_MISSION
        assert rig.commands() == ["upload", "start"]
        assert await rig.steps(result.order.id) == [
            ("order created", "created"),
            ("flight created", "planned"),
            ("flight", "permission_granted"),
            ("flight", "mission_uploaded"),
            ("order", "assigned"),
            ("flight", "started"),
        ]
        await assert_rule2(rig)

    async def test_the_stored_flight_is_the_one_returned(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        assert result.flight is not None
        assert await rig.dispatch.get_flight(result.flight.id) == result.flight
        assert await rig.flights(result.order.id) == [result.flight]

    async def test_staged_then_started(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig, start=False)
        assert result.outcome is O.READY
        assert result.flight is not None

        started = await rig.dispatch.start_flight(result.flight.id)

        assert started.status is S.STARTED
        assert (await rig.drone(drone_id)).status is D.ON_MISSION
        await assert_rule2(rig)

    async def test_no_drone(self, rig: Rig) -> None:
        order = await rig.new_order()

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.NO_DRONE_AVAILABLE
        assert result.order.status is OrderStatus.CREATED
        assert await rig.flights(order.id) == []

    async def test_a_denial_is_stored_and_the_drone_released(self, rig: Rig) -> None:
        rig.compliance.close_airspace("closed")
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.PERMISSION_DENIED
        assert result.flight is not None
        assert result.flight.denial is not None
        assert result.flight.denial.reason == "closed"
        assert (await rig.drone(drone_id)).status is D.AVAILABLE
        assert rig.commands() == []

    async def test_a_revocation_before_the_gate_ends_the_flight_and_fails_the_order(
        self, rig: Rig
    ) -> None:
        rig.compliance.on_validate = lambda n, pid: rig.compliance.revoke(pid) if n == 2 else None
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.order.status is OrderStatus.FAILED
        assert result.flight is not None
        assert result.flight.status is S.CANCELLED
        assert (await rig.drone(drone_id)).status is D.AVAILABLE
        assert rig.commands() == ["upload"]
        await assert_rule2(rig)

    async def test_a_revoked_staged_flight_is_refused_and_nothing_changes(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig, start=False)
        assert result.flight is not None
        rig.compliance.revoke(rig.compliance.issued[0].permission_id)

        with pytest.raises(PermissionRevokedError):
            await rig.dispatch.start_flight(result.flight.id)

        assert (await rig.dispatch.get_flight(result.flight.id)).status is S.MISSION_UPLOADED
        assert rig.commands() == ["upload"]

    @pytest.mark.parametrize(
        "error", [CommandRejectedError("no"), NoAcknowledgementError("no")], ids=["rej", "none"]
    )
    async def test_a_failed_upload_releases_the_drone(self, rig: Rig, error: Any) -> None:
        rig.link.set_failure("upload", error)
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.UPLOAD_FAILED
        assert result.flight is not None
        assert result.flight.status is S.UPLOAD_FAILED
        assert (await rig.drone(drone_id)).status is D.AVAILABLE
        assert result.order.status is OrderStatus.CREATED
        await assert_rule2(rig)

    async def test_a_refused_start_command(self, rig: Rig) -> None:
        rig.link.set_failure("start", CommandRejectedError("no"))
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert result.order.status is OrderStatus.FAILED
        assert result.flight is not None
        assert result.flight.status is S.ABORTED
        assert (await rig.drone(drone_id)).status is D.AVAILABLE
        await assert_rule2(rig)

    async def test_an_unanswered_start_command_recalls_the_drone(self, rig: Rig) -> None:
        rig.link.set_failure("start", NoAcknowledgementError("lost"), executed=True)
        result, drone_id = await dispatch_one(rig)

        assert result.outcome is O.START_FAILED
        assert (await rig.drone(drone_id)).status is D.RETURNING
        assert rig.commands() == ["upload", "start", "return_to_home"]
        await assert_rule2(rig)

    async def test_redispatch_after_a_failure_keeps_the_history(self, rig: Rig) -> None:
        rig.link.set_failure("upload", NoAcknowledgementError("down"))
        result, _ = await dispatch_one(rig)
        rig.link.set_failure("upload", None)

        again = await rig.dispatch.dispatch_order(result.order.id)

        assert again.outcome is O.STARTED
        statuses = [f.status for f in await rig.flights(result.order.id)]
        assert statuses == [S.UPLOAD_FAILED, S.STARTED]

    async def test_not_dispatchable_twice(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)

        with pytest.raises(OrderNotDispatchableError):
            await rig.dispatch.dispatch_order(result.order.id)


class TestCommandsAgreeOnBothBackends:
    async def test_pause_resume_and_return_to_home(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)
        assert result.flight is not None
        flight_id = result.flight.id

        assert (await rig.dispatch.pause_flight(flight_id)).status is S.PAUSED
        assert (await rig.dispatch.resume_flight(flight_id)).status is S.STARTED
        aborted = await rig.dispatch.return_to_home(flight_id, reason="storm")

        assert aborted.status is S.ABORTED
        assert aborted.failure_reason is not None
        assert "storm" in aborted.failure_reason
        assert (await rig.drone(drone_id)).status is D.RETURNING
        assert rig.commands() == ["upload", "start", "pause", "start", "return_to_home"]
        await assert_rule2(rig)

    async def test_land_grounds_the_drone(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)
        assert result.flight is not None

        flight = await rig.dispatch.land(result.flight.id)

        assert flight.status is S.ABORTED
        assert (await rig.drone(drone_id)).status is D.OFFLINE

    async def test_an_unconfirmed_recall_changes_nothing(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)
        assert result.flight is not None
        rig.link.set_failure("return_to_home", NoAcknowledgementError("no answer"))

        with pytest.raises(CommandNotConfirmedError):
            await rig.dispatch.return_to_home(result.flight.id)

        assert (await rig.dispatch.get_flight(result.flight.id)).status is S.STARTED
        assert (await rig.drone(drone_id)).status is D.ON_MISSION

    async def test_a_refused_resume_leaves_the_flight_paused(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)
        assert result.flight is not None
        await rig.dispatch.pause_flight(result.flight.id)
        rig.link.set_failure("start", CommandRejectedError("not ready"))

        with pytest.raises(StartCommandFailedError) as excinfo:
            await rig.dispatch.resume_flight(result.flight.id)

        assert excinfo.value.flight_aborted is False
        assert (await rig.dispatch.get_flight(result.flight.id)).status is S.PAUSED
        assert (await rig.drone(drone_id)).status is D.ON_MISSION

    async def test_safety_commands_are_not_gated_by_a_revocation(self, rig: Rig) -> None:
        result, _ = await dispatch_one(rig)
        assert result.flight is not None
        rig.compliance.revoke(rig.compliance.issued[0].permission_id)

        flight = await rig.dispatch.return_to_home(result.flight.id)

        assert flight.status is S.ABORTED


class TestCancellationAgreesOnBothBackends:
    async def test_cancelling_a_flying_order_recalls_the_drone_first(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig)

        cancelled = await rig.dispatch.cancel_order(result.order.id, source=EventSource.API)

        assert cancelled.order.status is OrderStatus.CANCELLED
        assert cancelled.flight is not None
        assert cancelled.flight.status is S.ABORTED
        assert (await rig.drone(drone_id)).status is D.RETURNING
        assert rig.commands() == ["upload", "start", "return_to_home"]
        assert (await rig.steps(result.order.id))[-2:] == [
            ("flight", "aborted"),
            ("order", "cancelled"),
        ]

    async def test_an_unconfirmed_recall_keeps_the_order_alive_and_a_retry_works(
        self, rig: Rig
    ) -> None:
        result, drone_id = await dispatch_one(rig)
        rig.link.set_failure("return_to_home", NoAcknowledgementError("no answer"))

        with pytest.raises(CommandNotConfirmedError):
            await rig.dispatch.cancel_order(result.order.id, source=EventSource.API)
        assert (await rig.order(result.order.id)).status is OrderStatus.ASSIGNED
        assert (await rig.drone(drone_id)).status is D.ON_MISSION

        rig.link.set_failure("return_to_home", None)
        cancelled = await rig.dispatch.cancel_order(result.order.id, source=EventSource.API)

        assert cancelled.order.status is OrderStatus.CANCELLED

    async def test_a_staged_order_is_cancelled_and_the_drone_released(self, rig: Rig) -> None:
        result, drone_id = await dispatch_one(rig, start=False)

        cancelled = await rig.dispatch.cancel_order(result.order.id, source=EventSource.API)

        assert cancelled.order.status is OrderStatus.CANCELLED
        assert cancelled.flight is not None
        assert cancelled.flight.status is S.CANCELLED
        assert (await rig.drone(drone_id)).status is D.AVAILABLE

    async def test_cancelling_during_the_upload_wins(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()

        async def cancel() -> None:
            await rig.dispatch.cancel_order(order.id, source=EventSource.API)

        rig.link.on_upload = cancel

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.CANCELLED_DURING_DISPATCH
        assert result.order.status is OrderStatus.CANCELLED
        assert (await rig.drone(drone.id)).status is D.AVAILABLE
        assert "start" not in rig.commands()

    async def test_cancelling_just_before_the_order_is_assigned_wins(self, rig: Rig) -> None:
        drone = await rig.add_drone()
        order = await rig.new_order()

        async def cancel() -> None:
            await rig.dispatch.cancel_order(order.id, source=EventSource.API)

        rig.orders.before_assign = cancel

        result = await rig.dispatch.dispatch_order(order.id)

        assert result.outcome is O.CANCELLED_DURING_DISPATCH
        assert (await rig.drone(drone.id)).status is D.AVAILABLE
        assert "start" not in rig.commands()


class TestFleetAgreesOnBothBackends:
    async def test_register_list_maintenance_docked(self, rig: Rig) -> None:
        a = await rig.add_drone("a")
        b = await rig.add_drone("b")

        assert (await rig.dispatch.set_maintenance(b.id)).status is D.MAINTENANCE
        assert [d.id for d in await rig.dispatch.list_drones(status=D.AVAILABLE)] == [a.id]
        assert (await rig.dispatch.confirm_docked(b.id)).status is D.AVAILABLE

    async def test_a_drone_with_a_live_flight_is_busy(self, rig: Rig) -> None:
        from app.dispatch.exceptions import DroneBusyError

        _, drone_id = await dispatch_one(rig)

        with pytest.raises(DroneBusyError):
            await rig.dispatch.confirm_docked(drone_id)


# =============================================================================================
# Real concurrency (PostgreSQL only)
# =============================================================================================


@pytest.mark.integration
class TestConcurrency:
    @pytest.fixture
    def pg_rig(self, pg_uow_factory: UnitOfWorkFactory) -> Rig:
        return build_rig(pg_uow_factory)

    async def test_ten_concurrent_dispatches_of_one_order_make_one_flight(
        self, pg_rig: Rig
    ) -> None:
        rig = pg_rig
        for _ in range(3):
            await rig.add_drone()
        order = await rig.new_order()

        outcomes = await asyncio.gather(
            *(rig.dispatch.dispatch_order(order.id) for _ in range(10)), return_exceptions=True
        )

        results = [o for o in outcomes if isinstance(o, DispatchResult)]
        errors = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(results) == 1
        assert results[0].outcome is O.STARTED
        assert len(errors) == 9
        assert all(
            isinstance(e, FlightInProgressError | OrderNotDispatchableError) for e in errors
        ), errors
        assert len(await rig.flights(order.id)) == 1
        assert rig.commands() == ["upload", "start"]  # one drone was told, once
        await assert_rule2(rig)

    async def test_many_orders_never_double_book_a_drone(self, pg_rig: Rig) -> None:
        rig = pg_rig
        drones = [await rig.add_drone() for _ in range(3)]
        orders = [await rig.new_order() for _ in range(12)]

        results = await asyncio.gather(*(rig.dispatch.dispatch_order(o.id) for o in orders))

        started = [r for r in results if r.outcome is O.STARTED]
        refused = [r for r in results if r.outcome is O.NO_DRONE_AVAILABLE]
        assert len(started) == 3
        assert len(refused) == 9
        assert {r.drone.id for r in started if r.drone} == {d.id for d in drones}
        assert sorted(rig.link.operations("start")) == sorted(d.id for d in drones)
        for drone in drones:
            assert (await rig.drone(drone.id)).status is D.ON_MISSION
        for refusal in refused:
            assert refusal.order.status is OrderStatus.CREATED
            assert refusal.flight is None
        await assert_rule2(rig)

    async def test_staged_dispatches_reserve_distinct_drones(self, pg_rig: Rig) -> None:
        rig = pg_rig
        drones = [await rig.add_drone() for _ in range(4)]
        orders = [await rig.new_order() for _ in range(10)]

        results = await asyncio.gather(
            *(rig.dispatch.dispatch_order(o.id, start=False) for o in orders)
        )

        ready = [r for r in results if r.outcome is O.READY]
        assert len(ready) == 4
        assert len({r.drone.id for r in ready if r.drone}) == 4
        assert {d.id for d in drones} == {r.drone.id for r in ready if r.drone}
        assert rig.commands().count("start") == 0

    @pytest.mark.parametrize("round_", range(6))
    async def test_cancelling_while_dispatching_always_ends_consistently(
        self, pg_rig: Rig, round_: int
    ) -> None:
        rig = pg_rig
        drone = await rig.add_drone()
        order = await rig.new_order()

        dispatched, cancelled = await asyncio.gather(
            rig.dispatch.dispatch_order(order.id),
            rig.dispatch.cancel_order(order.id, source=EventSource.API),
            return_exceptions=True,
        )

        # cancellation always wins eventually; dispatch either finished first or was beaten
        assert not isinstance(cancelled, BaseException), cancelled
        assert isinstance(dispatched, DispatchResult | OrderNotDispatchableError), dispatched
        assert (await rig.order(order.id)).status is OrderStatus.CANCELLED
        assert all(not f.status.is_live for f in await rig.flights(order.id))
        assert (await rig.drone(drone.id)).status in {D.AVAILABLE, D.RETURNING}
        commands = rig.commands()
        if "start" in commands:
            # a drone that was started must have been recalled afterwards
            assert commands[-1] == "return_to_home", commands
        await assert_rule2(rig)

    async def test_concurrent_registration_of_one_name_registers_once(self, pg_rig: Rig) -> None:
        rig = pg_rig

        outcomes = await asyncio.gather(
            *(rig.add_drone("same-name") for _ in range(5)), return_exceptions=True
        )

        assert sum(1 for o in outcomes if not isinstance(o, BaseException)) == 1
        assert all(isinstance(o, DispatchError) for o in outcomes if isinstance(o, BaseException))
        assert len(await rig.dispatch.list_drones()) == 1
