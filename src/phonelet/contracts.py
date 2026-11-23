"""Callback contracts for records parsed from the CDR journal, the AMI action types and the
driver's SMS journal capability.

AMI headers remain mappings (ami_client.Message looks keys up ignoring case),
and event-list actions return a sequence of messages. A replayed CDR journal
row is a mapping of the same shape. Parsed records describe the keyword
arguments accepted by the persistence callbacks.
"""
from collections.abc import Awaitable, Mapping
from datetime import datetime, timezone
from typing import NotRequired, Protocol, TypeAlias, TypedDict, Unpack

AmiMessage: TypeAlias = Mapping[str, str]
AmiAction: TypeAlias = Mapping[str, str]
AmiResult: TypeAlias = AmiMessage | list[AmiMessage] | tuple[AmiMessage, ...]
CdrRow: TypeAlias = Mapping[str, str]  # one CDR journal row, field name to value

DEVICE_LIST_ACTION = "DongleShowDevices"
# chan_dongle_ingress.patch (manager.c): each device entry of DEVICE_LIST_ACTION carries
# SMSJournalVersion and SMSJournalStatus. Version 1 is the only journal format ingress replays.
SMS_JOURNAL_VERSION = "1"
SMS_JOURNAL_OK = "ok"


def sms_journal_ok(version, status) -> bool:
    return version == SMS_JOURNAL_VERSION and status == SMS_JOURNAL_OK


_DATE_TIME_LEN = len('YYYY-MM-DD HH:MM:SS')  # date and time before an optional offset


def parse_timestamp(value):
    # Driver journal (pdu.c): 'YYYY-MM-DD HH:MM:SS +HH:MM'; the space before the
    # offset is dropped for fromisoformat. Also the CDR StartTime parser
    # (cdr_events): a value without an offset is read as UTC.
    value = value.strip()
    if len(value) > _DATE_TIME_LEN and value[_DATE_TIME_LEN] == ' ':
        value = value[:_DATE_TIME_LEN] + value[_DATE_TIME_LEN + 1:]
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class MalformedCdr(ValueError):
    """A CDR journal row whose fields can never describe a call or an SMS report.

    Raised only from parsing, never from storing, so that ingress sets the row
    aside instead of retrying it, while a database failure is still retried.
    """


class CallEnded(TypedDict):
    source_id: str
    timestamp: str
    caller: str
    status: str
    duration: int
    recording_path: str | None
    history: NotRequired[bool]


class SmsFailed(TypedDict):
    event_key: str
    recipient: str
    reason: str
    history: NotRequired[bool]  # only records in the first replay inventory


class CallReceiver(Protocol):
    def __call__(self, **event: Unpack[CallEnded]) -> Awaitable[None]: ...


class SmsFailureReceiver(Protocol):
    # Resolves to True when the alert is new, False when a replay finds it already queued.
    def __call__(self, **event: Unpack[SmsFailed]) -> Awaitable[bool]: ...


class AmiActionSender(Protocol):
    async def send_action(self, action: AmiAction) -> AmiResult: ...
