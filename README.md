# Drone Delivery Platform

A B2B drone delivery platform. Partners (Swiggy, Zomato, and others) submit delivery
orders through dedicated client adapters. The platform validates orders, assigns drones,
obtains flight permission from the compliance module, uploads predefined missions, sends
high-level flight commands, ingests live telemetry, streams tracking to clients and ops,
reports status back to partners, and keeps an append-only audit trail.

**Stack:** Python 3.12, FastAPI, Pydantic v2, PostgreSQL + PostGIS, SQLAlchemy 2.0 (async),
Alembic, Redis, MQTT (Mosquitto), WebSockets, pytest / pytest-asyncio, Docker Compose.

## Architecture rules (non-negotiable)

1. **The cloud never steers a drone.** It only uploads predefined missions and sends
   high-level commands: start mission, pause mission, return to home (RTH), land. The
   drone's onboard flight controller executes the mission.
2. **Flight permission is mandatory.** Dispatch: identify a drone, request permission,
   validate it, prepare and upload the mission, and only then allow the flight to start.
   Denied, expired or invalid permission means no flight.
3. **Client adapters are mandatory.** Partners never reach core logic directly. Each
   adapter validates its client's payloads, maps them to the internal order model, and
   translates status updates back into the client's format. Partner formats never enter
   the domain layer.
4. **The order state machine is mandatory.** Every status change goes through
   `app/domain/order_state_machine.py`. Never set a status through SQLAlchemy models,
   raw queries, API endpoints or background tasks.
5. **Audit logging is append-only.** Every flight and every order status change is
   recorded with event id, order id, drone id, event type, previous/new state, timestamp,
   metadata and source. Records are never silently modified or deleted.

## Order lifecycle

```
created -> assigned -> picked_up -> in_flight -> arriving -> delivered

any active state -> failed
any active state -> cancelled
in_flight -> returned
arriving  -> returned
```

Active states: `created`, `assigned`, `picked_up`, `in_flight`, `arriving`.
Terminal states: `delivered`, `failed`, `cancelled`, `returned`.

### How the state machine is used

```python
machine = OrderStateMachine()

result = machine.create(
    client_id="mock",
    external_order_id="ORD-1",
    pickup=Location(12.97, 77.59),
    dropoff=Location(12.93, 77.62),
    package=Package(weight_kg=1.2),
    source=EventSource.API,
)
order, event = result.order, result.audit_event  # persist BOTH together

result = machine.transition(
    order, OrderStatus.ASSIGNED, source=EventSource.DISPATCH, drone_id=drone_id
)
```

`create` and `transition` are pure: they return a new immutable `Order` plus the
`AuditEvent` that describes the change. Invalid changes raise `InvalidTransitionError`
(or `TerminalStateError`) and produce nothing.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # create .venv (Python 3.12) and install dev tools
uv run pytest                # run the tests
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy                  # strict type-check of app/domain
```

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation and domain core: order model, state machine, audit event, exceptions | **done** |
| 2 | Persistence and audit: Docker Compose, async SQLAlchemy, Alembic, `OrderService`, append-only audit table | planned |
| 3 | Client adapters (mock, Swiggy, Zomato) and order API | planned |
| 4 | Compliance (mock Digital Sky) and dispatch (assignment, missions, high-level commands) | planned |
| 5 | Telemetry: MQTT ingest, Redis, WebSockets, drone simulator | planned |
| 6 | Webhooks: sender, retries, idempotency, failure tracking | planned |
| 7 | Ops dashboard APIs, structured logging, end-to-end tests | planned |

## Decisions Log

- [2026-11-11] Python is pinned to 3.12 (`>=3.12,<3.13`) and managed with `uv`: reproducible environments and a lockfile, and the machine default is 3.13.
- [2026-11-11] The domain layer uses stdlib frozen dataclasses; Pydantic v2 is used only at the edges (API schemas, adapter payloads, settings): the domain stays free of third-party imports and I/O.
- [2026-11-11] The state machine is a functional core whose `create`/`transition` return `TransitionResult(order, audit_event)`: a status change cannot exist without its audit event (Rules 4 and 5).
- [2026-11-11] The transition table is declarative and built from the spec (17 allowed edges): one place to read and one place to change the lifecycle.
- [2026-11-11] `delivered`, `failed`, `cancelled` and `returned` are terminal: the spec defines no edges out of them.
- [2026-11-11] Moving to the current status is rejected as an invalid transition: idempotent retries belong in the service layer, not the lifecycle.
- [2026-11-11] Cancelling or failing an in-flight order is only a state change in the domain: sending RTH to the drone is dispatch's job, so the domain never commands a drone (Rule 1).
- [2026-11-11] `created -> assigned` requires a `drone_id`, and orders past `created` must carry one (except `failed`/`cancelled`): an assigned order without a drone is meaningless.
- [2026-11-11] `Order.rehydrate()` exists for repositories to rebuild orders in any state; it is documented as persistence-only and will be guarded by an architecture test and a DB trigger in Phase 2: loading from the DB must reach every state, but must never be a way to change one.
- [2026-11-11] The state machine takes an injectable clock and id factory: deterministic tests with no hidden `datetime.now()` or `uuid4()` calls.
- [2026-11-11] Audit metadata is deep-frozen and restricted to JSON-compatible values: events are immutable facts and are persisted as JSON.
- [2026-11-11] Runtime dependencies and package directories are added in the phase that first needs them: no unused dependencies or empty modules.
- [2026-11-11] The Decisions Log lives in this README: one place to find project context.
