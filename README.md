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

## How Rules 4 and 5 are enforced

Several independent layers, so no single mistake can break a rule:

| Layer | Rule 4 (status only via the state machine) | Rule 5 (append-only audit) |
|---|---|---|
| Domain | `Order` is frozen; `TransitionResult` refuses to exist with a mismatched event or an illegal edge | `AuditEvent` is frozen and deep-immutable |
| Repository | No generic `save()`. Writes take only a `TransitionResult`, with `SELECT ... FOR UPDATE` and a compare-and-set on the previous status | `SqlAlchemyAuditLog` has insert and read only, and the audit row is written in the same transaction as the order change |
| Architecture test | `tests/unit/test_no_direct_status_writes.py` scans `app/` for `.status =`, table-class use, raw SQL, `rehydrate` and `replace(status=...)` outside the blessed path | same scan covers the audit table |
| Database | A deferred constraint trigger refuses any new order not in `created` or without its `order_created` audit row, and any status change that is not an edge in `order_transitions` or lacks a matching audit row | Triggers reject `UPDATE`, `DELETE` and `TRUNCATE` on `audit_events`; the foreign key to `orders` is `RESTRICT`; `UNIQUE(order_id, new_state)` stops an order entering a state twice |

The database guards protect against mistakes and raw-SQL bypass. A database owner or
superuser can still drop triggers; using a separate migration role with no DDL rights for
the application role is a later hardening step.

Usage through the service (the only caller of the repository's write methods):

```python
service = OrderService(lambda: SqlAlchemyUnitOfWork(session_factory))

created = await service.create_order(client_id="mock", external_order_id="ORD-1", ...)
# created.created is False if (client_id, external_order_id) already existed (idempotent)

order = await service.transition(
    created.order.id, OrderStatus.ASSIGNED, source=EventSource.DISPATCH, drone_id=drone_id
)
trail = await service.get_audit_trail(order.id)
```

## Development

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and, for the database,
Docker (or a local PostgreSQL 16 with PostGIS).

```bash
uv sync                      # create .venv (Python 3.12) and install dependencies
cp .env.example .env         # local settings
docker compose up -d postgres
uv run alembic upgrade head  # create the schema
```

```bash
uv run pytest                # all tests
uv run pytest -m "not integration"   # only tests that need no database
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy                  # strict type-check of app/
```

### Integration tests

Tests marked `integration` need PostgreSQL with PostGIS. A migrated template database is
created once per run and every test gets its own throwaway clone, so tests never share
state. Configure the admin connection with `TEST_DATABASE_ADMIN_URL` (default
`postgresql://drone:drone@localhost:5432/postgres`; the role needs `CREATEDB` and permission
to `CREATE EXTENSION postgis`).

If the database is unreachable those tests are **skipped with the reason shown**. Set
`REQUIRE_INTEGRATION=1` (use this in CI) to make that an error instead, so a missing
database can never look like a green run.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation and domain core: order model, state machine, audit event, exceptions | **done** |
| 2 | Persistence and audit: PostgreSQL/PostGIS, async SQLAlchemy, Alembic, `OrderService`, append-only audit table, DB lifecycle guard | **done** |
| 3 | Client adapters (mock, Swiggy, Zomato) and order API | planned |
| 4 | Compliance (mock Digital Sky) and dispatch (assignment, missions, high-level commands) | planned |
| 5 | Telemetry: MQTT ingest, Redis, WebSockets, drone simulator | planned |
| 6 | Webhooks: sender, retries, idempotency, failure tracking | planned |
| 7 | Ops dashboard APIs, hardening, end-to-end tests | planned |

## Decisions Log

- [2026-11-11] Python is pinned to 3.12 (`>=3.12,<3.13`) and managed with `uv`: reproducible environments and a lockfile, and the machine default is 3.13.
- [2026-11-11] The domain layer uses stdlib frozen dataclasses; Pydantic v2 is used only at the edges (API schemas, adapter payloads, settings): the domain stays free of third-party imports and I/O.
- [2026-11-11] The state machine is a functional core whose `create`/`transition` return `TransitionResult(order, audit_event)`: a status change cannot exist without its audit event (Rules 4 and 5).
- [2026-11-11] The transition table is declarative and built from the spec (17 allowed edges): one place to read and one place to change the lifecycle.
- [2026-11-11] `delivered`, `failed`, `cancelled` and `returned` are terminal: the spec defines no edges out of them.
- [2026-11-11] Moving to the current status is rejected as an invalid transition: idempotent retries belong in the service layer, not the lifecycle.
- [2026-11-11] Cancelling or failing an in-flight order is only a state change in the domain: sending RTH to the drone is dispatch's job, so the domain never commands a drone (Rule 1).
- [2026-11-11] `created -> assigned` requires a `drone_id`, and orders past `created` must carry one (except `failed`/`cancelled`): an assigned order without a drone is meaningless.
- [2026-11-11] `Order.rehydrate()` exists for repositories to rebuild orders in any state; it is documented as persistence-only and is guarded by an architecture test and the DB lifecycle trigger (Phase 2): loading from the DB must reach every state, but must never be a way to change one.
- [2026-11-11] The state machine takes an injectable clock and id factory: deterministic tests with no hidden `datetime.now()` or `uuid4()` calls.
- [2026-11-11] Audit metadata is deep-frozen and restricted to JSON-compatible values: events are immutable facts and are persisted as JSON.
- [2026-11-11] Runtime dependencies and package directories are added in the phase that first needs them: no unused dependencies or empty modules.
- [2026-11-11] The Decisions Log lives in this README: one place to find project context.
- [2026-11-11] `TransitionResult` validates itself (event matches order, status change is an allowed edge): the repository accepts it as proof of a valid change, so it must not be forgeable by hand.
- [2026-11-11] Orders and audit events are written only through `OrderService` -> `UnitOfWork`; the repository has no generic `save()`/`update_status()`: Rule 4 has no code path to bypass.
- [2026-11-11] Order status is `TEXT` with a `CHECK` rather than a Postgres ENUM: new states are a plain migration instead of `ALTER TYPE`.
- [2026-11-11] Locations are PostGIS `geography(Point, 4326)` read back with `ST_X`/`ST_Y`, without shapely: no extra dependency, and tests pin the (longitude, latitude) order.
- [2026-11-11] Concurrency uses `SELECT ... FOR UPDATE` plus a compare-and-set on the previous status, with no version column: the state graph has no cycles, so a matching status proves nothing else moved the order.
- [2026-11-11] Order creation is idempotent on `(client_id, external_order_id)` via `INSERT ... ON CONFLICT DO NOTHING`; a repeat returns the existing order unchanged and writes no audit row: partners retry, and the payload of a retry is not compared.
- [2026-11-11] The database enforces the lifecycle with a deferred constraint trigger plus a seeded, immutable `order_transitions` table, with a unit test against the Python table: raw SQL is the only way to bypass the Python layers, and Rule 4 names database queries explicitly.
- [2026-11-11] `audit_events` rejects UPDATE/DELETE/TRUNCATE by trigger, has an `ON DELETE RESTRICT` foreign key and `UNIQUE(order_id, new_state)`: the audit trail is append-only and an order cannot enter a state twice.
- [2026-11-11] `drone_id` is a plain UUID until the dispatch phase adds the `drones` table and its foreign key: avoids designing the drone model before it is needed.
- [2026-11-11] The initial migration is hand-written: autogenerate cannot produce triggers or PostGIS indexes, and a test asserts the models and the migrated schema agree.
- [2026-11-11] Integration tests use one database per test, cloned from a migrated template: TRUNCATE is blocked by design, and rollback-only isolation would never fire the commit-time trigger.
- [2026-11-11] Integration tests skip with a reason when PostgreSQL is unreachable, and fail under `REQUIRE_INTEGRATION=1`: a missing database must never look like a green run.
- [2026-11-11] The service contract tests run against both the in-memory and PostgreSQL unit of work: keeps the fast fake honest.
- [2026-11-11] Docker Compose contains only PostgreSQL/PostGIS for now; Redis and Mosquitto arrive with telemetry (Phase 5) and the app image with the API (Phase 3): nothing unused and untestable.
- [2026-11-11] `structlog` is configured now (`app/logging.py`) because the service logs every create and transition: avoids retrofitting logging across phases.
- [2026-11-11] `mypy --strict` now covers all of `app/`, and ruff treats the project root as first-party: consistent import grouping and type checking everywhere.
