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

## Partner API and adapters (Rule 3)

Partners never reach core logic. Every partner request goes through one gateway and is
handled by that partner's **adapter**, which validates the payload, maps it into the
internal `OrderRequest`, and renders every response and error in the partner's own format.
Only internal types cross into `OrderService`.

| Endpoint | Caller | Behaviour |
|---|---|---|
| `POST /partner/{client_id}/orders` | partner | Parse via the adapter, create the order. `201` if new, `200` if the partner's id was already seen (idempotent) |
| `GET /partner/{client_id}/orders/{external_order_id}` | partner | Look up by the partner's own id; partner-format body |
| `POST /partner/{client_id}/orders/{external_order_id}/cancel` | partner | Cancel (idempotent). A flying drone is recalled first; see [Cancellation](#cancellation) |
| `GET /internal/orders/{order_id}` | internal | Platform-format order |
| `GET /internal/orders/{order_id}/status` | internal | Status, terminal flag, last update |
| `GET /health`, `GET /ready` | ops | Liveness; readiness (checks the database) |

There is no `PUT`/`PATCH`/`DELETE` and no endpoint that accepts a status: orders move only
through the state machine (Rule 4). The internal endpoints for drones, flights and dispatch
are described under [Dispatch, drone commands and cancellation](#dispatch-drone-commands-and-cancellation).

Integrations: `mock` (generic, for development and tests), `swiggy` and `zomato`.

> **The Swiggy and Zomato payload formats are illustrative, not the partners' real
> contracts.** They exist to exercise the adapter architecture (unit conversion, differing
> coordinate order, different status vocabularies and error shapes). When the real
> specifications are available, replace the mapping inside `app/adapters/<partner>/`; nothing
> outside that package changes.

**There is no authentication** (it has not been requested): partners are identified only by
`client_id` in the URL. Do not expose these endpoints publicly until auth is added.

### Adding a partner

1. Create `app/adapters/<name>/` with `schemas.py` (Pydantic inbound models built from
   `app.adapters.common`) and `adapter.py` implementing `ClientAdapter`
   (`parse_order`, `render_order`, `render_status_update`, `render_error`).
2. Register it in `app/adapters/registry.py::default_registry`.
3. Add a sample payload to `tests/payloads.py` (`VALID_PAYLOADS`, `EXTERNAL_IDS`) and the
   response accessors in `tests/api/conftest.py`. The contract tests in
   `tests/unit/adapters/test_adapter_contract.py` then apply to it automatically: junk
   input must raise only `AdapterValidationError`, every status must translate, responses
   must be JSON-serialisable and must not leak internal data such as the drone id.
4. Write the partner-specific mapping tests.

Import boundaries are enforced by `tests/unit/test_import_boundaries.py`: adapters import
only the domain and Pydantic; core code never imports adapters or the HTTP layer; the API
layer never touches the database layer; and partner names appear nowhere outside
`app/adapters/`.

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

## Drones, flights and the Rule 2 gate

A **flight** is one drone flying one mission for one order. It has its own state machine
(`app/domain/flight_state_machine.py`), built the same way as the order's: it returns the
new flight plus its audit event, and persistence accepts only that result.

```
planned -> permission_granted -> mission_uploaded -> started <-> paused
planned -> permission_denied | cancelled
permission_granted -> upload_failed | cancelled
mission_uploaded -> cancelled
started | paused -> aborted            started -> completed
```

**Entering `started` (the first start and a resume) is the gate.** It requires all of:

1. a granted permission and a mission the drone acknowledged (by checksum),
2. a fresh re-check of the permission with the compliance provider: not revoked or unknown,
   and no older than 30 seconds,
3. the permission in force right now (the earlier of what we stored and what the provider
   reports now), and
4. enough validity left to cover the whole mission.

Anything else raises a specific error (`PermissionRevokedError`, `PermissionExpiredError`,
`PermissionWindowTooShortError`, ...) and nothing is persisted. A successful start also yields
a `StartAuthorization`, a token only the state machine can create, which the drone link
requires before it will send a start command.

**Safety commands are never gated.** Pause, abort (return-to-home, land) and complete do not
consult the permission: they must work even if it has expired or been revoked.

| Layer | Enforcement of the start gate |
|---|---|
| Domain | `FlightStateMachine` checks all four conditions; `FlightTransitionResult` refuses to exist without a matching `StartAuthorization` for every entry into `started`, and for nothing else |
| Architecture | `tests/unit/test_no_direct_status_writes.py` bans minting a `StartAuthorization` anywhere else, and bans writing flights or drones outside the dispatch service |
| Database | `flights_enforce_lifecycle` (deferred constraint trigger): valid edge, audit row from the *same transaction*, write-once mission/permission columns, and for `started` a stored permission, an acknowledged mission, a permission still in force *and* long enough |
| Database | At most one live flight per drone and per order (partial unique indexes): a drone cannot be double-booked. Drone status changes are compare-and-set; dispatch claims the drone it chose that way (the store can also list drones with `FOR UPDATE SKIP LOCKED`, which dispatch deliberately does not use: see the Decisions Log) |

The database cannot see the provider re-check, so a raw start that satisfies its data
conditions is accepted; the Python gate is what insists on the fresh re-check. The trigger
compares the permission's end with the **database** clock, so keep application and database
clocks in sync.

**Compliance** (`app/compliance`) is an interface with two questions: *may this drone fly this
route?* and *is this permission still in force?* Only a **mock Digital Sky** exists (no-fly
circles, closed airspace, revocation, expiry, outages); a real integration is not built until
it is requested, and the config only accepts `COMPLIANCE_PROVIDER=mock`. If the provider
cannot be reached the platform **fails closed**: no answer means no permission.

**Drone selection** (`app/dispatch/assignment.py`) is a pure function: a drone must be
available, strong enough, charged enough, and able to fly the whole sortie (home -> pickup ->
dropoff -> home) within its range less a safety reserve. The nearest suitable drone wins, then
higher battery, then lowest id; every ruled-out drone is reported with its reasons.

A **mission** is data (waypoints and a checksum). The cloud uploads it and may later send
start / pause / return-to-home / land; the drone flies it (Rule 1).

## Dispatch, drone commands and cancellation

`DispatchService` (`app/dispatch/service.py`) is the only code that writes flights and drones
and the only code that talks to a drone (architecture tests enforce both). It never changes an
order itself: it asks `OrderService`, so the order state machine stays the single door (Rule 4).

### The drone link (Rule 1)

`DroneLink` (`app/dispatch/link.py`) has exactly five operations: **upload a mission**,
**start**, **pause**, **return to home**, **land**. Their parameters are only a drone id, a
mission, and (for start) the `StartAuthorization` from the Rule 2 gate: there is no way to hand
a drone a position, heading, speed or status. A mission is a short list of high-level waypoints
(takeoff, pickup, dropoff, return, land) planned by `app/dispatch/mission.py` as a straight-line
sortie at fixed altitudes; the drone's own controller flies it. Failures are either
*rejected* (the drone definitely did not act) or *no acknowledgement* (unknown: it may have).

Two implementations: an **in-memory stand-in** (`DRONE_LINK=in_memory`, the default: it behaves
like a well-mannered drone and can be told to fail, for tests) and the **MQTT link**
(`DRONE_LINK=mqtt`, see [Telemetry and drones over MQTT](#telemetry-and-drones-over-mqtt)).

### The dispatch workflow (Rule 2)

`POST /internal/orders/{order_id}/dispatch` (add `?start=false` to stage the mission without
starting). Each step is its own short transaction (no transaction is held across a network call,
and each step re-reads the flight under a lock):

1. choose the best suitable drone, **claim** it (compare-and-set), plan the mission, create the
   `planned` flight;
2. request permission from the compliance provider (an outage counts as a denial);
3. validate the granted permission;
4. upload the mission: the drone must echo its checksum;
5. only now move the order `created -> assigned`, so `assigned` means *ready to fly*;
6. start the flight: the Rule 2 gate runs and is **recorded before** the start command is sent.

What each ending leaves behind (every row that leaves the order `created` is safely retryable;
a retry plans a new flight and the old one stays in the history):

| Outcome | Flight | Drone | Order | Reached the drone |
|---|---|---|---|---|
| `started` | `started` | `on_mission` | `assigned` | upload, start |
| `ready` (`start=false`) | `mission_uploaded` | `assigned` | `assigned` | upload |
| `no_drone_available` | none | untouched | `created` | nothing |
| `permission_denied` (incl. provider outage) | `permission_denied` | `available` | `created` | nothing |
| `permission_invalid` (revoked/expired right after the grant) | `cancelled` | `available` | `created` | nothing |
| `upload_failed` (refused, no answer, or wrong checksum) | `upload_failed` | `available` | `created` | upload (maybe) |
| `cancelled_during_dispatch` | `cancelled` | `available` | `cancelled` | up to upload |
| `start_failed`: the gate refused | `cancelled` | `available` | `failed` | upload |
| `start_failed`: the drone refused the start | `aborted` | `available` | `failed` | upload |
| `start_failed`: the start went unanswered | `aborted` | `returning` | `failed` | upload, start, return-to-home |

Dispatch answers **200 with an `outcome`** for all of these: a refusal is a normal, well-defined
result, not a request error. `404` means an unknown order; `409` means the order is not
`created` or already has a live flight.

### Flight commands

| Endpoint | Does | Gated by permission? |
|---|---|---|
| `POST /internal/flights/{id}/start` | start a staged flight | **yes** (Rule 2 gate) |
| `POST /internal/flights/{id}/resume` | resume a paused flight | **yes**, same gate |
| `POST /internal/flights/{id}/pause` | pause a flying mission | never |
| `POST /internal/flights/{id}/return-to-home` | recall the drone; aborts the flight | never |
| `POST /internal/flights/{id}/land` | land where it is; aborts the flight | never |
| `GET /internal/flights/{id}`, `GET /internal/orders/{id}/flights` | read | |

Return-to-home and land accept an optional `{"reason": "..."}` and nothing else. Two orderings,
each failing in its safe direction: **start and resume record the intent first, then send** (a
lost command is then undone: the flight is aborted and a return-to-home is sent as a
precaution), whereas **pause, return-to-home and land send first, then record** (if the drone
does not confirm, nothing is recorded and the caller gets a retryable `503`). A *refused*
resume puts the flight back to `paused`, because the drone is still hovering where it was.

| Error | When |
|---|---|
| `409 permission_refused` | the Rule 2 gate refused a start or resume; nothing changed, nothing was sent |
| `409 conflict` | not allowed in the current state (flight, drone or order) |
| `503 command_not_confirmed` | the drone did not confirm a pause/return/land; nothing was recorded; retry |
| `502 start_failed` | the drone did not take the start; the flight was cleaned up (see above) |

### Fleet endpoints

`POST /internal/drones` (register; a new drone is `available`), `GET /internal/drones`,
`GET /internal/drones/{id}`, and two verbs: `POST .../docked` (operations confirms a recalled
or grounded drone is back at its dock) and `POST .../maintenance`. Neither accepts a status;
both refuse a drone that has a live flight, and any change the drone's state machine forbids.

### Cancellation

`POST /partner/{client_id}/orders/{external_order_id}/cancel`, answered in the partner's own
format. A not-yet-flying order is simply cancelled (a staged flight is cancelled and its drone
released). If the drone is **flying, it is recalled first**, and only when it confirms are the
flight aborted and the order cancelled. **If the recall cannot be confirmed the order is not
cancelled**: the partner gets `503` (retry), because a cancelled order must never leave a
drone flying. Cancelling an already-cancelled order is a no-op; a finished order (delivered,
failed, returned) answers `409`.

### Known limits of dispatch (Phase 4B)

- A crash between dispatch steps can leave a drone `assigned` and a flight live (there is no
  reaper). Cancelling the order, or `docked` once the flight is over, releases it.
- After a recall the drone stays `returning` until it reports `docked` (the telemetry worker
  records it; without a worker, operations calls `docked`). After a `land` it stays `offline`
  until it is recovered and reports `docked`, or operations calls `docked`.
- A refused or failed command is **logged**, not audited: the audit trail records status
  changes, and a command that changed nothing is in the structured log.
- A cancelled flight's mission may remain on the drone until the next upload replaces it
  (there is deliberately no "clear mission" command).
- The database gate cannot see the provider re-check and uses the database clock (see above).
- Still no authentication, billing or real Digital Sky integration (none has been requested).

## Telemetry and drones over MQTT

Phase 5A. Drones are separate machines that fly missions on their own, so the platform talks to
them over **MQTT** (Mosquitto in compose), and a separate **worker process** records what they
report. The wire contract lives in `app/protocol` (validated models and every topic name), shared
by the link, the worker and the simulator so none depends on another.

```
API process                         broker                       drone (or simulator)
 MqttDroneLink --- drones/{id}/commands --->               --->  checks, then acts
               <-- platform/acks/{instance} ---            <---  acknowledges (matched by command_id)
                                    drones/{id}/events <---       reports progress
 telemetry worker  <------------------------------------
   TelemetryIngest -> DispatchService.record_progress -> OrderService / state machines
```

**Commands** (`DRONE_LINK=mqtt`). Each carries a `command_id`, and the drone answers on the reply
topic named in the command, so any number of API processes can share one broker. A start carries
the Rule 2 authorisation (flight, permission, mission checksum, expiry), which the drone checks
itself. The failure kinds map onto the two the service already understands: **no connection** to
the broker means the command was *definitely not sent* (rejected); **no acknowledgement in time**
(`MQTT_ACK_TIMEOUT_SECONDS`) or a publish that failed part-way means *unknown*.
`GET /ready` fails while the API is not connected to the broker.

**Events** (drone -> platform, QoS 1, durable session). A drone reports what happened, never what
to do. Each event applies at most one change, only if it fits, and never invents a missing step:

| Event | Effect |
|---|---|
| `package_picked_up` | order `assigned -> picked_up` |
| `departed_pickup` | order `picked_up -> in_flight` |
| `arriving_at_dropoff` | order `in_flight -> arriving` |
| `package_delivered` | order `arriving -> delivered` |
| `mission_completed` | flight `started -> completed`, drone `on_mission -> available` (no manual `docked` needed) |
| `docked` | a recalled (`returning`) or grounded (`offline`) drone becomes `available` |
| `fault` | logged as an error; **nothing changes and nothing is sent** (alerts only: any automatic recall is a policy decision for later) |

Every event carries a per-flight sequence number. A repeat or an older one is dropped (and
counted) before it touches anything; MQTT delivers at least once, and an event whose effect is
already recorded is "already applied", which also covers a worker restart. An event that does not
fit (a delivery for an order that was never picked up, a flight that is not flying, another
drone's flight, a cancelled order) is rejected and logged. Applied events are audited with
source `telemetry`; the audit keeps the event name, flight and time, never the drone's free text.

**Rule 1 and 4 on this path.** The worker's dispatch service is built with a link and a
compliance provider that refuse everything, so that process *cannot* command a drone or request
permission even through a bug. The ingest has no MQTT client and no store (architecture tests
prove it), only the dispatch service, which is still the only writer of flights and drones. Only
the MQTT link publishes to command topics.

### The worker and the simulator

```bash
python -m app.worker                      # run exactly ONE worker per broker (durable session)
python -m app.simulator --api-url http://localhost:8000 --fleet 3 --speed-factor 30
```

The **simulator** (`app/simulator`) plays drones: it registers `sim-1..N` with the API (or takes
`--drone-id`), receives the same five commands, recomputes the mission checksum and refuses an
expired, mis-addressed or mismatched authorisation as a real drone should, flies the mission on a
fast-forwardable clock, and reports events (queued and retried in order if the broker blinks). It
is a development and test tool, not part of the platform.

```bash
docker compose up --build                       # API + worker + Postgres + Mosquitto
docker compose --profile simulator up --build   # ...plus three simulated drones
```

### Known limits of the MQTT phase (5A)

- **No auth or TLS on the broker** (none was requested): the dev `mosquitto.conf` accepts anonymous
  connections. Do not expose it, and do not point real drones at it as it is.
- A drone's own failsafe (it aborts and lands by itself) is not reported yet, so the platform would
  still think it is flying; nothing in this phase detects a silent or lost drone. Heartbeats,
  alerts and a reaper for stale reservations arrive with live tracking (Phase 5B).
- A worker outage longer than its retry window (about 8 s of failing database calls) loses the event
  it was handling, because the broker has already been acknowledged; events sent while the worker
  is merely *away* are kept by the durable session. Run one worker only.
- Telemetry samples (position, battery), Redis and WebSockets are Phase 5B.

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
uv run pytest -m "not integration"   # only tests that need no database or broker
uv run ruff check .          # lint
uv run ruff format --check . # formatting
uv run mypy                  # strict type-check of app/
```

### Running the API

```bash
uv run uvicorn app.main:create_app --factory --reload   # needs the database from above
# or, with everything in containers (applies migrations, then serves on :8000):
docker compose up --build
```

With `DRONE_LINK=mqtt` (the compose default) the API needs the broker and something playing the
drones, or a dispatch ends in `upload_failed: no acknowledgement`. For local development without
a broker, keep the default `DRONE_LINK=in_memory`.

Interactive docs are at `http://localhost:8000/docs`. Example:

```bash
curl -X POST localhost:8000/partner/mock/orders -H 'content-type: application/json' -d '{
  "order_id": "ORD-1",
  "pickup":  {"lat": 12.9716, "lng": 77.5946},
  "dropoff": {"lat": 12.9352, "lng": 77.6245},
  "package": {"weight_kg": 1.5, "description": "2 meals"}}'
```

### Integration tests

Tests marked `integration` need PostgreSQL with PostGIS and, for the MQTT tests, a `mosquitto`
binary (each run starts its own throwaway broker on a free port). A migrated template database is
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
| 3 | Client adapters (mock, Swiggy, Zomato) and order API, Dockerfile | **done** |
| 4A | Drones, compliance (mock Digital Sky) and the flight lifecycle: flight state machine with the Rule 2 start gate, drone selection, drone/flight persistence and database guards | **done** |
| 4B | Dispatch workflow, mission planner, drone link (start/pause/RTH/land), cancellation, internal dispatch API | **done** |
| 5A | MQTT drone link and wire contract, drone-reported progress events, telemetry worker, drone simulator, Mosquitto in compose | **done** |
| 5B | Telemetry samples, Redis, WebSocket tracking streams, heartbeats and alerts, reaper for stale reservations | planned |
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
- [2026-11-12] Partner traffic enters through one generic gateway (`/partner/{client_id}/...`) that dispatches to that partner's adapter: routing is written once, and each adapter owns its payload and response formats (Rule 3).
- [2026-11-12] `OrderRequest` is the only type that crosses from adapters into core, and the gateway unpacks it into the unchanged `OrderService.create_order`: partner formats cannot leak, and no Phase 2 call site changed.
- [2026-11-12] Adapters import only the domain and Pydantic, core never imports adapters or the HTTP layer, the API never imports the database layer, and partner names appear only under `app/adapters/`: all enforced by tests, not convention.
- [2026-11-12] Inbound partner models are tolerant readers (unknown fields ignored) but strict about the fields they read (no string-to-number coercion, no booleans as numbers, no NaN/infinity): forward-compatible with partners without accepting garbage.
- [2026-11-12] Validation errors carry a field path and message but never echo the offending value, and adapters must raise only `AdapterValidationError`: nothing sensitive or internal leaks into partner responses.
- [2026-11-12] Partner ids are whitespace-stripped and capped at 128 chars, descriptions at 500: the idempotency key is normalised and unbounded partner strings do not reach the database.
- [2026-11-12] The Swiggy and Zomato formats are illustrative, because the real specifications are not available: the mapping is isolated per package so the real contracts replace it without touching anything else.
- [2026-11-12] Every partner gets `201` for a new order and `200` for a repeat or lookup; errors are rendered in the partner's format; an unknown client gets a neutral 404; unexpected failures become a partner-format 500 with no internal detail.
- [2026-11-12] No cancel endpoint until dispatch exists (Phase 4): cancelling an in-flight order without a return-to-home command would leave a drone flying on a cancelled order.
- [2026-11-12] The internal API is read-only and in platform format, and a test inspects the OpenAPI document to prove no operation can write a status (Rule 4); the inspection is itself tested against a deliberately bad app so it cannot pass by seeing nothing.
- [2026-11-12] No authentication on any endpoint, as it has not been requested: partners are identified by the URL only, so the API must not be exposed publicly yet.
- [2026-11-12] `create_app` takes an injectable unit-of-work factory and builds the database engine in the lifespan otherwise: API tests run on the in-memory fake with no database, and startup does not fail when the database is down (readiness reports it).
- [2026-11-12] `UnitOfWork.orders`/`.audit` are read-only protocol properties, not mutable attributes: mutable protocol attributes are invariant, so the SQLAlchemy implementation with narrower types did not satisfy the protocol (found by mypy).
- [2026-11-12] The Docker image installs from `uv.lock` with `--frozen --no-dev` (uv pinned), and migrations run in the compose command but should be a separate release step in production: reproducible images with no test tooling.
- [2026-11-12] Creation audit metadata records only `client_id`; the raw partner payload is never stored in the domain or the audit log: partner formats stay out of core data.
- [2026-11-12] Flights are a first-class aggregate with their own state machine (10 states, 13 edges, built like the order's): Rule 2's workflow is exactly a flight's lifecycle, and it inherits the pure, audited, unforgeable-result pattern.
- [2026-11-12] Entering `started` is the one gate for both first start and resume, and it needs a granted permission, an acknowledged mission, a fresh non-revoked provider re-check, a permission in force, and enough validity to cover the mission: a permission can expire or be revoked between grant and start, or while paused.
- [2026-11-12] The gate trusts the earlier of the stored permission end and the provider's current answer, and a re-check older than 30 seconds is stale: the provider may shorten a permission, and a stale answer proves nothing.
- [2026-11-12] Pause, abort (return-to-home, land) and complete are never permission-gated, and passing a validation to them is rejected: safety commands must work even when a permission has expired or been revoked.
- [2026-11-12] A compliance outage fails closed (`provider_unavailable` denial, no permission): no answer must never be read as "probably fine".
- [2026-11-12] A successful start yields a `StartAuthorization` that only the flight state machine can create (architecture-tested), and the drone link will require it: the start command cannot be sent by anything that skipped the gate.
- [2026-11-12] A mission is data with a SHA-256 checksum the drone must echo back on upload: the platform can prove the drone holds exactly the mission that was planned, and the cloud never steers (Rule 1).
- [2026-11-12] A mission covers the whole sortie (home -> pickup -> dropoff -> home) and the start command goes out once the order is `assigned`; picked_up, in_flight, arriving and delivered then arrive from telemetry through the state machine.
- [2026-11-12] An order becomes `assigned` only after permission is granted and the mission uploaded (so `assigned` means ready to fly), while the drone is reserved earlier through the flight: orders have no way back, so an order must not be stuck `assigned` with a denied flight.
- [2026-11-12] Flight events go into the one existing append-only audit log (a `flight_id` column, per-event-type state vocabularies in the CHECKs, the same triggers): the spec asks for "an audit log", and splitting it would split the trail.
- [2026-11-12] The audit CHECKs spell out `IS NOT NULL` before `IN (...)`: `x IN (...)` is NULL for a NULL x and a CHECK accepts NULL, which would let an event with no previous state through.
- [2026-11-12] Order and flight statuses share strings such as "cancelled" (they hash equal as StrEnums), so audit events type their states per event type, adapters translate order events only and refuse flight events, and tests pin both: a flight event must never be rendered as an order status.
- [2026-11-12] At most one live flight per drone and per order is enforced by partial unique indexes, drone listing uses `FOR UPDATE SKIP LOCKED`, and status changes are compare-and-set: double-booking is impossible even if every Python check is bypassed.
- [2026-11-12] The flights trigger demands an audit row from the *same transaction* (`recorded_at = now()`), because pause and resume repeat edges and an old audit row must not be reusable; it also makes mission and permission columns write-once so the gate's inputs cannot be edited.
- [2026-11-12] The database gate judges permission validity with the database clock (`clock_timestamp()`), and cannot see the provider re-check: it enforces the data conditions as a backstop, not the whole gate (clock skew is a known risk).
- [2026-11-12] Migration 0002 refuses to downgrade while flight audit events exist and refuses to upgrade while orders reference unregistered drones: the audit log is append-only so a migration will not delete it, and the new foreign key must not silently fail on old data.
- [2026-11-12] Only a mock Digital Sky exists and the config accepts only `mock`: a real integration is not built until explicitly requested.
- [2026-11-12] Drone battery and position are set at registration (and by operations) until telemetry exists, and drone status changes are not audited separately: flight events record the meaningful changes.
- [2026-11-12] Drone selection is a pure function with an explicit policy (minimum battery, range reserve, maximum pickup distance) and deterministic tie-breaks: the same inputs always choose the same drone, and every rejection carries its reasons.
- [2026-11-12] `orders.drone_id` now references the `drones` registry (RESTRICT), delivering the foreign key the Phase 2 decision on plain-UUID drone ids deferred: an order can only be assigned to a registered drone, so tests and fixtures register drones first.
- [2026-11-14] A start that fails after the order is `assigned` fails the order (user-approved): orders have no way back from `assigned`, so an order whose drone never left must end `failed` rather than hang.
- [2026-11-14] `dispatch` starts the flight by default and `start=false` stages it with the mission on board (user-approved): the common case is one call, and staging lets operations hold a flight (and exercises the gate separately).
- [2026-11-14] Cancelling an in-flight order recalls the drone first, and if the recall cannot be confirmed the order is NOT cancelled and the caller gets a retryable error (user-approved): a cancelled order must never leave a drone flying.
- [2026-11-14] `DispatchService` is the only writer of flights and drones and the only caller of the drone link, and it changes orders only through `OrderService`: architecture tests enforce each (Rules 1, 2 and 4), and API handlers may import the service and its errors but never the link.
- [2026-11-14] The drone link has exactly five operations and no parameter that can carry a position, heading, speed or status; `start` requires the `StartAuthorization` from the gate: Rule 1 is a property of the port's shape, checked by signature, by OpenAPI inspection and by a scan for steering-named code.
- [2026-11-14] Link failures are either `CommandRejectedError` (the drone definitely did not act) or `NoAcknowledgementError` (unknown): recovery differs (a refused start frees the drone, an unanswered one recalls it), and a timeout must never be read as "it did not happen".
- [2026-11-14] Start and resume record the intent then send; pause, return-to-home and land send then record: a lost start must fail safe (abort and recall), while a lost stop must not leave a record claiming the drone is still flying. An unconfirmed stop records nothing and is retryable.
- [2026-11-14] Pause, return-to-home, land and the cancellation recall never consult the compliance provider, and a structural test proves their code cannot: stopping a drone must never be blocked by paperwork or an outage.
- [2026-11-14] A refused resume puts the flight back to `paused` and leaves the drone and order alone, while an unanswered resume recalls and aborts (found while testing: aborting after a refusal would have marked a hovering drone `available`).
- [2026-11-14] Dispatch answers 200 with an `outcome` for every result the workflow handled, including refusals: each leaves the system consistent and the order retryable, so none is a request error; 404/409 are reserved for an unknown order and an order that cannot be dispatched.
- [2026-11-14] Dispatch claims only the drone it chose, by compare-and-set, and re-chooses when it loses the claim, instead of locking every candidate with `FOR UPDATE SKIP LOCKED` as the 4A store allows: a concurrency test showed the lock-all approach made 12 concurrent dispatches use 1 of 3 drones (each deciding dispatch hid every drone from the rest, which wrongly reported "no drone"). It never double-booked, but it refused orders it could serve. The store option stays; dispatch no longer uses it.
- [2026-11-14] `PERMISSION_VALIDITY_MINUTES` now defaults to 60 and the composition root passes it to the mock provider: a typical sortie is estimated at ~22 minutes and the gate refuses a permission that ends before the mission would, so the old 15-minute default meant no dispatch could ever start.
- [2026-11-14] The planner produces a straight-line, fixed-altitude sortie of five waypoints with a documented duration formula and a 15% margin, with no airspace, terrain or weather modelling: enough for the gate to judge validity against, and honest about what it is not.
- [2026-11-14] A crash between dispatch steps can leak a reservation and there is no reaper; cancelling the order or `docked` is the manual remedy: automatic recovery needs telemetry to know what a drone is really doing, so it belongs with Phase 5.
- [2026-11-14] Failed commands are logged, not audited (no migration 0003): the audit trail records status changes (Rule 5), and a command that changed nothing is in the structured log; auditing attempts would need a new event type.
- [2026-11-14] `docked` and `maintenance` are operational verbs (not status writes) and refuse a drone with a live flight; the drone list has no `status` filter, so the invariant "no endpoint has an input named like `status`" stays absolute and is tested over OpenAPI.
- [2026-11-14] Partner cancellation maps a finished order to a new `CONFLICT` (409) error kind and an unconfirmed recall to `UNAVAILABLE` (503), rendered by every adapter in its own format: partners learn what to do (stop, or retry) without seeing internals.
- [2026-11-14] Only an in-memory drone link exists (`DRONE_LINK=in_memory`): the MQTT link arrives with telemetry in Phase 5, and nothing that would pretend to reach a real drone is built before then.
- [2026-11-14] Dispatch locks the order row while it decides, so a concurrent dispatch and cancel (or ten dispatches) of one order are serialised by the database; the one-live-flight indexes remain the backstop, and a mutation check showed that removing the lock is caught by the concurrent HTTP test.
- [2026-11-14] Phase 5 is split into 5A (MQTT link, drone-reported events, worker, simulator) and 5B (telemetry samples, Redis, WebSockets, heartbeats, reaper) (user-approved): the command and progress path is one reviewable unit and live tracking another.
- [2026-11-14] Order progress comes from drone-reported mission events, never from the cloud inferring it from GPS positions (user-approved): geofence inference is brittle, while an event is idempotent and auditable.
- [2026-11-14] The telemetry ingest runs as its own worker process, exactly one per broker (user-approved): it must not die with an API worker, and MQTT 3.1.1 has no shared subscriptions, so two workers with one identity would take the session from each other.
- [2026-11-14] The platform uses `aiomqtt` with Mosquitto: QoS 1 for commands, acknowledgements and events, a durable session for the worker so events sent while it is away are kept, and acknowledgements on a per-process reply topic named in each command, so any number of API processes can share a broker without receiving each other's answers (user-approved).
- [2026-11-14] A drone fault is an alert only: it is logged as an error and nothing changes and nothing is sent (user-approved): whether the cloud should automatically recall a drone is a safety policy for the user to decide, not a side effect of a log line.
- [2026-11-14] No position history table and no migration in Phase 5 (user-approved): the audit log records status changes (Rule 5), and live position and battery belong in Redis in 5B.
- [2026-11-14] The broker has no authentication or TLS and the dev config says so (user-approved): consistent with not introducing auth until requested; it must not face real drones as it is.
- [2026-11-14] The wire contract is its own `app/protocol` layer rather than part of `app/telemetry`: the link (sends), the ingest (reads) and the simulator (plays the drone) all need it and none should depend on another.
- [2026-11-14] A drone reports four order-progress events (picked up, departed, arriving, delivered) instead of fewer: the order state machine allows one forward step at a time, and replaying two steps from one event would record `arriving` as if it happened at the same instant as `in_flight`.
- [2026-11-14] Events carry a per-flight sequence number: a repeat or older event is dropped before it touches anything, a gap is logged, and an event that does not fit is rejected, never completed by inventing the missing steps: history must not be fabricated to make a state machine happy.
- [2026-11-14] The worker's dispatch service is built with a link and a compliance provider that refuse everything (`DisabledDroneLink`, `DisabledCompliance`), and the ingest has no MQTT client and no store: that process cannot command a drone or write a status even through a bug (Rules 1 and 4), and architecture tests keep it so.
- [2026-11-14] The MQTT link maps "no connection to the broker" to a rejection (the command was definitely not sent) and "published but unacknowledged" or "publish failed part-way" to no-acknowledgement (unknown): the service recovers differently from each, and a timeout must never be read as "it did not happen".
- [2026-11-14] `mission_completed` ends the flight and frees the drone automatically, and `docked` frees a recalled or grounded one; manual `docked` stays as the fallback. A completion with no reported delivery is applied but warned about, and the order is left alone: the platform does not guess an order's fate.
- [2026-11-14] Audit metadata for telemetry-driven changes keeps only the event name, flight id and time, never the drone's free-form detail: a drone is an untrusted source and its text should not be able to bloat or pollute the append-only log.
- [2026-11-14] If recording an event keeps failing past a short retry window (about 8 s), the event is lost: the broker was already acknowledged and aiomqtt offers no manual acknowledgement. Accepted for 5A, documented, and revisited with heartbeats and reconciliation in 5B.
- [2026-11-14] The simulator behaves like a drone that does not trust the cloud (it recomputes the checksum, refuses an expired, mis-addressed or mismatched authorisation) and only ever answers on platform reply topics, whatever a command's `reply_to` says: a test double that is more trusting than a real drone would hide Rule 2 bugs.
