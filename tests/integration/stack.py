"""A whole platform for integration tests: API, worker and simulated drones on real infrastructure.

Shared by the MQTT and the live-view end-to-end tests. Nothing here is faked: the app runs its
lifespan against PostgreSQL, the worker and the simulators talk to a real Mosquitto, and (when
asked) the live view is a real Redis.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from app.mqtt.client import MqttClient
from app.protocol import messages as wire
from app.realtime.ports import LiveView
from app.services.ports import UnitOfWorkFactory
from app.simulator.drone import SimulatedDrone
from app.simulator.runner import SimulatorRunner
from app.worker import Worker, WorkerConfig
from tests.api.conftest import register_drone
from tests.conftest import HOME, T0
from tests.integration.conftest import RunSql


async def eventually(check: Callable[[], Awaitable[bool]], *, timeout: float = 15.0) -> None:
    """Poll until ``check`` is true, failing with a clear message if it never is."""
    try:
        async with asyncio.timeout(timeout):
            while not await check():
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise AssertionError(f"the condition did not become true within {timeout}s") from None


@dataclass
class Stack:
    http: httpx.AsyncClient
    app: FastAPI
    mqtt_url: str
    uow_factory: UnitOfWorkFactory
    live: LiveView | None = None
    worker_config: WorkerConfig = field(default_factory=WorkerConfig)
    sims: dict[UUID, SimulatedDrone] = field(default_factory=dict)
    runners: dict[UUID, SimulatorRunner] = field(default_factory=dict)
    worker: Worker | None = None
    worker_id: str = field(default_factory=lambda: f"worker-{uuid4().hex[:10]}")
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _worker_task: asyncio.Task[None] | None = None

    async def add_drone(
        self,
        *,
        speed_factor: float = 3000.0,
        hold_s: float = 1.0,
        play: bool = True,
        telemetry_interval_s: float = 0.1,
        **drone_options: Any,
    ) -> SimulatedDrone:
        """Register a drone with the platform and (by default) start simulating it."""
        registered = await register_drone(self.http, name=f"drone-{uuid4().hex[:8]}")
        drone = SimulatedDrone(
            UUID(registered["id"]),
            home=HOME,
            speed_factor=speed_factor,
            hold_s=hold_s,
            **drone_options,
        )
        self.sims[drone.drone_id] = drone
        if play:
            client = MqttClient(self.mqtt_url, client_id=f"sim-{drone.drone_id.hex[:12]}")
            runner = SimulatorRunner(
                [drone], client, tick_s=0.02, telemetry_interval_s=telemetry_interval_s
            )
            self.runners[drone.drone_id] = runner
            self._tasks.append(asyncio.create_task(runner.run()))
            await client.wait_connected(5)
        return drone

    async def start_worker(self) -> Worker:
        client = MqttClient(self.mqtt_url, client_id=self.worker_id, durable=True)
        self.worker = Worker(self.uow_factory, client, live=self.live, config=self.worker_config)
        self._worker_task = asyncio.create_task(self.worker.run())
        await client.wait_connected(5)
        return self.worker

    async def stop_worker(self) -> None:
        assert self._worker_task is not None
        self._worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None

    async def publish_raw(self, topic: str, payload: bytes, *, qos: int = 1) -> None:
        client = MqttClient(self.mqtt_url, client_id=f"raw-{uuid4().hex[:8]}")
        task = asyncio.create_task(client.run({}))
        try:
            await client.wait_connected(5)
            await client.publish(topic, payload, qos=qos)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._worker_task is not None:
            await self.stop_worker()

    # --- observing ---------------------------------------------------------------------------

    async def order(self, order_id: UUID) -> dict[str, Any]:
        body: dict[str, Any] = (await self.http.get(f"/internal/orders/{order_id}")).json()
        return body

    async def flights(self, order_id: UUID) -> list[dict[str, Any]]:
        body: list[dict[str, Any]] = (
            await self.http.get(f"/internal/orders/{order_id}/flights")
        ).json()
        return body

    async def drone(self, drone_id: UUID) -> dict[str, Any]:
        body: dict[str, Any] = (await self.http.get(f"/internal/drones/{drone_id}")).json()
        return body

    async def drone_status(self, drone_id: UUID) -> str:
        return str((await self.drone(drone_id))["status"])

    async def alerts(self) -> dict[str, Any]:
        body: dict[str, Any] = (await self.http.get("/internal/alerts")).json()
        return body


async def audit(run_sql: RunSql, order_id: UUID) -> list[tuple[str, str, str]]:
    rows = await run_sql(
        "SELECT event_type, new_state, source FROM audit_events WHERE order_id = :o ORDER BY seq",
        o=order_id,
    )
    return [(r[0], r[1], r[2]) for r in rows]


def picked_up_event(flight_id: str, seq: int, type_: str = "package_picked_up") -> bytes:
    return wire.encode(
        wire.DroneEvent(
            event_id=uuid4(),
            flight_id=UUID(flight_id),
            seq=seq,
            type=wire.EventType(type_),
            occurred_at=T0,
        )
    )


async def is_(awaitable: Awaitable[str], expected: str) -> bool:
    return (await awaitable) == expected


async def order_is(stack: Stack, order_id: UUID, expected: str) -> bool:
    return (await stack.order(order_id))["status"] == expected


async def counted(stack: Stack, outcome: str, at_least: int) -> bool:
    assert stack.worker is not None
    return stack.worker.ingest.counts[outcome] >= at_least  # type: ignore[index]


async def have(items: list[Any]) -> bool:
    return bool(items)
