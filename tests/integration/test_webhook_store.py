"""The webhook delivery store contract, run against BOTH the in-memory and PostgreSQL stores.

The ``memory`` variant always runs; ``postgres`` needs PostgreSQL with PostGIS. Identical
assertions on both keep the in-memory fake honest.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.exceptions import DomainError
from app.domain.models import (
    AttemptOutcome,
    AuditEventType,
    DeliveryAttempt,
    DeliveryStatus,
    EventSource,
    Location,
    Order,
    OrderStatus,
    Package,
    WebhookDelivery,
)
from app.domain.order_state_machine import OrderStateMachine
from app.services.exceptions import ConcurrentUpdateError
from app.services.order_service import OrderService
from app.services.ports import UnitOfWorkFactory

D = DeliveryStatus
LEASE = timedelta(seconds=60)


@pytest.fixture
def service(uow_factory: UnitOfWorkFactory) -> OrderService:
    return OrderService(uow_factory)


async def make_order(
    service: OrderService, external_id: str, *, client_id: str = "mock", cancel: bool = False
) -> Order:
    created = await service.create_order(
        client_id=client_id,
        external_order_id=external_id,
        pickup=Location(latitude=12.9716, longitude=77.5946),
        dropoff=Location(latitude=12.9352, longitude=77.6245),
        package=Package(weight_kg=1.0),
        source=EventSource.API,
    )
    if cancel:
        return await service.transition(
            created.order.id, OrderStatus.CANCELLED, source=EventSource.API
        )
    return created.order


async def everything(uow_factory: UnitOfWorkFactory) -> list[WebhookDelivery]:
    """Every delivery, oldest first."""
    async with uow_factory() as uow:
        return list(reversed(await uow.webhooks.list_recent(limit=1000)))


def soon() -> datetime:
    """A moment after anything the test has created (so every delivery is due)."""
    return datetime.now(UTC) + timedelta(minutes=5)


async def claim(
    uow_factory: UnitOfWorkFactory, *, now: datetime, limit: int = 100
) -> list[WebhookDelivery]:
    async with uow_factory() as uow:
        claimed = await uow.webhooks.claim_due(now=now, lease_until=now + LEASE, limit=limit)
        await uow.commit()
    return claimed


class TestQueueingIsPartOfTheOrdersTransaction:
    async def test_creating_an_order_queues_its_created_event(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1")
        (event,) = await service.get_audit_trail(order.id)

        (delivery,) = await everything(uow_factory)

        assert delivery.event_id == event.event_id
        assert (delivery.order_id, delivery.client_id) == (order.id, "mock")
        assert (delivery.status, delivery.attempts, delivery.round_start) == (D.PENDING, 0, 0)
        assert delivery.next_attempt_at == event.occurred_at
        assert delivery.lease_until is None
        assert delivery.seq is not None

    async def test_every_status_change_queues_one_more_in_order(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1", cancel=True)
        trail = await service.get_audit_trail(order.id)

        deliveries = await everything(uow_factory)

        assert [d.event_id for d in deliveries] == [e.event_id for e in trail]
        assert [e.event_type for e in trail] == [
            AuditEventType.ORDER_CREATED,
            AuditEventType.ORDER_STATUS_CHANGED,
        ]
        seqs = [d.seq for d in deliveries]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == 2

    async def test_a_repeated_order_request_queues_nothing_more(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        await make_order(service, "ORD-1")  # the partner retried

        assert len(await everything(uow_factory)) == 1

    async def test_a_refused_transition_queues_nothing(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1", cancel=True)

        with pytest.raises(DomainError):
            await service.transition(order.id, OrderStatus.IN_FLIGHT, source=EventSource.API)

        assert len(await everything(uow_factory)) == 2

    async def test_a_lost_race_queues_nothing(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1")
        stale = OrderStateMachine().transition(order, OrderStatus.CANCELLED, source=EventSource.API)
        await service.transition(order.id, OrderStatus.CANCELLED, source=EventSource.API)
        before = len(await everything(uow_factory))

        async with uow_factory() as uow:
            with pytest.raises(ConcurrentUpdateError):
                await uow.orders.apply_transition(stale)
            await uow.commit()

        assert len(await everything(uow_factory)) == before == 2

    async def test_nothing_is_queued_if_the_transaction_is_not_committed(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        result = OrderStateMachine().create(
            client_id="mock",
            external_order_id="ORD-X",
            pickup=Location(latitude=12.9716, longitude=77.5946),
            dropoff=Location(latitude=12.9352, longitude=77.6245),
            package=Package(weight_kg=1.0),
            source=EventSource.API,
        )

        async with uow_factory() as uow:
            assert await uow.orders.insert(result)
            assert len(await uow.webhooks.list_recent()) == 1  # visible inside the transaction
            # ... and the block ends without commit()

        assert await everything(uow_factory) == []
        async with uow_factory() as uow:
            assert await uow.audit.get(result.audit_event.event_id) is None

    async def test_the_delivery_and_its_audit_event_exist_together(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1", cancel=True)

        async with uow_factory() as uow:
            for delivery in await uow.webhooks.list_recent():
                assert await uow.audit.get(delivery.event_id) is not None


class TestAdding:
    async def test_an_event_is_queued_at_most_once(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1")
        (event,) = await service.get_audit_trail(order.id)

        async with uow_factory() as uow:
            assert await uow.webhooks.add(event, "mock") is False
            await uow.commit()

        assert len(await everything(uow_factory)) == 1

    async def test_a_delivery_round_trips_exactly(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1")
        (event,) = await service.get_audit_trail(order.id)

        (stored,) = await everything(uow_factory)

        assert replace(stored, seq=None) == WebhookDelivery.queue(event, "mock")

    async def test_the_audit_log_can_fetch_an_event_by_id(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1")
        (event,) = await service.get_audit_trail(order.id)

        async with uow_factory() as uow:
            assert await uow.audit.get(event.event_id) == event
            assert await uow.audit.get(uuid4()) is None


class TestClaiming:
    async def test_the_oldest_due_ones_are_claimed_and_leased(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        orders = [await make_order(service, f"ORD-{n}") for n in range(3)]
        now = soon()

        claimed = await claim(uow_factory, now=now, limit=2)

        assert [d.order_id for d in claimed] == [orders[0].id, orders[1].id]
        assert {d.lease_until for d in claimed} == {now + LEASE}
        stored = await everything(uow_factory)
        assert [d.lease_until for d in stored] == [now + LEASE, now + LEASE, None]

    async def test_one_that_is_not_due_yet_is_left(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        (first,) = await claim(uow_factory, now=now)
        async with uow_factory() as uow:
            later = first.retry(now=now, at=now + timedelta(hours=1), status_code=503, error="x")
            assert await uow.webhooks.apply(first, later, None)
            await uow.commit()

        assert await claim(uow_factory, now=now + timedelta(minutes=59)) == []
        assert len(await claim(uow_factory, now=now + timedelta(hours=1))) == 1

    async def test_a_leased_one_is_left_until_the_lease_ends(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        assert len(await claim(uow_factory, now=now)) == 1

        assert await claim(uow_factory, now=now + timedelta(seconds=59)) == []
        again = await claim(uow_factory, now=now + LEASE)

        assert len(again) == 1
        assert again[0].lease_until == now + LEASE + LEASE

    async def test_finished_ones_are_never_claimed(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        for n in range(3):
            await make_order(service, f"ORD-{n}")
        now = soon()
        delivered, failed, skipped = await claim(uow_factory, now=now)

        async with uow_factory() as uow:
            for before, after in (
                (delivered, delivered.delivered(now=now, status_code=200)),
                (failed, failed.failed(now=now, error="x")),
                (skipped, skipped.skipped(now=now, reason="x")),
            ):
                assert await uow.webhooks.apply(before, after, None)
            await uow.commit()

        assert await claim(uow_factory, now=soon() + timedelta(days=1)) == []

    async def test_an_orders_second_update_waits_for_the_first(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        order = await make_order(service, "ORD-1", cancel=True)
        now = soon()

        (first,) = await claim(uow_factory, now=now)  # only the first of the two
        assert await claim(uow_factory, now=now + timedelta(seconds=30)) == []  # leased
        # Long after, the first's lease has run out and it is claimed again; the second still
        # waits behind it.
        again = await claim(uow_factory, now=now + timedelta(days=1))
        assert [d.event_id for d in again] == [first.event_id]

        async with uow_factory() as uow:
            assert await uow.webhooks.apply(first, first.delivered(now=now, status_code=200), None)
            await uow.commit()
        (second,) = await claim(uow_factory, now=now)

        assert second.order_id == order.id
        assert second.event_id != first.event_id

    async def test_it_waits_while_the_first_is_retrying_too(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1", cancel=True)
        now = soon()
        (first,) = await claim(uow_factory, now=now)
        async with uow_factory() as uow:
            retry = first.retry(now=now, at=now + timedelta(seconds=5), status_code=503, error="x")
            assert await uow.webhooks.apply(first, retry, None)
            await uow.commit()

        assert await claim(uow_factory, now=now + timedelta(minutes=1)) == [
            replace(
                retry,
                lease_until=now + timedelta(minutes=1) + LEASE,
                updated_at=now + timedelta(minutes=1),
            )
        ]  # only the first again; the second is still behind it

    async def test_it_does_not_wait_for_a_first_that_failed_for_good(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1", cancel=True)
        now = soon()
        (first,) = await claim(uow_factory, now=now)
        async with uow_factory() as uow:
            assert await uow.webhooks.apply(first, first.failed(now=now, error="rejected"), None)
            await uow.commit()

        (second,) = await claim(uow_factory, now=now)

        assert second.event_id != first.event_id

    async def test_other_orders_are_not_held_up(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        stuck = await make_order(service, "ORD-1", cancel=True)
        other = await make_order(service, "ORD-2")
        now = soon()
        first_claim = await claim(uow_factory, now=now)  # stuck's first, and other's only
        assert {d.order_id for d in first_claim} == {stuck.id, other.id}
        assert len(first_claim) == 2


class TestRecordingAnOutcome:
    async def test_an_outcome_and_its_attempt_are_stored_together(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        (taken,) = await claim(uow_factory, now=now)
        done = taken.delivered(now=now, status_code=204)
        attempt = DeliveryAttempt(
            taken.event_id, 1, now, 87, AttemptOutcome.DELIVERED, status_code=204
        )

        async with uow_factory() as uow:
            assert await uow.webhooks.apply(taken, done, attempt) is True
            await uow.commit()

        async with uow_factory() as uow:
            stored = await uow.webhooks.get(taken.event_id)
            assert stored is not None
            assert replace(stored, seq=None) == replace(done, seq=None)
            assert await uow.webhooks.attempts_for(taken.event_id) == [attempt]

    async def test_a_stale_outcome_is_refused_and_leaves_no_attempt(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        (taken,) = await claim(uow_factory, now=now)
        winner = taken.delivered(now=now, status_code=200)
        attempt = DeliveryAttempt(
            taken.event_id, 1, now, 10, AttemptOutcome.DELIVERED, status_code=200
        )
        async with uow_factory() as uow:
            assert await uow.webhooks.apply(taken, winner, attempt)
            await uow.commit()

        loser = taken.retry(now=now, at=now + timedelta(seconds=5), status_code=503, error="x")
        late = DeliveryAttempt(taken.event_id, 1, now, 10, AttemptOutcome.RETRY, status_code=503)
        async with uow_factory() as uow:
            assert await uow.webhooks.apply(taken, loser, late) is False
            await uow.commit()

        async with uow_factory() as uow:
            stored = await uow.webhooks.get(taken.event_id)
            assert stored is not None
            assert stored.status is D.DELIVERED
            assert await uow.webhooks.attempts_for(taken.event_id) == [attempt]

    async def test_an_outcome_based_on_an_older_attempt_count_is_refused(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        (taken,) = await claim(uow_factory, now=now)
        first = taken.retry(now=now, at=now, status_code=503, error="x")
        async with uow_factory() as uow:
            assert await uow.webhooks.apply(taken, first, None)  # attempts is now 1, still pending
            await uow.commit()

        async with uow_factory() as uow:  # a sender still holding the old copy (attempts 0)
            assert (
                await uow.webhooks.apply(taken, taken.delivered(now=now, status_code=200), None)
                is False
            )

    async def test_an_outcome_is_refused_once_the_status_has_moved_on_even_if_no_attempt_was_made(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        (taken,) = await claim(uow_factory, now=now)
        async with uow_factory() as uow:  # one sender finds the update cannot be built...
            unsendable = taken.failed(now=now, error="no adapter", attempted=False)
            assert await uow.webhooks.apply(taken, unsendable, None)
            await uow.commit()

        async with uow_factory() as uow:  # ...a stale one, holding the pending copy, skips it
            skipped = taken.skipped(now=now, reason="no endpoint")
            assert await uow.webhooks.apply(taken, skipped, None) is False

        async with uow_factory() as uow:
            stored = await uow.webhooks.get(taken.event_id)
        assert stored is not None
        assert (stored.status, stored.attempts) == (D.FAILED, 0)  # the first decision stands

    async def test_a_requeue_is_applied_once(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        (taken,) = await claim(uow_factory, now=now)
        async with uow_factory() as uow:
            failed = taken.failed(now=now, error="rejected", attempted=False)
            assert await uow.webhooks.apply(taken, failed, None)
            await uow.commit()
        first, second = now + timedelta(minutes=1), now + timedelta(minutes=2)

        async with uow_factory() as uow:  # two operators ask for another round at once
            assert await uow.webhooks.apply(failed, failed.requeued(now=first), None) is True
            await uow.commit()
        async with uow_factory() as uow:
            assert await uow.webhooks.apply(failed, failed.requeued(now=second), None) is False

        async with uow_factory() as uow:
            stored = await uow.webhooks.get(taken.event_id)
        assert stored is not None
        assert (stored.status, stored.next_attempt_at) == (D.PENDING, first)

    async def test_an_unknown_delivery_is_refused(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        (real,) = await everything(uow_factory)
        ghost = replace(real, event_id=uuid4())

        async with uow_factory() as uow:
            assert (
                await uow.webhooks.apply(ghost, ghost.skipped(now=soon(), reason="x"), None)
                is False
            )

    async def test_attempts_come_back_in_order(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        await make_order(service, "ORD-1")
        now = soon()
        (current,) = await claim(uow_factory, now=now)
        for number in (1, 2, 3):
            after = current.retry(now=now, at=now, status_code=503, error="x")
            attempt = DeliveryAttempt(
                current.event_id, number, now, 5, AttemptOutcome.RETRY, status_code=503
            )
            async with uow_factory() as uow:
                assert await uow.webhooks.apply(current, after, attempt)
                await uow.commit()
            current = after

        async with uow_factory() as uow:
            numbers = [a.attempt_no for a in await uow.webhooks.attempts_for(current.event_id)]
        assert numbers == [1, 2, 3]


class TestReading:
    async def test_recent_is_newest_first_and_filterable(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        a = await make_order(service, "A")
        b = await make_order(service, "B", client_id="zomato")
        c = await make_order(service, "C")
        now = soon()
        async with uow_factory() as uow:
            (only_a,) = await uow.webhooks.list_for_order(a.id)
            failed = only_a.failed(now=now, error="x")
            assert await uow.webhooks.apply(only_a, failed, None)
            await uow.commit()

        async with uow_factory() as uow:
            everyone = await uow.webhooks.list_recent()
            assert [d.order_id for d in everyone] == [c.id, b.id, a.id]
            assert [d.order_id for d in await uow.webhooks.list_recent(limit=2)] == [c.id, b.id]
            assert [d.order_id for d in await uow.webhooks.list_recent(status=D.FAILED)] == [a.id]
            assert [d.order_id for d in await uow.webhooks.list_recent(status=D.PENDING)] == [
                c.id,
                b.id,
            ]
            assert [d.order_id for d in await uow.webhooks.list_recent(client_id="zomato")] == [
                b.id
            ]
            assert await uow.webhooks.list_recent(client_id="zomato", status=D.FAILED) == []

    async def test_an_orders_deliveries_are_oldest_first_and_only_its_own(
        self, service: OrderService, uow_factory: UnitOfWorkFactory
    ) -> None:
        mine = await make_order(service, "MINE", cancel=True)
        await make_order(service, "OTHER")

        async with uow_factory() as uow:
            deliveries = await uow.webhooks.list_for_order(mine.id)

        trail = await service.get_audit_trail(mine.id)
        assert [d.event_id for d in deliveries] == [e.event_id for e in trail]

    async def test_unknown_things_are_simply_absent(self, uow_factory: UnitOfWorkFactory) -> None:
        async with uow_factory() as uow:
            assert await uow.webhooks.get(UUID(int=1)) is None
            assert await uow.webhooks.list_for_order(UUID(int=1)) == []
            assert await uow.webhooks.attempts_for(UUID(int=1)) == []
            assert await uow.webhooks.list_recent() == []
