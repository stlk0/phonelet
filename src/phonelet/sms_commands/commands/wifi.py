"""The Wi-Fi command (code 1): move the board to another Wi-Fi network.

Arguments: ssid length (1 byte) | ssid (UTF-8) | password (UTF-8; empty for an
open network). check_network and pack_args are mirrored by checkWifiNetwork and
packWifiArgs in tools/sms-command.html: change both together. Logs and
notifications may name the SSID, never the password.
"""
import asyncio
import logging
import shutil
import time

from phonelet.constants import NotificationMethod
from phonelet.netplan_wifi import NetplanState, NetplanWifi
from phonelet.sms_commands.commands.base import Command

logger = logging.getLogger(__name__)

CODE = 1
MAX_SSID_BYTES = 32
MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH = 8, 63  # WPA-PSK passphrase
_NOT_RUN = object()  # recover_at_start did not finish: the first tick rolls back itself
# Queued after the command's own message: it must not read as a rollback of that command.
FINISHED_BY_COMMAND_MESSAGE = "Wi-Fi: the earlier unfinished change was rolled back before the last command ran"


def check_network(ssid, password):
    """Raise ValueError naming the field (never its value) unless the command allows it."""
    if not 1 <= len(ssid.encode("utf-8")) <= MAX_SSID_BYTES:
        raise ValueError(f"SSID must be 1-{MAX_SSID_BYTES} bytes of UTF-8")
    if password and not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH
                         and all(" " <= c <= "~" for c in password)):
        raise ValueError(f"Password must be empty or {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} "
                         "printable ASCII characters")


def pack_args(ssid, password):
    check_network(ssid, password)
    raw_ssid = ssid.encode("utf-8")
    return bytes([len(raw_ssid)]) + raw_ssid + password.encode("utf-8")


def unpack_args(packed_args):
    """(ssid, password) of packed arguments; ValueError, never quoting their bytes."""
    if not packed_args:
        raise ValueError("No SSID length")
    ssid_end = 1 + packed_args[0]
    if len(packed_args) < ssid_end:
        raise ValueError("Truncated SSID")
    try:
        ssid, password = packed_args[1:ssid_end].decode("utf-8"), packed_args[ssid_end:].decode("utf-8")
    except UnicodeDecodeError:  # its text quotes the offending byte
        raise ValueError("SSID or password is not UTF-8") from None
    check_network(ssid, password)
    return ssid, password


class WifiCommand(Command):
    """Applies a network through NetplanWifi in a thread, and reports the rollback of an unfinished change."""
    code, name, label = CODE, "wifi", "Wi-Fi"

    @classmethod
    def from_settings(cls, config):
        if not shutil.which("netplan") or not shutil.which("wpa_cli"):
            return None
        return cls(NetplanWifi(config.wifi_netplan_file, config.wifi_state_dir,
                               config.wifi_interface, config.wifi_connect_timeout))

    def __init__(self, netplan):
        self.netplan = netplan
        self._start_outcome = _NOT_RUN  # recover_at_start's outcome (None: nothing to roll back)
        self._recovered = self._failure_reported = False
        self._report = None  # kept until queued: a database error retries it next pass

    async def recover_at_start(self):
        """Roll back a change a crash left half-applied; needs no database (Runtime.lifespan says why).

        The first tick reports it.
        """
        try:
            self._start_outcome = await self._recover()
        except Exception:
            logger.exception("Wi-Fi start-up rollback failed; the worker retries")

    async def tick(self, db):
        """Retry a rollback that failed, at start or during a change, every pass while its marker exists.

        A failure is reported once, the success when it comes.
        """
        # While a failure is reported, a command's apply() may finish the rollback: look again.
        if not self._recovered or self._failure_reported or self.netplan.has_unfinished_change():
            outcome, self._start_outcome = self._start_outcome, _NOT_RUN
            if outcome is _NOT_RUN:
                outcome = await self._recover()
            self._note_recovery(outcome)
        if self._report:
            await db.enqueue_notification(NotificationMethod.HEALTH, {"message": self._report})
            self._report = None

    async def _recover(self):
        started = time.monotonic()
        outcome = await asyncio.to_thread(self.netplan.recover)
        if outcome is not None:
            logger.warning("Wi-Fi unfinished change: %s in %.0f s", outcome.result, time.monotonic() - started)
        return outcome

    def _note_recovery(self, outcome):
        if outcome is not None and outcome.state != NetplanState.RESTORED:
            if not self._failure_reported:
                self._failure_reported = True
                self._report = outcome.recovery_message()
            return
        if outcome is not None:
            self._report = outcome.recovery_message()
        elif self._failure_reported:  # apply() finished the rollback that failed
            self._report = FINISHED_BY_COMMAND_MESSAGE
        self._recovered, self._failure_reported = True, False

    def interrupted_message(self):
        return "Wi-Fi: change interrupted by a restart; not repeated, network left as it ended"

    def parse(self, packed_args):
        return unpack_args(packed_args)

    def describe(self, network) -> str:
        ssid, _ = network
        return f"ssid={ssid!r}"

    async def run(self, network):
        ssid, password = network
        outcome = await asyncio.to_thread(self.netplan.apply, ssid, password)
        return outcome.result, outcome.message(ssid)
