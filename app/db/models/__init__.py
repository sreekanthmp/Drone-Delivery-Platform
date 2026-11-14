"""SQLAlchemy table definitions.

``OrderRecord``, ``AuditEventRecord``, ``DroneRecord``, ``FlightRecord`` and the webhook
tables are referenced only by the repositories in ``app/db/repositories`` (enforced by
``tests/unit/test_no_direct_status_writes.py``).
"""

from app.db.models.audit_event import AuditEventRecord
from app.db.models.drone import DroneRecord
from app.db.models.flight import FlightRecord
from app.db.models.order import OrderRecord
from app.db.models.webhook import WebhookAttemptRecord, WebhookDeliveryRecord

__all__ = [
    "AuditEventRecord",
    "DroneRecord",
    "FlightRecord",
    "OrderRecord",
    "WebhookAttemptRecord",
    "WebhookDeliveryRecord",
]
