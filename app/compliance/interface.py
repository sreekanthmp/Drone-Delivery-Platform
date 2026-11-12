"""The compliance provider interface.

Two questions, and only two:

* ``request_permission``: may this drone fly this route? (granted, or refused with a reason)
* ``validate_permission``: is a permission we hold still in force *right now*?

Dispatch asks the first when planning a flight and the second immediately before every
start or resume, because a permission can expire or be revoked in between (Rule 2).

**Fail closed.** If the provider cannot be reached it raises
:class:`ComplianceUnavailableError`; callers must treat that as "no permission", never as
"probably fine".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.models import (
    FlightPermission,
    PermissionDenial,
    PermissionRequest,
    PermissionValidation,
)


class ComplianceUnavailableError(Exception):
    """The compliance provider could not be reached or did not answer."""


@dataclass(frozen=True, slots=True)
class PermissionGranted:
    permission: FlightPermission


@dataclass(frozen=True, slots=True)
class PermissionRefused:
    denial: PermissionDenial


PermissionDecision = PermissionGranted | PermissionRefused


class ComplianceProvider(Protocol):
    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        """Ask for permission to fly ``request.route``. Raises ``ComplianceUnavailableError``."""
        ...

    async def validate_permission(self, permission_id: str) -> PermissionValidation:
        """The provider's current answer about a permission, stamped with the platform's clock.

        Raises ``ComplianceUnavailableError``.
        """
        ...
