"""A mock Digital Sky for local development and tests. NOT a real integration.

Behaviour is deterministic and fully controllable:

* A route that passes within a no-fly zone's radius is refused (``no_fly_zone``).
* ``close_airspace`` refuses everything until reopened (``airspace_closed``).
* A granted permission lasts ``validity`` (it does not look at the mission's duration, so
  a too-short window is easy to provoke); it then reports ``expired``.
* ``revoke`` makes a permission report ``revoked``.
* ``set_outage`` makes every call raise ``ComplianceUnavailableError``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import UUID, uuid4

from app.compliance.interface import (
    ComplianceUnavailableError,
    PermissionDecision,
    PermissionGranted,
    PermissionRefused,
)
from app.domain.geo import distance_to_segment_m
from app.domain.models import (
    DenialCode,
    FlightPermission,
    Location,
    PermissionDenial,
    PermissionRequest,
    PermissionStatus,
    PermissionValidation,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class NoFlyZone:
    name: str
    center: Location
    radius_m: float

    def __post_init__(self) -> None:
        if self.radius_m <= 0:
            raise ValueError("a no-fly zone needs a positive radius")


class MockDigitalSky:
    issuer = "mock-digital-sky"

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        validity: timedelta = timedelta(minutes=15),
        no_fly_zones: Sequence[NoFlyZone] = (),
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if validity <= timedelta(0):
            raise ValueError("validity must be positive")
        self._clock = clock
        self._validity = validity
        self._zones = tuple(no_fly_zones)
        self._id_factory = id_factory
        self._issued: dict[str, FlightPermission] = {}
        self._revoked: set[str] = set()
        self._outage = False
        self._closed_reason: str | None = None
        self.requests: list[PermissionRequest] = []
        """Every permission request received, oldest first (for tests to inspect)."""

    # --- controls (not part of the provider interface) -----------------------------------

    def set_outage(self, down: bool) -> None:
        self._outage = down

    def close_airspace(self, reason: str | None) -> None:
        """Refuse all requests until called again with ``None``."""
        self._closed_reason = reason

    def revoke(self, permission_id: str) -> None:
        if permission_id not in self._issued:
            raise KeyError(f"unknown permission {permission_id}")
        self._revoked.add(permission_id)

    @property
    def issued(self) -> list[FlightPermission]:
        return list(self._issued.values())

    # --- ComplianceProvider --------------------------------------------------------------

    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        self._raise_if_down()
        self.requests.append(request)

        if self._closed_reason is not None:
            return PermissionRefused(
                PermissionDenial(DenialCode.AIRSPACE_CLOSED, self._closed_reason)
            )
        for zone in self._zones:
            if self._route_enters(request.route, zone):
                return PermissionRefused(
                    PermissionDenial(
                        DenialCode.NO_FLY_ZONE, f"the route enters no-fly zone '{zone.name}'"
                    )
                )

        now = self._clock()
        permission = FlightPermission(
            permission_id=f"MOCK-{self._id_factory().hex.upper()}",
            issuer=self.issuer,
            flight_id=request.flight_id,
            drone_id=request.drone_id,
            issued_at=now,
            valid_from=now,
            valid_until=now + self._validity,
        )
        self._issued[permission.permission_id] = permission
        return PermissionGranted(permission)

    async def validate_permission(self, permission_id: str) -> PermissionValidation:
        self._raise_if_down()
        now = self._clock()
        permission = self._issued.get(permission_id)
        if permission is None:
            return PermissionValidation(permission_id, PermissionStatus.UNKNOWN, now)
        if permission_id in self._revoked:
            return PermissionValidation(permission_id, PermissionStatus.REVOKED, now)
        if now >= permission.valid_until:
            return PermissionValidation(
                permission_id, PermissionStatus.EXPIRED, now, permission.valid_until
            )
        return PermissionValidation(
            permission_id, PermissionStatus.VALID, now, permission.valid_until
        )

    # --- internals -----------------------------------------------------------------------

    def _raise_if_down(self) -> None:
        if self._outage:
            raise ComplianceUnavailableError("the mock compliance provider is unavailable")

    @staticmethod
    def _route_enters(route: Sequence[Location], zone: NoFlyZone) -> bool:
        return any(
            distance_to_segment_m(zone.center, start, end) <= zone.radius_m
            for start, end in pairwise(route)
        )
