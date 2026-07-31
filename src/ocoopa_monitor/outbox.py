from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict

from .delivery import DeliveryClient
from .models import utcnow

LOGGER = logging.getLogger(__name__)


class DeliveryOutboxWorker:
    """Deliver durable jobs with bounded exponential backoff."""

    def __init__(self, db, delivery: DeliveryClient):
        self.db = db
        self.delivery = delivery

    def drain(self, limit: int = 10) -> Dict[str, int]:
        stats = {"attempted": 0, "sent": 0, "failed": 0}
        for job in self.db.list_due_deliveries(limit):
            stats["attempted"] += 1
            try:
                public_payload = dict(job["payload"])
                public_payload.pop("_recall_record_ids", None)
                sent_to = self.delivery.send_alert(public_payload)
                if not sent_to:
                    raise RuntimeError("delivery channel is not configured")
                sent_at = utcnow()
                self.db.mark_delivery_sent(job["id"], sent_to, sent_at)
                stats["sent"] += 1
            except Exception as exc:
                attempts = int(job.get("attempt_count") or 0) + 1
                delay_seconds = min(3600, 30 * (2 ** min(attempts - 1, 7)))
                self.db.mark_delivery_failed(
                    job["id"],
                    f"{type(exc).__name__}: {exc}",
                    utcnow() + timedelta(seconds=delay_seconds),
                )
                stats["failed"] += 1
                LOGGER.warning("delivery job %s failed; retry scheduled: %s", job["id"], exc)
        return stats
