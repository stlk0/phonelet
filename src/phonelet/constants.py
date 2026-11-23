import re
from enum import StrEnum

# Call statuses (stored in DB `calls.status` column)
CALL_ANSWERED = "answered"
CALL_MISSED = "missed"
CALL_REJECTED = "rejected"

ANONYMOUS_SENDER = "anonymous"

# Network registration status (AT+CREG? <stat> values)
NET_NOT_REGISTERED = 0
NET_REGISTERED_HOME = 1
NET_SEARCHING = 2
NET_DENIED = 3
NET_UNKNOWN = 4
NET_REGISTERED_ROAMING = 5

# AT+CSQ <rssi> 'not known or not detectable'
RSSI_UNKNOWN = 99

# UDH concatenation counts the parts of one SMS in one octet.
MAX_SMS_PARTS = 255

# The single supported chan_dongle device; templates use the same literal (checked by tests).
DEFAULT_DONGLE_DEVICE = "dongle0"

# Backend id of incoming SMS delivery to the SIP phone (stored in `outbox.backend`)
SIP_BACKEND_ID = "sip"

# Host part of the sip: URIs that SipDelivery builds (PHONELET_SIP_DOMAIN).
SIP_DOMAIN_PATTERN = re.compile(r"[A-Za-z0-9.:-]+")


class RecordingState(StrEnum):
    """Stored in DB `calls.recording_state`. PENDING, READY and FAILED are the `status` values
    phonelet.recording accepts in <mp3>.status.json; scripts/convert-recording.py writes READY and FAILED.
    DELETED: the retention removed the files (PHONELET_RECORDING_RETENTION_DAYS); never reconciled again."""
    NONE = "none"
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class NotificationMethod(StrEnum):
    """Stored in `outbox.method`; delivery calls the notifier method of this name."""
    SMS = "notify_sms"
    CALL = "notify_call"
    HEALTH = "notify_health"
    NETWORK_LOST = "notify_network_lost"
    NETWORK_RESTORED = "notify_network_restored"
    MODEM_UNAVAILABLE = "notify_modem_unavailable"
    SMS_FAILED = "notify_sms_failed"


class Availability(StrEnum):
    """The monitor's current modem availability, published in /api/modem, /api/stats and the HA sensor."""
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
