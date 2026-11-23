import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from phonelet.constants import (
    Availability,
    DEFAULT_DONGLE_DEVICE,
    NET_DENIED,
    NET_NOT_REGISTERED,
    NET_REGISTERED_HOME,
    NET_REGISTERED_ROAMING,
    NET_SEARCHING,
    NET_UNKNOWN,
    RSSI_UNKNOWN,
    NotificationMethod,
)
from phonelet.contracts import DEVICE_LIST_ACTION, SMS_JOURNAL_OK, AmiMessage
from phonelet.notifiers.protocol import ModemStatusReceiver

logger = logging.getLogger(__name__)

# --- DongleShowDevices parsers ---

_GSM_STATUS_MAP = {
    "Not registered": NET_NOT_REGISTERED,
    "Not registered, but searching": NET_SEARCHING,
    "Registered, home network": NET_REGISTERED_HOME,
    "Registered, roaming": NET_REGISTERED_ROAMING,
    "Registration denied": NET_DENIED,
    "Unknown": NET_UNKNOWN,
    "Registered, home network (SMS only)": NET_REGISTERED_HOME,
    "Registered, roaming (SMS only)": NET_REGISTERED_ROAMING,
}

_RSSI_RE = re.compile(r"(\d+),\s*(?:[<>]=\s*)?(-?\d+)\s*dBm")
_RSSI_MAX = 31  # highest CSQ <rssi> signal level


def parse_device_entry(entry: AmiMessage) -> "ModemStatus":
    """Parse a DongleDeviceEntry event into ModemStatus."""
    reg_status = _GSM_STATUS_MAP.get(entry.get("GSMRegistrationStatus"), NET_UNKNOWN)

    # RSSI: "12, -89 dBm"
    m = _RSSI_RE.search(entry.get("RSSI", ""))
    rssi = int(m.group(1)) if m else RSSI_UNKNOWN

    operator = entry.get("ProviderName", "")

    return ModemStatus(
        registration_status=reg_status,
        signal_rssi=rssi,
        operator=operator,
        last_updated=datetime.now(timezone.utc),
    )


def rssi_to_dbm(rssi: int) -> int | None:
    """Convert a CSQ rssi value to dBm; None when unknown or out of range."""
    if not 0 <= rssi <= _RSSI_MAX:
        return None
    return -113 + 2 * rssi


# --- Modem status dataclass ---


@dataclass
class ModemStatus:
    registration_status: int = NET_UNKNOWN
    signal_rssi: int = RSSI_UNKNOWN
    operator: str = ""
    last_updated: datetime | None = None

    @property
    def network_registered(self) -> bool | None:
        if self.registration_status == NET_UNKNOWN:
            return None
        return self.registration_status in (NET_REGISTERED_HOME, NET_REGISTERED_ROAMING)

    @property
    def roaming(self) -> bool:
        return self.registration_status == NET_REGISTERED_ROAMING

    @property
    def signal_dbm(self) -> int | None:
        return rssi_to_dbm(self.signal_rssi)

    def to_dict(self) -> dict:
        return {
            "network_registered": self.network_registered,
            "registration_status": self.registration_status,
            "roaming": self.roaming,
            "signal_rssi": self.signal_rssi,
            "signal_dbm": self.signal_dbm,
            "operator": self.operator,
            "modem_status_updated": (
                self.last_updated.isoformat() if self.last_updated else None
            ),
        }


# --- Monitor ---

# Longer than AmiClient's action_timeout (10 s): the client's expiry ends a stuck poll and starts a
# fresh AMI session; this one only bounds a client that never answers at all.
_CMD_TIMEOUT = 15.0
_DELIVERY_TIMEOUT = 10.0
_LOGIN_WAIT_STEP = 0.1  # seconds between AMI login checks before the first poll
# Answered polls in a row with SMSJournalStatus other than ok before one HEALTH alert;
# the driver retries its journal every 30 s, so a single error poll is not yet an outage.
SMS_JOURNAL_ALERT_POLLS = 3


class DeviceMissing(ValueError):
    pass


class DeviceNotConnected(ValueError):
    pass


def _ami_down_reason(connected: bool, authenticated: bool) -> str | None:
    if not connected:
        return "ami_disconnected"
    if not authenticated:
        return "ami_unauthenticated"
    return None


def _poll_failure_reason(error: Exception) -> str:
    if isinstance(error, DeviceMissing):
        return "device_missing"
    if isinstance(error, DeviceNotConnected):
        return "device_not_connected"
    if isinstance(error, TimeoutError):
        return "poll_timeout"
    return "poll_failed"


class ModemStatusMonitor:
    """One polling authority for availability, readiness, samples and alerts.

    AMI must expose ``connected`` (transport) and ``authenticated`` (login).
    ``alert_sink(method, payload, state)`` must return only once durably queued; an
    exception or False leaves the alert pending. Without a sink, alerts are not queued.
    ``state`` is the announced (availability, registered) of a state-change alert, None
    for other alerts: the sink stores it with the alert, and ``load_alert_state()``
    returns the last stored one (or None) when ``run`` starts, so a restart neither
    repeats an announced state nor misses a change that happened while it was down.
    Status receivers get every snapshot with their own timeout; a failed push is
    retried on the next poll and does not stop other receivers or durable alerts.
    """

    def __init__(
        self, ami_client, *,
        alert_sink: Callable[[str, dict, tuple | None], Awaitable[object]] | None = None,
        load_alert_state: Callable[[], Awaitable[tuple | None]] | None = None,
        status_receivers: Sequence[ModemStatusReceiver] = (),
        poll_interval: int = 30,
    ):
        if poll_interval <= 0:
            raise ValueError("monitor poll interval must be positive")
        self._ami = ami_client
        self._status_receivers = tuple(status_receivers)
        self._poll_interval = poll_interval
        self._alert_sink = alert_sink
        self._load_alert_state = load_alert_state
        self._stale_after = max(2 * poll_interval, poll_interval + _CMD_TIMEOUT)
        self._status = ModemStatus()
        self._sample_time: float | None = None
        self._availability = Availability.UNKNOWN
        self._reason = "not_sampled"
        self._device_present: bool | None = None
        self._alerted_state: tuple | None = None
        self._pending_alerts: list[tuple[str, dict, tuple | None]] = []
        self._journal_failed_polls = 0  # answered polls in a row with the driver journal not ok
        self._refresh_lock = asyncio.Lock()
        # SMSJournalVersion/SMSJournalStatus of this device in the last answered poll; {} without its entry.
        self.driver_ingress: dict = {}

    def _ami_link(self) -> tuple[bool, bool]:
        connected = self._ami.connected is True
        return connected, connected and self._ami.authenticated is True

    @property
    def status(self) -> dict:
        connected, authenticated = self._ami_link()
        availability, reason = self._availability, self._reason
        present = self._device_present
        ami_down = _ami_down_reason(connected, authenticated)
        if ami_down:
            availability, reason, present = Availability.UNAVAILABLE, ami_down, None
        elif self._sample_time is not None and time.monotonic() - self._sample_time > self._stale_after:
            # Preserve the more specific poll failure if we already know it.
            if availability == Availability.AVAILABLE:
                availability, reason, present = Availability.UNAVAILABLE, "stale", None
        sample = self._status.to_dict() if self._sample_time is not None else None
        current = self._status.to_dict()
        if availability != Availability.AVAILABLE:
            current = {key: None for key in current}
        current.update(
            availability=availability, availability_reason=reason,
            ami_connected=connected, ami_authenticated=authenticated,
            device_present=present, last_sample=sample,
        )
        return current

    @property
    def readiness(self) -> dict:
        status = self.status
        return {"ready": status["availability"] == Availability.AVAILABLE
                and status["network_registered"] is True, **status}

    async def _poll_once(self) -> ModemStatus:
        response = await asyncio.wait_for(
            self._ami.send_action({"Action": DEVICE_LIST_ACTION}), timeout=_CMD_TIMEOUT,
        )
        events = response if isinstance(response, (list, tuple)) else [response]
        self.driver_ingress = {}
        for event in events:
            if event.get("Response") == "Error":
                raise RuntimeError("AMI action rejected")
            if event.get("Device") == DEFAULT_DONGLE_DEVICE:
                self.driver_ingress = {"version": event.get("SMSJournalVersion"),
                                       "status": event.get("SMSJournalStatus")}
                # The driver lists configured devices even when their ports never opened.
                if event.get("State") in ("Not connected", "Stopped"):
                    raise DeviceNotConnected(f"Device {DEFAULT_DONGLE_DEVICE} is not connected")
                return parse_device_entry(event)
        raise DeviceMissing(f"Device {DEFAULT_DONGLE_DEVICE} not found in DongleShowDevices response")

    def _queue_alert(self, method: str, payload: dict, state: tuple | None = None):
        if self._alert_sink is not None:
            self._pending_alerts.append((method, payload, state))

    async def _flush_alerts(self):
        # Stop at the first failure so alerts keep their order.
        while self._pending_alerts:
            method, payload, state = self._pending_alerts[0]
            try:
                result = await asyncio.wait_for(self._alert_sink(method, dict(payload), state), _DELIVERY_TIMEOUT)
                accepted = result is not False
            except Exception:
                logger.exception("Monitor alert pending")
                accepted = False
            if not accepted:
                return
            self._pending_alerts.pop(0)

    async def _push_status(self, status: dict):
        for receiver in self._status_receivers:
            try:
                if not await asyncio.wait_for(
                    receiver.push_modem_sensor(dict(status)), _DELIVERY_TIMEOUT,
                ):
                    logger.warning("Modem status update failed for %s; will retry next poll",
                                   type(receiver).__name__)
            except Exception:
                logger.warning("Modem status push failed for %s; will retry next poll",
                               type(receiver).__name__, exc_info=True)

    async def _take_sample(self) -> None:
        ami_down = _ami_down_reason(*self._ami_link())
        if ami_down:
            self._availability = Availability.UNAVAILABLE
            self._reason = ami_down
            self._device_present = None
            return
        try:
            new = await self._poll_once()
        except Exception as error:
            self._availability = Availability.UNAVAILABLE
            if isinstance(error, DeviceMissing):
                self._device_present = False
            elif isinstance(error, DeviceNotConnected):
                self._device_present = True
            else:
                self._device_present = None
            self._reason = _poll_failure_reason(error)
            logger.warning("Modem poll failed", exc_info=True)
        else:
            self._status = new
            self._sample_time = time.monotonic()
            self._availability = Availability.AVAILABLE
            self._reason = "registration_unknown" if new.network_registered is None else None
            self._device_present = True

    def _network_payload(self) -> dict:
        return {"operator": self._status.operator, "signal_rssi": self._status.signal_rssi}

    def _queue_state_change_alert(self, status: dict) -> None:
        registered = status["network_registered"]
        state = (status["availability"], registered)
        if state == self._alerted_state:
            return
        # No restoration without an earlier announced state, from this run or a stored one.
        first_state = self._alerted_state is None
        self._alerted_state = state
        if status["availability"] != Availability.AVAILABLE or registered is None:
            self._queue_alert(NotificationMethod.MODEM_UNAVAILABLE, {"reason": status["availability_reason"]}, state)
        elif registered is False:
            self._queue_alert(NotificationMethod.NETWORK_LOST, self._network_payload(), state)
        elif not first_state:
            self._queue_alert(NotificationMethod.NETWORK_RESTORED, self._network_payload(), state)

    def _queue_journal_alert(self) -> None:
        # Only an answered poll counts. A missing SMSJournalStatus (a driver without the
        # journal) is left to readiness and the deploy preflight.
        if self._availability != Availability.AVAILABLE:
            return
        journal = self.driver_ingress.get("status")
        if journal is None or journal == SMS_JOURNAL_OK:
            self._journal_failed_polls = 0
            return
        self._journal_failed_polls += 1
        if self._journal_failed_polls == SMS_JOURNAL_ALERT_POLLS:
            self._queue_alert(NotificationMethod.HEALTH, {"message": (
                f"SMS journal error reported by the driver for {SMS_JOURNAL_ALERT_POLLS} polls in a row: "
                f"new SMS stay in the modem, not delivered, until the driver can write its journal")})

    async def refresh(self) -> dict:
        """Perform one health check, publish state, and retry pending alerts.

        State is committed before delivery. Sink failures do not turn a valid
        modem sample into a poll failure or consume the undelivered transition.
        """
        async with self._refresh_lock:
            await self._take_sample()
            status = self.status
            self._queue_state_change_alert(status)
            self._queue_journal_alert()
            await self._push_status(status)
            await self._flush_alerts()
            return status

    async def run(self):
        """Load the last announced state, poll once AMI login settles, then every interval.

        AMI connects concurrently with this task at startup. Its own login latency
        is not an outage, so the first sample waits for login for at most one
        interval; an AMI that is still down after that is reported as unavailable.
        """
        if self._load_alert_state is not None:
            self._alerted_state = await self._load_alert_state()
        deadline = time.monotonic() + self._poll_interval
        while not self._ami_link()[1] and time.monotonic() < deadline:  # authenticated implies connected
            await asyncio.sleep(_LOGIN_WAIT_STEP)
        while True:
            await self.refresh()
            await asyncio.sleep(self._poll_interval)
