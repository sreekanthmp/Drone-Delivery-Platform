"""Tell live watchers about changes that were just committed.

Implements the services' ``AuditNotifier`` port. It receives the audit events a service has
*already committed* and publishes a small summary of each to the order's channel and to the
fleet channel. It is best-effort by construction: it never raises, so a failure here cannot
fail or roll back the change it reports. A watcher that misses one re-syncs from a snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from app.domain.models import AuditEvent
from app.realtime import channels
from app.realtime.models import Envelope
from app.realtime.ports import Broadcast

logger = structlog.get_logger(__name__)


def audit_envelope(event: AuditEvent) -> Envelope:
    return Envelope(
        kind="audit",
        droppable=False,
        data={
            "event_type": event.event_type.value,
            "order_id": str(event.order_id),
            "flight_id": None if event.flight_id is None else str(event.flight_id),
            "drone_id": None if event.drone_id is None else str(event.drone_id),
            "previous_state": None if event.previous_state is None else event.previous_state.value,
            "new_state": None if event.new_state is None else event.new_state.value,
            "occurred_at": event.occurred_at.isoformat(),
            "source": event.source.value,
        },
    )


class AuditBroadcaster:
    def __init__(self, broadcast: Broadcast) -> None:
        self._broadcast = broadcast

    async def notify(self, events: Sequence[AuditEvent]) -> None:
        for event in events:
            try:
                envelope = audit_envelope(event)
                await self._broadcast.publish(channels.order_channel(event.order_id), envelope)
                await self._broadcast.publish(channels.FLEET, envelope)
            except Exception:
                logger.exception("notify_failed", event_id=str(event.event_id))
