"""Channel configuration, outbox delivery and optional modem status receivers."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, Self, TypeAlias, runtime_checkable

if TYPE_CHECKING:
    from phonelet.constants import NotificationMethod
    from phonelet.config import Settings
    from phonelet.contracts import AmiActionSender


# Payloads are persisted JSON dictionaries. Chunked backends add the checkpoint
# key below, so the envelope deliberately permits backend extensions.
NotificationPayload: TypeAlias = dict[str, Any]
# Stored in `outbox.payload` by chunked backends: the number of chunks already acknowledged.
NEXT_CHUNK_KEY = "_next_chunk"
DeliveryCheckpoint: TypeAlias = Callable[[NotificationPayload], Awaitable[None]]


def resume_chunk(payload: NotificationPayload, total: int, backend: str) -> int:
    """Number of chunks already acknowledged for this job, out of `total`."""
    done = payload.get(NEXT_CHUNK_KEY, 0)
    if type(done) is not int or not 0 <= done <= total:
        raise ValueError(f"Invalid {backend} chunk checkpoint")
    return done


class NotificationBackend(Protocol):
    @property
    def backend_id(self) -> str: ...


class DeliveryBackend(NotificationBackend, Protocol):
    async def deliver(
        self, method: str, payload: NotificationPayload, checkpoint: DeliveryCheckpoint,
    ) -> bool: ...


class Notifier(DeliveryBackend, Protocol):
    """A registry entry builds a configured channel, or returns None to disable it.

    secret_settings names Settings fields to redact before any channel is built.
    accepted_methods declares which events create outbox jobs for this channel.
    """
    secret_settings: ClassVar[tuple[str, ...]]
    accepted_methods: frozenset[NotificationMethod]

    @classmethod
    def from_settings(cls, config: Settings, *, ami: AmiActionSender) -> Self | None: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class ModemStatusReceiver(Protocol):
    """Optional channel capability, published each poll independently of the outbox."""

    async def push_modem_sensor(self, status: dict) -> bool: ...
