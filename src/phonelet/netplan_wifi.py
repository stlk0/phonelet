"""Apply a Wi-Fi network to netplan, with rollback.

The new network replaces the Wi-Fi netplan file; if it does not connect in
time, the previous file comes back. A marker in the state directory, written
before netplan changes, stands for an unfinished change: recover() rolls it
back (after a crash, or after a rollback that failed) and apply() starts no new
change while it exists. Logs and messages may name the SSID, never the password.
"""
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

NETPLAN_APPLY_TIMEOUT = 90
SUPPLICANT_TIMEOUT = 30
SYSTEMD_UNITS_DIR = "/run/systemd/system"  # where netplan generate puts netplan-wpa-<iface>.service
CHECK_COMMAND_TIMEOUT = 10  # wpa_cli and ip answer at once
CONNECT_POLL_SECONDS = 2
CONNECTED_POLLS = 2  # consecutive polls: one good poll can be a blip
MARKER_NAME = "pending.json"  # an unfinished change: its presence means roll back
PREVIOUS_EXISTED_KEY = "existed"  # the marker's field: whether there was a Wi-Fi netplan file to put back
BACKUP_NAME = "previous.yaml"  # the Wi-Fi netplan file before the change

CONNECTED = "connected"
ROLLED_BACK_MESSAGE = "Wi-Fi: interrupted change rolled back"
_WPA_ESCAPE = re.compile(rb'\\(?:x([0-9A-Fa-f]{2})|(["\\enrt]))')
_WPA_SIMPLE = {b"\\": b"\\", b'"': b'"', b"e": b"\x1b", b"n": b"\n", b"r": b"\r", b"t": b"\t"}


class FailureReason(StrEnum):
    """Why a change failed: failed:<reason> in the command's result, (<reason>) in its message."""
    INTERRUPTED = "interrupted"  # an unfinished change recover() found (a crash, or a failed rollback)
    ROLLBACK_PENDING = "rollback_pending"  # an earlier change is still not rolled back
    STATE_WRITE_FAILED = "state_write_failed"
    WRITE_FAILED = "write_failed"
    NETPLAN_APPLY_FAILED = "netplan_apply_failed"
    NETPLAN_APPLY_TIMEOUT = "netplan_apply_timeout"
    WPA_RESTART_FAILED = "wpa_restart_failed"
    CONNECT_TIMEOUT = "connect_timeout"
    ERROR = "error"


class NetplanState(StrEnum):
    """What a failed change left in netplan."""
    RESTORED = "restored"  # the previous file back, or removed
    ROLLBACK_FAILED = "rollback failed"
    UNCHANGED = "netplan unchanged"


def run_command(args, timeout):
    """(return code, stdout) of a command; return code None if it did not finish or start.

    Never raises: output that is not UTF-8 (an SSID byte from wpa_cli) is replaced, not an error.
    """
    try:
        done = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                              check=False, stdin=subprocess.DEVNULL)
    except Exception as error:  # argv holds no secret; the exception text stays out
        logger.warning("%s did not finish: %s", args[0], type(error).__name__)
        return None, ""
    return done.returncode, done.stdout


def _yaml_string(value):
    """A double-quoted scalar that YAML and JSON both read back as `value`.

    json.dumps escapes quotes, backslashes and C0 controls; not with ensure_ascii,
    whose surrogate-pair escapes libyaml rejects. YAML also forbids DEL and C1
    controls raw, reads U+2028/U+2029 as line breaks and U+FEFF as a byte order
    mark, and U+FFFE/U+FFFF are not YAML characters: those become \\uXXXX escapes too.
    """
    return "".join(f"\\u{ord(c):04x}" if 0x7f <= ord(c) <= 0x9f or c in "\u2028\u2029\ufeff\ufffe\uffff" else c
                   for c in json.dumps(value, ensure_ascii=False))


def render_netplan(interface, ssid, password):
    access_point = f"\n          password: {_yaml_string(password)}" if password else " {}"
    return ("network:\n  version: 2\n  wifis:\n"
            f"    {_yaml_string(interface)}:\n      dhcp4: true\n      dhcp6: true\n"
            f"      access-points:\n        {_yaml_string(ssid)}:{access_point}\n")


def unescape_wpa(value):
    """Bytes of a wpa_cli value: printf_encode writes \\\\, \\", \\e, \\n, \\r, \\t and \\xNN."""
    return _WPA_ESCAPE.sub(lambda match: bytes([int(match[1], 16)]) if match[1] else _WPA_SIMPLE[match[2]],
                           value.encode("utf-8"))


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path, data):
    """Temp file in the same directory, fsync, rename, fsync of the directory; mode 0600."""
    temporary = path.with_name("." + path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:  # a buffered write: all of data, or an error
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(temporary, path)
    except Exception:  # not BaseException: after a kill the file stays, the next write of `path` reuses it
        temporary.unlink(missing_ok=True)  # it may hold a password; nothing else removes it
        raise
    _fsync_dir(path.parent)


def _remove(path):
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_dir(path.parent)


@dataclass
class Outcome:
    reason: FailureReason | None = None  # None: the new network connected
    state: NetplanState = NetplanState.RESTORED
    previous_existed: bool = True
    previous_connected: bool = False

    def _previous_network(self, separator):
        if not self.previous_existed:
            return "no previous Wi-Fi configuration"
        return f"previous network connected{separator}{'yes' if self.previous_connected else 'no'}"

    @property
    def result(self):
        if self.reason is None:
            return CONNECTED
        if self.state == NetplanState.RESTORED:
            return f"failed:{self.reason}; {NetplanState.RESTORED}, {self._previous_network('=')}"
        return f"failed:{self.reason}; {self.state}"

    def message(self, ssid):
        """The HEALTH notification of a command's change."""
        if self.reason is None:
            return f'Wi-Fi: connected to "{ssid}"'
        if self.state == NetplanState.RESTORED:
            return (f'Wi-Fi: "{ssid}" did not connect ({self.reason}); previous configuration restored, '
                    f"{self._previous_network(': ')}")
        if self.state == NetplanState.UNCHANGED:
            return f'Wi-Fi: "{ssid}" not applied ({self.reason}); netplan unchanged'
        return f'Wi-Fi: "{ssid}" did not connect ({self.reason}); rollback failed, check the board'

    def recovery_message(self):
        """The HEALTH notification of a start-up (or retried) rollback."""
        if self.state == NetplanState.RESTORED:
            return f"{ROLLED_BACK_MESSAGE}, {self._previous_network(': ')}"
        return "Wi-Fi: interrupted change not rolled back yet; retrying, check the board"


class NetplanWifi:
    """Blocking netplan change with rollback; callers run it in a thread, one at a time."""
    def __init__(self, netplan_file, state_dir, interface, connect_timeout,
                 runner=run_command, sleep=time.sleep, clock=time.monotonic, units_dir=SYSTEMD_UNITS_DIR):
        self.netplan_file, self.state_dir = Path(netplan_file), Path(state_dir)
        self.interface, self.connect_timeout = interface, connect_timeout
        self.runner, self.sleep, self.clock = runner, sleep, clock
        self.units_dir = Path(units_dir)

    @property
    def supplicant_unit(self):
        return f"netplan-wpa-{self.interface}.service"

    @property
    def marker(self):
        return self.state_dir / MARKER_NAME

    @property
    def backup(self):
        return self.state_dir / BACKUP_NAME

    def apply(self, ssid, password):
        """Never raises: an unexpected error ends as a failed outcome, the marker kept if it was written."""
        try:
            return self._apply(ssid, password)
        except Exception:
            # logging_privacy keeps the exception text out of the journal. A kept marker: the next recover() rolls back.
            logger.exception("Wi-Fi change failed")
            try:
                unfinished = self.has_unfinished_change()
            except OSError:
                unfinished = True  # unknown: assume a change to roll back
            return Outcome(FailureReason.ERROR,
                           state=NetplanState.ROLLBACK_FAILED if unfinished else NetplanState.UNCHANGED)

    def has_unfinished_change(self):
        """A marker is left: recover() rolls it back."""
        return self.marker.exists()

    def _apply(self, ssid, password):
        # A rollback that failed earlier is finished first. Until it succeeds the copy
        # in the state directory is the only good configuration: never overwrite it.
        self.recover()
        if self.has_unfinished_change():
            logger.warning("Wi-Fi change not started: an earlier change is still not rolled back")
            return Outcome(FailureReason.ROLLBACK_PENDING, state=NetplanState.UNCHANGED)
        try:
            self._begin_change()
        except OSError:
            # Netplan is untouched: nothing to roll back.
            logger.exception("Wi-Fi change not started: state directory not writable")
            return Outcome(FailureReason.STATE_WRITE_FAILED, state=NetplanState.UNCHANGED)
        try:
            return self._change(ssid, password)
        except Exception:
            logger.exception("Wi-Fi change failed; rolling back")
            return self._roll_back(FailureReason.ERROR)

    def _change(self, ssid, password):
        try:
            _write_atomic(self.netplan_file, render_netplan(self.interface, ssid, password).encode("utf-8"))
        except OSError:
            logger.exception("Wi-Fi netplan file not written")
            return self._roll_back(FailureReason.WRITE_FAILED)
        code = self._netplan_apply()
        if code != 0:
            return self._roll_back(FailureReason.NETPLAN_APPLY_TIMEOUT if code is None
                                   else FailureReason.NETPLAN_APPLY_FAILED)
        # netplan 1.1.2 apply stops netplan-wpa-*.service with --no-block: the old supplicant can
        # survive on the old association (or the same SSID with the old password) and pass for success.
        if self._supplicant("restart") != 0:
            return self._roll_back(FailureReason.WPA_RESTART_FAILED)
        if not self._wait_connected(ssid.encode("utf-8")):
            return self._roll_back(FailureReason.CONNECT_TIMEOUT)
        try:
            _remove(self.marker)
            _remove(self.backup)
        except OSError:
            # The network works, but the stale marker makes the next recover() roll it back (and report it).
            logger.exception("Wi-Fi change marker not removed")
        return Outcome()

    def recover(self):
        """Roll back an unfinished change; None if there was none."""
        if not self.has_unfinished_change():
            return None
        logger.warning("Wi-Fi change unfinished; rolling back")
        return self._roll_back(FailureReason.INTERRUPTED)

    def _begin_change(self):
        """Copy the current file, then write the marker: from here a crash rolls back."""
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        try:
            previous = self.netplan_file.read_bytes()
        except FileNotFoundError:
            previous = None
        else:
            _write_atomic(self.backup, previous)
        # Last, so a marker always has its backup.
        _write_atomic(self.marker, json.dumps({PREVIOUS_EXISTED_KEY: previous is not None}).encode())

    def _roll_back(self, reason):
        try:
            existed = json.loads(self.marker.read_text())[PREVIOUS_EXISTED_KEY]
            if existed:
                _write_atomic(self.netplan_file, self.backup.read_bytes())
            else:
                _remove(self.netplan_file)
        except (OSError, ValueError, KeyError, TypeError):
            # The marker stays: the next recover() tries again.
            logger.exception("Wi-Fi rollback failed: netplan file not restored")
            return Outcome(reason, state=NetplanState.ROLLBACK_FAILED)
        code = self._netplan_apply()
        if code != 0:
            logger.warning("Wi-Fi rollback: netplan apply %s", "timed out" if code is None else f"exited {code}")
        # The old supplicant survives here too (see _change). Without the Wi-Fi file the unit is gone
        # unless another netplan file still defines the interface: then restart it, else stop the old process.
        restart = existed or (self.units_dir / self.supplicant_unit).exists()
        code = self._supplicant("restart" if restart else "stop")
        if code != 0:
            logger.warning("Wi-Fi rollback: supplicant %s", "timed out" if code is None else f"exited {code}")
        # Without a previous file there is no previous network to wait for.
        connected = existed and self._wait_connected(None)
        try:
            _remove(self.marker)
            _remove(self.backup)
        except OSError:
            logger.exception("Wi-Fi change marker not removed after rollback")
        return Outcome(reason, previous_existed=existed, previous_connected=connected)

    def _netplan_apply(self):
        code, _ = self.runner(["netplan", "apply"], NETPLAN_APPLY_TIMEOUT)
        return code

    def _supplicant(self, action):
        code, _ = self.runner(["systemctl", action, self.supplicant_unit], SUPPLICANT_TIMEOUT)
        return code

    def _wait_connected(self, raw_ssid):
        deadline = self.clock() + self.connect_timeout
        streak = 0
        while True:
            streak = streak + 1 if self._connected(raw_ssid) else 0
            if streak >= CONNECTED_POLLS:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(CONNECT_POLL_SECONDS)

    def _connected(self, raw_ssid):
        """COMPLETED association (to raw_ssid, unless None), an IPv4 address and a default route."""
        code, output = self.runner(["wpa_cli", "-i", self.interface, "status"], CHECK_COMMAND_TIMEOUT)
        if code != 0:
            return False
        status = dict(line.partition("=")[::2] for line in output.splitlines())
        if status.get("wpa_state") != "COMPLETED":
            return False
        if raw_ssid is not None and unescape_wpa(status.get("ssid", "")) != raw_ssid:
            return False
        try:
            code, output = self.runner(["ip", "-j", "-4", "addr", "show", "dev", self.interface],
                                       CHECK_COMMAND_TIMEOUT)
            if code != 0 or not any(address.get("family") == "inet" and address.get("local")
                                    for link in json.loads(output) for address in link.get("addr_info", ())):
                return False
            code, output = self.runner(["ip", "-j", "-4", "route", "show", "default", "dev", self.interface],
                                       CHECK_COMMAND_TIMEOUT)
            return code == 0 and any(route.get("dst") == "default" for route in json.loads(output))
        except (ValueError, AttributeError, TypeError):
            return False

