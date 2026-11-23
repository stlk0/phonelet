"""Deliver stored incoming SMS to the SIP phone via AMI MessageSend, one checkpoint per chunk."""
import base64
import logging
import re
from typing import ClassVar, cast

from phonelet.constants import SIP_BACKEND_ID, SIP_DOMAIN_PATTERN, NotificationMethod
from phonelet.contracts import AmiActionSender, AmiMessage
from phonelet.notifiers.protocol import (
    NEXT_CHUNK_KEY, DeliveryCheckpoint, NotificationPayload, resume_chunk,
)


# Asterisk 20 MessageSend uses a 1301-byte decoded buffer and AMI has
# bounded header lines. 600 bytes leave room for Base64 and part labels.
MESSAGE_CHUNK_BYTES = 600
DISPLAY_NAME_MAX_CHARS = 80
DEFAULT_SIP_USER = 'phone'
# A single PJSIP endpoint (no /contact or @host part); its name is the To user.
ENDPOINT_DESTINATION = re.compile(r'pjsip:([A-Za-z0-9_.+-]+)')
logger = logging.getLogger(__name__)


def split_utf8(text: str) -> list[str]:
    """Split text into pieces of at most MESSAGE_CHUNK_BYTES UTF-8 bytes, between characters."""
    result, current, size = [], [], 0
    for char in text:
        width = len(char.encode('utf-8'))
        if size + width > MESSAGE_CHUNK_BYTES:
            result.append(''.join(current))
            current, size = [], 0
        current.append(char)
        size += width
    result.append(''.join(current))
    return result


class SipDelivery:
    backend_id = SIP_BACKEND_ID
    secret_settings: ClassVar[tuple[str, ...]] = ()
    accepted_methods = frozenset({NotificationMethod.SMS})

    @classmethod
    def from_settings(cls, config, *, ami):
        if not config.sip_message_destination:
            logger.info("SIP SMS delivery disabled: PHONELET_SIP_MESSAGE_DESTINATION is empty")
            return None
        domain = config.sip_domain or config.ami_host
        return cls(ami, domain=domain, destination=config.sip_message_destination)

    async def start(self) -> None:
        """AMI lifecycle belongs to the runtime."""

    async def close(self) -> None:
        """AMI lifecycle belongs to the runtime."""

    def __init__(self, ami: AmiActionSender, domain, destination='pjsip:phone'):
        if not SIP_DOMAIN_PATTERN.fullmatch(domain):
            raise ValueError('Invalid SIP message domain')
        if '\r' in destination or '\n' in destination:
            raise ValueError('Invalid SIP message destination')
        self.ami, self.domain, self.destination = ami, domain, destination
        endpoint = ENDPOINT_DESTINATION.fullmatch(destination)
        self.to = f'sip:{endpoint.group(1) if endpoint else DEFAULT_SIP_USER}@{domain}'

    async def deliver(
        self, method: str, payload: NotificationPayload, checkpoint: DeliveryCheckpoint,
    ) -> bool:
        if method != NotificationMethod.SMS:
            raise ValueError('SIP delivery supports SMS only')
        sender = payload['sender']
        sip_user = re.sub(r'[^A-Za-z0-9+_.-]', '', sender) or 'anonymous'
        sip_from = f'sip:{sip_user}@{self.domain}'
        # PJSIP's name-addr parser and printer preserve quoted-pair escapes and UTF-8.
        # Drop controls/line separators and cap before escaping (at most 320 UTF-8 bytes).
        display_name = ''.join(char for char in sender if char.isprintable())[:DISPLAY_NAME_MAX_CHARS].strip()
        if display_name:
            display_name = display_name.replace('\\', '\\\\').replace('"', '\\"')
            sip_from = f'"{display_name}" <{sip_from}>'
        text = payload['text']
        parts = split_utf8(text)
        for index in range(resume_chunk(payload, len(parts), 'SIP'), len(parts)):
            number = index + 1
            body = f'[{number}/{len(parts)}] {parts[index]}' if len(parts) > 1 else parts[index]
            result = await self.ami.send_action({
                'Action': 'MessageSend', 'Destination': self.destination,
                'To': self.to, 'From': sip_from,
                'Base64Body': base64.b64encode(body.encode('utf-8')).decode('ascii'),
            })
            # MessageSend is a single-response action; unlike DongleShowDevices
            # it does not answer with an event list.
            response = cast(AmiMessage, result)
            if str(response.get('Response', '')).lower() != 'success':
                return False
            await checkpoint({**payload, NEXT_CHUNK_KEY: number})
        # AMI acknowledgement means accepted by Asterisk, not read by handset.
        return True
