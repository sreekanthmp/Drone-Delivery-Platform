"""The mock Digital Sky, on its own and feeding the real flight state machine."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from app.compliance.interface import (
    ComplianceUnavailableError,
    PermissionGranted,
    PermissionRefused,
)
from app.compliance.mock_digital_sky import MockDigitalSky, NoFlyZone
from app.domain.exceptions import (
    PermissionExpiredError,
    PermissionRevokedError,
    PermissionWindowTooShortError,
)
from app.domain.flight_state_machine import FlightStateMachine
from app.domain.models import (
    DenialCode,
    EventSource,
    FlightStatus,
    Location,
    PermissionRequest,
    PermissionStatus,
)
from tests.conftest import (
    DRONE_ID,
    DROPOFF,
    FLIGHT_ID,
    HOME,
    ORDER_ID,
    PICKUP,
    T0,
    FakeClock,
    SequentialIds,
    build_flight,
    build_mission,
)

S = FlightStatus
MIDPOINT = Location(
    latitude=(PICKUP.latitude + DROPOFF.latitude) / 2,
    longitude=(PICKUP.longitude + DROPOFF.longitude) / 2,
)


def request(**overrides: Any) -> PermissionRequest:
    fields: dict[str, Any] = {
        "flight_id": FLIGHT_ID,
        "order_id": ORDER_ID,
        "drone_id": DRONE_ID,
        "route": (HOME, PICKUP, DROPOFF, HOME),
        "requested_start": T0,
        "estimated_duration_s": 600.0,
    }
    fields.update(overrides)
    return PermissionRequest(**fields)


@pytest.fixture
def sky(clock: FakeClock, ids: SequentialIds) -> MockDigitalSky:
    return MockDigitalSky(clock=clock, id_factory=ids)


class TestGranting:
    async def test_grants_a_permission_for_the_requested_flight(self, sky: MockDigitalSky) -> None:
        decision = await sky.request_permission(request())

        assert isinstance(decision, PermissionGranted)
        permission = decision.permission
        assert (permission.flight_id, permission.drone_id) == (FLIGHT_ID, DRONE_ID)
        assert permission.issuer == "mock-digital-sky"
        assert permission.issued_at == permission.valid_from == T0
        assert permission.valid_until == T0 + timedelta(minutes=15)

    async def test_the_validity_is_configurable(self, clock: FakeClock) -> None:
        sky = MockDigitalSky(clock=clock, validity=timedelta(minutes=3))
        decision = await sky.request_permission(request())

        assert isinstance(decision, PermissionGranted)
        assert decision.permission.valid_until == T0 + timedelta(minutes=3)

    async def test_every_permission_has_its_own_id(self, sky: MockDigitalSky) -> None:
        ids = {
            (await sky.request_permission(request())).permission.permission_id  # type: ignore[union-attr]
            for _ in range(5)
        }
        assert len(ids) == 5

    async def test_ids_look_like_permission_numbers(self, sky: MockDigitalSky) -> None:
        decision = await sky.request_permission(request())
        assert isinstance(decision, PermissionGranted)
        assert decision.permission.permission_id.startswith("MOCK-")

    async def test_requests_are_recorded_for_inspection(self, sky: MockDigitalSky) -> None:
        first, second = request(), request(flight_id=UUID(int=2))

        await sky.request_permission(first)
        await sky.request_permission(second)

        assert sky.requests == [first, second]

    async def test_issued_permissions_are_listed(self, sky: MockDigitalSky) -> None:
        await sky.request_permission(request())
        assert len(sky.issued) == 1

    def test_a_non_positive_validity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="validity"):
            MockDigitalSky(validity=timedelta(0))


class TestNoFlyZones:
    ZONE = NoFlyZone("airport", MIDPOINT, radius_m=300)

    async def test_a_route_through_a_zone_is_refused_with_the_zone_named(
        self, clock: FakeClock
    ) -> None:
        sky = MockDigitalSky(clock=clock, no_fly_zones=[self.ZONE])

        decision = await sky.request_permission(request())

        assert isinstance(decision, PermissionRefused)
        assert decision.denial.code is DenialCode.NO_FLY_ZONE
        assert "airport" in decision.denial.reason
        assert sky.issued == []

    async def test_a_route_that_crosses_a_zone_between_waypoints_is_refused(
        self, clock: FakeClock
    ) -> None:
        # Both endpoints are far outside the zone, but the straight leg passes through it.
        zone = NoFlyZone("crossing", Location(0.0, 0.5), radius_m=500)
        sky = MockDigitalSky(clock=clock, no_fly_zones=[zone])

        decision = await sky.request_permission(
            request(route=(Location(0.0, 0.0), Location(0.0, 1.0)))
        )

        assert isinstance(decision, PermissionRefused)

    async def test_a_route_passing_outside_the_radius_is_granted(self, clock: FakeClock) -> None:
        zone = NoFlyZone("far away", Location(latitude=12.0, longitude=77.0), radius_m=300)
        sky = MockDigitalSky(clock=clock, no_fly_zones=[zone])

        assert isinstance(await sky.request_permission(request()), PermissionGranted)

    async def test_just_outside_and_just_inside_the_radius(self, clock: FakeClock) -> None:
        route = (Location(0.0, 0.0), Location(0.0, 1.0))  # an equatorial leg
        centre = Location(0.01, 0.5)  # ~1112 m north of the leg
        outside = MockDigitalSky(clock=clock, no_fly_zones=[NoFlyZone("z", centre, 1_000)])
        inside = MockDigitalSky(clock=clock, no_fly_zones=[NoFlyZone("z", centre, 1_200)])

        assert isinstance(await outside.request_permission(request(route=route)), PermissionGranted)
        assert isinstance(await inside.request_permission(request(route=route)), PermissionRefused)

    async def test_any_zone_is_enough(self, clock: FakeClock) -> None:
        elsewhere = NoFlyZone("elsewhere", Location(0.0, 0.0), 100)
        sky = MockDigitalSky(clock=clock, no_fly_zones=[elsewhere, self.ZONE])

        assert isinstance(await sky.request_permission(request()), PermissionRefused)

    def test_a_zone_needs_a_positive_radius(self) -> None:
        with pytest.raises(ValueError, match="radius"):
            NoFlyZone("x", HOME, 0)


class TestClosedAirspace:
    async def test_everything_is_refused_until_it_reopens(self, sky: MockDigitalSky) -> None:
        sky.close_airspace("national event")

        refused = await sky.request_permission(request())
        assert isinstance(refused, PermissionRefused)
        assert refused.denial.code is DenialCode.AIRSPACE_CLOSED
        assert refused.denial.reason == "national event"

        sky.close_airspace(None)
        assert isinstance(await sky.request_permission(request()), PermissionGranted)


class TestValidation:
    async def grant(self, sky: MockDigitalSky) -> str:
        decision = await sky.request_permission(request())
        assert isinstance(decision, PermissionGranted)
        return decision.permission.permission_id

    async def test_a_fresh_permission_is_valid_and_reports_when_it_ends(
        self, sky: MockDigitalSky
    ) -> None:
        permission_id = await self.grant(sky)

        validation = await sky.validate_permission(permission_id)

        assert validation.status is PermissionStatus.VALID
        assert validation.valid_until == T0 + timedelta(minutes=15)
        assert validation.permission_id == permission_id

    async def test_the_answer_is_stamped_with_the_platforms_clock(
        self, sky: MockDigitalSky, clock: FakeClock
    ) -> None:
        permission_id = await self.grant(sky)
        clock.advance(minutes=4)

        assert (await sky.validate_permission(permission_id)).checked_at == clock.now

    async def test_it_expires_when_the_validity_ends(
        self, sky: MockDigitalSky, clock: FakeClock
    ) -> None:
        permission_id = await self.grant(sky)

        clock.advance(minutes=14, seconds=59)
        assert (await sky.validate_permission(permission_id)).status is PermissionStatus.VALID
        clock.advance(seconds=1)
        assert (await sky.validate_permission(permission_id)).status is PermissionStatus.EXPIRED

    async def test_a_revoked_permission_says_so_even_before_it_expires(
        self, sky: MockDigitalSky
    ) -> None:
        permission_id = await self.grant(sky)

        sky.revoke(permission_id)

        assert (await sky.validate_permission(permission_id)).status is PermissionStatus.REVOKED

    async def test_an_unknown_permission_is_unknown(self, sky: MockDigitalSky) -> None:
        validation = await sky.validate_permission("MOCK-NOPE")
        assert validation.status is PermissionStatus.UNKNOWN
        assert validation.valid_until is None

    def test_revoking_an_unknown_permission_is_an_error(self, sky: MockDigitalSky) -> None:
        with pytest.raises(KeyError):
            sky.revoke("MOCK-NOPE")


class TestOutage:
    async def test_every_call_raises_while_it_is_down(self, sky: MockDigitalSky) -> None:
        sky.set_outage(True)

        with pytest.raises(ComplianceUnavailableError):
            await sky.request_permission(request())
        with pytest.raises(ComplianceUnavailableError):
            await sky.validate_permission("anything")

    async def test_nothing_is_recorded_or_issued_during_an_outage(
        self, sky: MockDigitalSky
    ) -> None:
        sky.set_outage(True)
        with pytest.raises(ComplianceUnavailableError):
            await sky.request_permission(request())

        assert sky.requests == []
        assert sky.issued == []

    async def test_it_recovers(self, sky: MockDigitalSky) -> None:
        sky.set_outage(True)
        sky.set_outage(False)
        assert isinstance(await sky.request_permission(request()), PermissionGranted)


class TestFeedingTheRealGate:
    """The mock and the flight state machine together: Rule 2 end to end, without a database."""

    async def flight_ready_to_start(
        self, sky: MockDigitalSky, machine: FlightStateMachine, mission_s: float = 600.0
    ) -> Any:
        mission = build_mission(estimated_duration_s=mission_s)
        planned = machine.create(
            order_id=ORDER_ID, drone_id=DRONE_ID, mission=mission, source=EventSource.DISPATCH
        ).flight
        decision = await sky.request_permission(
            PermissionRequest.from_mission(
                flight_id=planned.id,
                order_id=ORDER_ID,
                drone_id=DRONE_ID,
                mission=mission,
                requested_start=T0,
            )
        )
        assert isinstance(decision, PermissionGranted)
        granted = machine.transition(
            planned,
            S.PERMISSION_GRANTED,
            source=EventSource.DISPATCH,
            permission=decision.permission,
        ).flight
        return machine.transition(
            granted,
            S.MISSION_UPLOADED,
            source=EventSource.DISPATCH,
            acknowledged_checksum=mission.checksum,
        ).flight

    async def start(self, sky: MockDigitalSky, machine: FlightStateMachine, flight: Any) -> Any:
        validation = await sky.validate_permission(flight.permission.permission_id)
        return machine.transition(
            flight, S.STARTED, source=EventSource.DISPATCH, validation=validation
        )

    async def test_a_granted_permission_lets_the_flight_start(
        self, sky: MockDigitalSky, flight_machine: FlightStateMachine
    ) -> None:
        flight = await self.flight_ready_to_start(sky, flight_machine)

        result = await self.start(sky, flight_machine, flight)

        assert result.flight.status is S.STARTED
        assert result.start_authorization is not None

    async def test_a_revocation_between_grant_and_start_blocks_the_start(
        self, sky: MockDigitalSky, flight_machine: FlightStateMachine
    ) -> None:
        flight = await self.flight_ready_to_start(sky, flight_machine)
        sky.revoke(flight.permission.permission_id)

        with pytest.raises(PermissionRevokedError):
            await self.start(sky, flight_machine, flight)

    async def test_an_expiry_between_grant_and_start_blocks_the_start(
        self, sky: MockDigitalSky, flight_machine: FlightStateMachine, clock: FakeClock
    ) -> None:
        flight = await self.flight_ready_to_start(sky, flight_machine)
        clock.advance(minutes=16)

        with pytest.raises(PermissionExpiredError):
            await self.start(sky, flight_machine, flight)

    async def test_a_mission_longer_than_the_permission_cannot_start(
        self, sky: MockDigitalSky, flight_machine: FlightStateMachine
    ) -> None:
        # The mock grants 15 minutes whatever the mission needs; the gate catches the mismatch.
        flight = await self.flight_ready_to_start(sky, flight_machine, mission_s=1800.0)

        with pytest.raises(PermissionWindowTooShortError):
            await self.start(sky, flight_machine, flight)

    async def test_a_flight_planned_through_a_no_fly_zone_never_gets_a_permission(
        self, clock: FakeClock, ids: SequentialIds, flight_machine: FlightStateMachine
    ) -> None:
        sky = MockDigitalSky(
            clock=clock, id_factory=ids, no_fly_zones=[NoFlyZone("airport", MIDPOINT, 300)]
        )
        mission = build_mission()
        planned = build_flight(S.PLANNED, mission=mission)

        decision = await sky.request_permission(
            PermissionRequest.from_mission(
                flight_id=planned.id,
                order_id=ORDER_ID,
                drone_id=DRONE_ID,
                mission=mission,
                requested_start=T0,
            )
        )

        assert isinstance(decision, PermissionRefused)
        denied = flight_machine.transition(
            planned,
            S.PERMISSION_DENIED,
            source=EventSource.DISPATCH,
            denial=decision.denial,
        )
        assert denied.flight.status is S.PERMISSION_DENIED
        assert not flight_machine.can_transition(S.PERMISSION_DENIED, S.STARTED)
