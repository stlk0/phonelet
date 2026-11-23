import logging

from phonelet.constants import ANONYMOUS_SENDER, CALL_ANSWERED, CALL_MISSED, NotificationMethod
from phonelet.logging_privacy import install_log_privacy, safe_error
from phonelet.notifiers.base import BaseNotifier, sms_failure_text
from phonelet.notifiers.protocol import (
    NEXT_CHUNK_KEY, DeliveryCheckpoint, NotificationPayload, resume_chunk,
)

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


TELEGRAM_LIMIT = 4096


def _width(text: str) -> int:
    """Length in UTF-16 code units, as Telegram counts it."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def split_message(text: str, limit: int):
    """Preserve all text and Unicode scalars, within Telegram's UTF-16 budget.

    Counting astral characters twice is conservative for the 4096-character
    limit and also respects Telegram's UTF-16 entity indexing. No parse_mode
    is used, so punctuation and markup remain literal text.
    """
    if limit < 2:
        raise ValueError("limit must be at least 2")
    start = units = 0
    for index, char in enumerate(text):
        width = _width(char)
        if units + width > limit:
            yield text[start:index]
            start, units = index, 0
        units += width
    if start < len(text):
        yield text[start:]


def sms_chunks(sender: str, text: str) -> list[str]:
    """SMS text in chunks within TELEGRAM_LIMIT, each headed with the sender and its part number.

    The split depends only on the arguments, so a resume checkpoint stays valid.
    The body is split with room for the widest header of the resulting count.
    """
    single = f"SMS from {sender}:\n{text}"
    if _width(single) <= TELEGRAM_LIMIT:
        return [single]
    total = 2
    while True:
        header_width = _width(f"SMS from {sender} ({total}/{total}):\n")
        parts = list(split_message(text, TELEGRAM_LIMIT - header_width))
        if len(str(len(parts))) <= len(str(total)):
            break
        total = len(parts)
    return [f"SMS from {sender} ({number}/{len(parts)}):\n{part}" for number, part in enumerate(parts, 1)]


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs}s"


class TelegramNotifier(BaseNotifier):
    backend_id = "telegram"
    secret_settings = ("telegram_token",)

    @classmethod
    def from_settings(cls, config, *, ami):
        if config.telegram_token and config.telegram_chat_id:
            return cls(config.telegram_token, config.telegram_chat_id)
        return None

    def __init__(self, token: str, chat_id: str):
        super().__init__()
        install_log_privacy(secrets=(token,))
        self._chat_id = chat_id
        self._url = f"{TELEGRAM_API}/bot{token}/sendMessage"

    async def _send(self, text: str) -> bool:
        async with self._http_client() as client:
            try:
                resp = await client.post(
                    self._url,
                    json={"chat_id": self._chat_id, "text": text,
                          # Previews could consume one-time links received in SMS.
                          "link_preview_options": {"is_disabled": True}},
                )
                resp.raise_for_status()
                if resp.json().get("ok") is not True:
                    logger.error("Telegram send failed: API rejected message")
                    return False
                return True
            except Exception as e:
                logger.error("Telegram send failed: %s", safe_error(e))
                return False

    async def deliver(
        self, method: str, payload: NotificationPayload, checkpoint: DeliveryCheckpoint,
    ) -> bool:
        if method != NotificationMethod.SMS:
            return await super().deliver(method, payload, checkpoint)
        chunks = sms_chunks(payload["sender"], payload["text"])
        for index in range(resume_chunk(payload, len(chunks), "Telegram"), len(chunks)):
            if not await self._send(chunks[index]):
                return False
            # Persist only after the remote endpoint acknowledges this chunk.
            await checkpoint({**payload, NEXT_CHUNK_KEY: index + 1})
        return True

    async def notify_call(self, caller: str, status: str, duration: int = 0, note: str = "") -> bool:
        display = "anonymous number" if caller == ANONYMOUS_SENDER else caller
        if status == CALL_MISSED:
            msg = f"Missed call from {display}"
        elif status == CALL_ANSWERED:
            msg = f"Call from {display} — {format_duration(duration)}"
        else:
            msg = f"Call from {display} — status unknown"
        if note:
            msg += f" ({note})"
        return await self._send(msg)

    async def notify_health(self, message: str) -> bool:
        msg = f"Alert:\n{message}"
        return await self._send(msg)

    async def notify_network_lost(self, operator: str, signal_rssi: int) -> bool:
        op = operator or "unknown"
        return await self._send(f"SIM network lost (was: {op}, signal rssi: {signal_rssi})")

    async def notify_network_restored(self, operator: str, signal_rssi: int) -> bool:
        op = operator or "unknown"
        return await self._send(f"SIM network restored ({op}, signal rssi: {signal_rssi})")

    async def notify_modem_unavailable(self, reason: str) -> bool:
        return await self._send(f"Modem status unavailable ({reason})")

    async def notify_sms_failed(self, recipient: str, reason: str) -> bool:
        return await self._send(sms_failure_text(recipient, reason))
