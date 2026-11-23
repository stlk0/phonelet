import asyncio
import logging
from collections.abc import Sequence

from phonelet.notifiers.protocol import DeliveryBackend, NotificationPayload

logger = logging.getLogger(__name__)

BATCH_SIZE = 50  # jobs per pass, so one pass cannot hold the loop indefinitely
# Must stay below Database.claim_delivery's lease (DELIVERY_LEASE_SECONDS, 120 s):
# a chunk checkpoint is accepted only while the job's lease is live.
DELIVERY_TIMEOUT_SECONDS = 30
QUEUE_POLL_SECONDS = 1


def backend_id(notifier: DeliveryBackend) -> str:
    value = getattr(notifier, "backend_id", None)
    if not isinstance(value, str):
        raise ValueError("Notifier must declare a stable backend_id")
    return value


async def deliver_pending(db, notifiers: Sequence[DeliveryBackend]) -> None:
    destinations = {backend_id(n): n for n in notifiers}
    if len(destinations) != len(notifiers):
        raise ValueError("Duplicate notifier backend_id")
    for _ in range(BATCH_SIZE):
        job = await db.claim_delivery(tuple(destinations))
        if job is None:
            break
        try:
            async def checkpoint(payload: NotificationPayload) -> None:
                await db.checkpoint_delivery(job, payload)
            notifier = destinations[job["backend"]]
            delivery = notifier.deliver(job["method"], job["payload"], checkpoint)
            success = bool(await asyncio.wait_for(delivery, timeout=DELIVERY_TIMEOUT_SECONDS))
        except Exception:
            # Provider exception text can contain credentials or message
            # contents; the privacy log factory keeps only type and frames.
            logger.warning("Delivery failed: backend=%s job=%s", job["backend"], job["id"],
                           exc_info=True)
            success = False
        await db.finish_delivery(job, success)
        if not success:
            logger.warning("Delivery retained for retry: backend=%s job=%s attempt=%s",
                           job["backend"], job["id"], job["attempts"])


async def delivery_loop(db, notifiers: Sequence[DeliveryBackend]) -> None:
    """Deliver queued outbox jobs, first attempts and retries alike."""
    while True:
        try:
            await deliver_pending(db, notifiers)
        except Exception:
            logger.exception("Notification queue unavailable; will retry")
        await asyncio.sleep(QUEUE_POLL_SECONDS)
