import httpx
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import ClassVar

from phonelet.constants import NotificationMethod
from phonelet.notifiers.protocol import DeliveryCheckpoint, NotificationPayload

# What each chan_dongle report type says about the SMS (see cdr_events.SMS_REPORT_TYPES).
SMS_FAILURE_OUTCOMES = {
    "i": "was not sent or only partly sent (modem send error)",
    "e": "was not delivered (delivery report)",
    "t": "got no send result from the modem; it may not have been sent",
    "r": "may not have been sent: the driver rejected it (modem not registered, invalid number or message)",
}


def sms_failure_text(recipient: str, reason: str) -> str:
    outcome = SMS_FAILURE_OUTCOMES.get(reason)
    if outcome is None:
        return f"Outgoing SMS to {recipient}: outcome unknown"
    return f"Outgoing SMS to {recipient} {outcome}"


class BaseNotifier(ABC):
    """Optional HTTP base for notification channels (SipDelivery does not use it).

    Provides httpx.AsyncClient lifecycle management and defines
    the notification interface.
    """

    backend_id: str
    secret_settings: ClassVar[tuple[str, ...]] = ()
    accepted_methods = frozenset(NotificationMethod)
    http_timeout_seconds = 10

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.http_timeout_seconds)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @asynccontextmanager
    async def _http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the started client, or a temporary one closed on exit."""
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(timeout=self.http_timeout_seconds) as client:
                yield client

    async def deliver(
        self, method: str, payload: NotificationPayload, checkpoint: DeliveryCheckpoint,
    ) -> bool:
        """Outbox dispatch hook; checkpoint durably stores updated job payload."""
        return await getattr(self, method)(**payload)

    @abstractmethod
    async def notify_call(self, caller: str, status: str, duration: int = 0, note: str = "") -> bool:
        """`note`: an optional short remark, e.g. why an answered call has no recording."""

    @abstractmethod
    async def notify_health(self, message: str) -> bool: ...

    @abstractmethod
    async def notify_network_lost(self, operator: str, signal_rssi: int) -> bool: ...

    @abstractmethod
    async def notify_network_restored(self, operator: str, signal_rssi: int) -> bool: ...

    async def notify_modem_unavailable(self, reason: str) -> bool:
        """Deliver an unavailable modem alert through the backend."""
        return await self.notify_health(f"Modem status unavailable ({reason})")

    async def notify_sms_failed(self, recipient: str, reason: str) -> bool:
        """Deliver a failed outgoing SMS alert through the backend."""
        return await self.notify_health(sms_failure_text(recipient, reason))
