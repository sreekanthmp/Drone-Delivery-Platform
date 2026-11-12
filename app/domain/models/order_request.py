"""The platform's internal description of "please deliver this" (Rule 3).

Adapters translate a partner's payload into an :class:`OrderRequest`; nothing
partner-specific survives past that point. The order service turns a request
into an :class:`~app.domain.models.order.Order` via the state machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models.order import Location, Package


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_id: str
    """Which integration the order came from (the adapter's ``client_id``)."""

    external_order_id: str
    """The partner's own identifier for the order. Together with ``client_id`` it is
    the idempotency key for order intake."""

    pickup: Location
    dropoff: Location
    package: Package
