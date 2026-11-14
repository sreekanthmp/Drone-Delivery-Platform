"""Where a drone is, as shown to someone tracking an order.

Deliberately nothing else: no drone id, no battery, no flight. What a partner may see of a
delivery is decided by what this type can carry, so a renderer cannot leak what it never had.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.domain.exceptions import TrackingValidationError
from app.domain.models.order import Location


@dataclass(frozen=True, slots=True)
class TrackingPosition:
    location: Location
    altitude_m: float
    observed_at: datetime

    def __post_init__(self) -> None:
        if not math.isfinite(self.altitude_m) or self.altitude_m < 0:
            raise TrackingValidationError(f"altitude_m must not be negative, got {self.altitude_m}")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise TrackingValidationError("observed_at must be timezone-aware")
