"""A compliance provider that is not there: for processes that must never ask for permission.

The telemetry worker records what drones report; it never starts a flight, so it has no
business with the compliance provider. Giving it this one means any attempt fails closed.
"""

from __future__ import annotations

from app.compliance.interface import ComplianceUnavailableError, PermissionDecision
from app.domain.models import PermissionRequest, PermissionValidation


class DisabledCompliance:
    async def request_permission(self, request: PermissionRequest) -> PermissionDecision:
        raise ComplianceUnavailableError("this process does not talk to the compliance provider")

    async def validate_permission(self, permission_id: str) -> PermissionValidation:
        raise ComplianceUnavailableError("this process does not talk to the compliance provider")
