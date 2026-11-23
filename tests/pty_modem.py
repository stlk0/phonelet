"""Offline one-PTY modem and isolated runner for the pinned, patched C driver.

Only PTYs created by this object can be passed to DriverSession. All paths,
subprocess output, binaries and journals belong to the caller's test directory.
The helpers at the end are shared by the PTY test modules.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import pty
import queue
import re
import select
import subprocess
import threading
import time
import tty
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Self

from phonelet.ingress import parse_fragment

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures'
REVISION = '7db8e75bc2b7c81b173e1bb465e376e64a70319a'
# Apply order of the project patches, shared with scripts/prepare-sources.sh.
DRIVER_PATCHES = tuple((ROOT / 'chan_dongle_patch/series').read_text().split())


# Added by chan_dongle_me_storage.patch; the runner needs that patch.
STORAGE_FUNCTIONS = {
    'at_response.c': ['phonelet_storage_counts', 'phonelet_storage_init', 'phonelet_storage_selected',
                      'phonelet_storage_parse', 'phonelet_storage_notice'],
    'at_command.c': ['phonelet_storage_select', 'phonelet_storage_next'],
}


def extract_function(source: str, name: str) -> str:
    """Source of one top-level C function, byte for byte, up to its closing brace."""
    match = re.search(r'^(?:EXPORT_DEF |static )[^\n;]*\b' + name + r'\s*\(', source, re.M)
    if not match:
        # at_poll_sms has its return type on the preceding line.
        match = re.search(r'^int\n' + name + r'\s*\(', source, re.M)
    if not match:
        raise ValueError(f'Cannot find upstream function {name}')
    end = source.index('\n}', match.start()) + 2
    return source[match.start():end] + '\n'


def patch_sources(build_dir: Path, patches: tuple[str, ...] = DRIVER_PATCHES) -> None:
    """Copy the verified pristine sources and apply the project patches in order."""
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / 'phonelet_ingress.h').unlink(missing_ok=True)
    source = FIXTURES / 'chan_dongle'
    assert f'CHAN_DONGLE_COMMIT={REVISION}' in (ROOT / 'asterisk/sources.lock').read_text()
    for name, digest in json.loads((source / 'SHA256.json').read_text()).items():
        content = (source / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest, name
        (build_dir / name).write_bytes(content)
    for name in patches:
        result = subprocess.run(
            ['patch', '--batch', '--forward', '--fuzz=0', '-p1', '-i',
             str(ROOT / f'chan_dongle_patch/chan_dongle_{name}.patch')],
            cwd=build_dir, capture_output=True, text=True, check=True,
        )
        assert 'offset' not in result.stdout and 'fuzz' not in result.stdout, result.stdout


def compile_driver(build_dir: Path, *, sanitize: bool = False,
                   patches: tuple[str, ...] = DRIVER_PATCHES) -> Path:
    """Patch the verified pristine sources (all project patches unless `patches` says otherwise),
    extract the real C paths and compile the runner."""
    patch_sources(build_dir, patches)
    # Only dependencies on Asterisk infrastructure are replaced. The functions
    # below are extracted directly after applying the production patches.
    selections = {
        'chan_dongle.h': ['phonelet_monotonic'],
        'chan_dongle.c': ['port_status', 'opentty', 'phonelet_rescan_tick', 'do_monitor_phone'],
        'at_queue.c': ['at_queue_run', 'at_queue_timeout'],
        'at_response.c': [*STORAGE_FUNCTIONS['at_response.c'], 'phonelet_sms_unanswered',
                          'log_cmd_response_error', 'at_response_error',
                          'at_response_ring', 'at_poll_sms', 'at_response_cmti', 'at_response_cmgr',
                          'at_response_busy', 'at_response_sms_prompt'],
        'at_command.c': ['at_enqueue_initialization', 'at_retrieve_next_sms', 'at_enqueue_retrieve_sms',
                         'at_enqueue_delete_sms', 'phonelet_enqueue_sms_sync', 'at_enqueue_pdu',
                         *STORAGE_FUNCTIONS['at_command.c']],
    }
    for filename, names in selections.items():
        contents = (build_dir / filename).read_text()
        (build_dir / (filename + '.inc')).write_text('\n'.join(extract_function(contents, name) for name in names))
    # Keep the complete production +CMGS arm, with only its dispatch adapted.
    response = (build_dir / 'at_response.c').read_text()
    start = response.index('\t\t\tcase RES_CMGS:')
    end = response.index('\t\t\tcase RES_OK:', start)
    (build_dir / 'cmgs.inc').write_text(response[start:end])
    # Command IDs and suppression modes without the Asterisk header dependencies.
    command_header = (build_dir / 'at_command.h').read_text()
    (build_dir / 'at_command_ids.inc').write_text(command_header[
        command_header.index('#define AT_CMD_AS_ENUM'):command_header.index('/*!')
    ])
    # Complete framing, ring-buffer, PDU and text codec implementation. Includes
    # are supplied by driver_runner.c; their Asterisk dependencies are unused.
    for filename in ('at_read.c', 'ringbuffer.c', 'at_parse.c', 'pdu.c', 'char_conv.c'):
        contents = (build_dir / filename).read_text()
        (build_dir / (filename + '.inc')).write_text('\n'.join(
            line for line in contents.splitlines() if not line.startswith('#include')
        ) + '\n')
    executable = build_dir / 'driver-runner'
    sanitizer_flags = ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-g'] if sanitize else []
    subprocess.run([
        'cc', *sanitizer_flags, '-pthread', '-D_GNU_SOURCE', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
        '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-sign-compare',
        '-Wno-array-parameter',
        '-ffunction-sections', '-fdata-sections', '-Wl,--gc-sections',
        '-I', str(build_dir), str(FIXTURES / 'driver_runner.c'), '-o', str(executable),
    ], check=True, capture_output=True, text=True)
    return executable


@dataclass(frozen=True)
class _Reply:
    chunks: tuple[bytes, ...]
    delay: float = 0
    chunk_delay: float = 0


# Slots per SMS memory, as the E1550 stand reports them: SIM card and modem.
CAPACITY = {'SM': 15, 'ME': 23}


def cmgr_reply(pdu: str) -> bytes:
    """+CMGR answer for a stored PDU; the length excludes the SMSC field."""
    length = len(bytes.fromhex(pdu)) - 1 - int(pdu[:2], 16)
    return f'\r\n+CMGR: 0,,{length}\r\n{pdu}\r\n\r\nOK\r\n'.encode()


class PtyModem:
    """One serial byte stream (data= on PCUI) and two SMS memories surviving unplug.

    SM (SIM) and ME (modem) have their own 0-based slots. AT+CPMS= takes one
    to three quoted names for read/delete, write and receive; omitted ones stay
    and the reply has the +CPMS counts. An unquoted or unknown name, or one in
    refused, gets storage_error and changes nothing. CMGR and CMGD use the read
    memory; deliver() stores into the receive memory and names it in +CMTI.
    As on the E1550, ATZ sets read and write to SM, and the receive memory
    survives unplug and power loss; the stand starts as "SM","SM","SM".
    The default modem answers AT and scans/reads/deletes SMS. Scripts override
    any exact command, may split/delay arbitrary bytes, or suppress replies;
    a scripted command bypasses the memories. send_data adds unsolicited
    traffic to the same stream, also during replies.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True)
        self.data_path = directory / 'data'
        self.memories: dict[str, dict[int, str]] = {name: {} for name in CAPACITY}
        self.sms = self.memories['ME']  # the patched driver receives into ME
        self.selected = ['SM', 'SM', 'SM']
        self.refused: set[str] = set()
        self.storage_error = b'\r\n+CMS ERROR: 302\r\n'
        self.commands: list[str] = []
        self.deletions: list[tuple[str, int]] = []
        self.reads: list[tuple[str, int]] = []
        # Called as (memory, slot) when CMGD arrives, before the slot is removed.
        self.on_delete: Callable[[str, int], None] | None = None
        self.scripts: dict[str, _Reply | None] = {}
        self._master: int | None = None
        self._slave: int | None = None
        self._pending: list[tuple[float, int, bytes]] = []  # (when, generation, chunk)
        self._generation = 0
        self._buffer = b''
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._errors: queue.Queue[BaseException] = queue.Queue()
        self.reappear()
        self._thread = threading.Thread(target=self._run, name='pty-modem', daemon=True)
        self._thread.start()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def deleted(self) -> list[int]:
        """Deleted slots in order, of either memory."""
        return [slot for _, slot in self.deletions]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.disconnect()
        assert not self._thread.is_alive()
        if not self._errors.empty():
            raise self._errors.get()

    def put_sms(self, slot: int, pdu: str, memory: str = 'ME') -> None:
        assert 0 <= slot < CAPACITY[memory]
        bytes.fromhex(pdu)
        with self._lock:
            self.memories[memory][slot] = pdu.upper()

    def deliver(self, pdu: str, slot: int | None = None) -> tuple[str, int]:
        """Store a new SMS in the receive memory and send +CMTI naming it.

        The slot is the lowest free one unless given.
        """
        bytes.fromhex(pdu)
        with self._lock:
            memory = self.selected[2]
            if slot is None:
                slot = min(set(range(CAPACITY[memory])) - set(self.memories[memory]))
            assert slot not in self.memories[memory]
            self.memories[memory][slot] = pdu.upper()
            self.send_data(f'\r\n+CMTI: "{memory}",{slot}\r\n'.encode())
        return memory, slot

    def script(self, command: str, chunks: list[bytes], *, delay: float = 0,
               chunk_delay: float = 0) -> None:
        with self._lock:
            self.scripts[command] = _Reply(tuple(chunks), delay, chunk_delay)

    def silence(self, command: str) -> None:
        with self._lock:
            self.scripts[command] = None

    def clear_script(self, command: str) -> None:
        with self._lock:
            self.scripts.pop(command, None)

    def send_data(self, content: bytes) -> None:
        with self._lock:
            if self._master is None:
                raise ValueError('data PTY is disconnected')
            self._pending.append((time.monotonic(), self._generation, content))

    def disconnect(self) -> None:
        with self._lock:
            for fd in (self._master, self._slave):
                if fd is not None:
                    os.close(fd)
            self._master = self._slave = None
            self.data_path.unlink(missing_ok=True)
            self._pending.clear()
            self._buffer = b''
            self._generation += 1

    def reappear(self) -> None:
        with self._lock:
            assert self._master is None, 'Disconnect before replacing the PTY'
            master, slave = pty.openpty()
            tty.setraw(slave)
            os.set_blocking(master, False)
            self._master, self._slave = master, slave
            self.data_path.symlink_to(os.ttyname(slave))

    def assert_data_path_is_own_pty(self) -> None:
        """Refuse a data_path that does not lead to the PTY this harness created."""
        with self._lock:
            expected = Path(os.ttyname(self._slave))
            assert self.data_path.resolve() == expected, 'data path is not this harness PTY'
            assert expected.as_posix().startswith('/dev/pts/')

    def _reply(self, command: str) -> _Reply | None:
        if command in self.scripts:
            return self.scripts[command]
        if command == 'ATZ':
            self.selected[:2] = ['SM', 'SM']
        if command.startswith('AT+CPMS='):
            fields = command.partition('=')[2].split(',')
            names = [field[1:-1] for field in fields if len(field) == 4 and field[0] == field[-1] == '"']
            if (not 1 <= len(fields) <= 3 or len(names) != len(fields) or not set(names) <= set(CAPACITY)
                    or set(names) & self.refused):
                return _Reply((self.storage_error,))
            self.selected[:len(names)] = names
            counts = ','.join(f'{len(self.memories[name])},{CAPACITY[name]}' for name in self.selected)
            return _Reply((f'\r\n+CPMS: {counts}\r\n\r\nOK\r\n'.encode(),))
        if command == 'AT+CPMS?':
            fields = ','.join(f'"{name}",{len(self.memories[name])},{CAPACITY[name]}'
                              for name in self.selected)
            return _Reply((f'\r\n+CPMS: {fields}\r\n\r\nOK\r\n'.encode(),))
        if command.startswith('AT+CMGR='):
            slot = int(command.partition('=')[2])
            self.reads.append((self.selected[0], slot))
            pdu = self.memories[self.selected[0]].get(slot)
            if pdu is None:
                return _Reply((b'\r\n+CMS ERROR: 321\r\n',))
            return _Reply((cmgr_reply(pdu),))
        if command.startswith('AT+CMGD='):
            slot = int(command.partition('=')[2])
            if self.on_delete:
                self.on_delete(self.selected[0], slot)
            self.memories[self.selected[0]].pop(slot, None)
            self.deletions.append((self.selected[0], slot))
        return _Reply((b'\r\nOK\r\n',))

    def _wait_for_data(self) -> bool:
        """Wait up to 2 ms, without the lock, for a command byte.

        A command is answered as soon as it arrives; the 2 ms bound keeps the
        latency of delayed replies and send_data low.
        """
        with self._lock:
            fd, generation = self._master, self._generation
        if fd is None:
            self._stop.wait(.002)
            return False
        try:
            return bool(select.select([fd], [], [], .002)[0])
        except (OSError, ValueError):
            # Expected when disconnect() closed the PTY meanwhile. If the same PTY is
            # still current it is a real failure (EBADF, fd >= 1024): let _run report it.
            with self._lock:
                if self._master == fd and self._generation == generation:
                    raise
            return False

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                ready = self._wait_for_data()
                with self._lock:
                    if self._master is None:
                        continue
                    if ready:  # a replaced PTY may reuse the number: the read is non-blocking
                        self._answer_commands()
                    self._flush_pending()
        except BaseException as exc:
            self._errors.put(exc)

    def _answer_commands(self) -> None:
        """Read command bytes, split them at CR or Ctrl-Z and queue each reply; holds the lock."""
        try:
            content = os.read(self._master, 65536)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EAGAIN):
                content = b''
            else:
                raise
        self._buffer += content
        while b'\r' in self._buffer or b'\x1a' in self._buffer:
            end = min(i for i in (self._buffer.find(b'\r'), self._buffer.find(b'\x1a')) if i >= 0)
            raw, self._buffer = self._buffer[:end], self._buffer[end + 1:]
            command = raw.decode('ascii')
            self.commands.append(command)
            reply = self._reply(command)
            if reply is not None:
                for index, chunk in enumerate(reply.chunks):
                    self._pending.append((time.monotonic() + reply.delay + index * reply.chunk_delay,
                                          self._generation, chunk))

    def _flush_pending(self) -> None:
        """Write the due chunks in order; after a short write the rest waits; holds the lock."""
        now = time.monotonic()
        pending, self._pending = self._pending, []
        blocked = False
        for when, generation, chunk in pending:
            if generation != self._generation:
                continue
            if when > now or blocked:
                self._pending.append((when, generation, chunk))
                continue
            try:
                written = os.write(self._master, chunk)
            except BlockingIOError:
                written = 0
            if written < len(chunk):
                self._pending.append((now, generation, chunk[written:]))
                blocked = True


class DriverSession:
    """Runner lifecycle and a bounded event queue with useful failure diagnostics."""

    def __init__(self, executable: Path, modem: PtyModem, spool: Path, *, timeout_ms: int = 300):
        self.events: list[tuple[str, ...]] = []
        self._events: queue.Queue[tuple[str, ...]] = queue.Queue()
        self._backlog: deque[tuple[str, ...]] = deque()
        modem.assert_data_path_is_own_pty()  # before the C process opens it
        self.process = subprocess.Popen(
            [str(executable), str(modem.data_path), str(spool), str(timeout_ms)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._thread = threading.Thread(target=self._read_events, daemon=True)
        self._thread.start()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self._thread.join(timeout=2)
        assert self.process.stdout is not None
        self.process.stdout.close()
        assert self.process.stdin is not None
        self.process.stdin.close()

    def command(self, text: str, timeout_ms: int | None = None) -> None:
        """Send one line to the C writer thread.

        ADVANCE= (both clocks), WALL= (time() only), STATE= and FAIL_*= lines adjust the runner
        at once (event ADAPTED).
        Any other line waits for an idle monitor wait with an empty queue: SEND_SMS
        queues a test PDU, other text is written as an AT command with timeout_ms
        (None: the runner default); both report WRITTEN.
        """
        assert text and not any(char in text for char in '\r\n\t')
        assert timeout_ms is None or 10 <= timeout_ms <= 10000
        assert self.process.stdin is not None
        self.process.stdin.write(f'{timeout_ms or 0}\t{text}\n')
        self.process.stdin.flush()

    def adapt(self, control: str) -> None:
        """Apply one runner control line and wait for its own ADAPTED echo."""
        self.command(control)
        assert self.wait('ADAPTED') == (control,)

    def _read_events(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            event = tuple(line.rstrip('\n').split('\t'))
            self.events.append(event)
            self._events.put(event)

    def wait(self, kind: str, timeout: float = 5) -> tuple[str, ...]:
        deadline = time.monotonic() + timeout
        for event in tuple(self._backlog):
            if event[0] == kind:
                self._backlog.remove(event)
                return event[1:]
        while True:
            try:
                event = self._events.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                raise AssertionError(f'Timed out waiting for {kind}; recent events: {self.events[-15:]}') from None
            if event[0] == kind:
                return event[1:]
            self._backlog.append(event)


# Helpers shared by the PTY test modules.

# SMS memories (chan_dongle_me_storage.patch): the init list selects modem
# memory (ME) for read, write and receive. Then only the read memory changes:
# the SIM (SM) is read through, then ME. SMS keep arriving in ME meanwhile.
INIT_ME = 'AT+CPMS="ME","ME","ME"'
READ_SM = 'AT+CPMS="SM"'
READ_ME = 'AT+CPMS="ME"'
KEEP_SM = 'AT+CPMS="SM","SM","SM"'
CNMI = 'AT+CNMI=2,1,0,2,0'


def until(predicate: Callable[[], object], timeout: float = 3) -> None:
    """Poll predicate until it is true; raise AssertionError after timeout seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.002)
    assert predicate(), 'PTY condition timed out'


def fragments(spool: Path) -> list[dict]:
    """Every record in the driver's journal spool."""
    return [parse_fragment(path.read_bytes()) for path in spool.glob('*.sms')]


def texts(spool: Path) -> list[str]:
    return sorted(fragment['text'] for fragment in fragments(spool))


def journal_before_delete(modem: PtyModem, spool: Path) -> list[tuple[str, int]]:
    """Record every CMGD whose slot content is not yet in the journal."""
    violations = []

    def check(memory, slot):
        pdu = modem.memories[memory].get(slot)
        journaled = any(fragment['slot'] == slot and fragment['pdu'].upper() == pdu
                        for fragment in fragments(spool))
        if pdu is None or not journaled:
            violations.append((memory, slot))

    modem.on_delete = check
    return violations


def logs(driver: DriverSession, marker: int = 0) -> list[str]:
    return [event[1] for event in driver.events[marker:] if event[0] == 'LOG']


def storage(commands: list[str]) -> list[str]:
    return [command for command in commands if command.startswith('AT+CPMS')]


def scripted_reads(monkeypatch, modem: PtyModem, command: str, replies: list[bytes], *,
                   memory: str | None = None) -> None:
    """Use each reply once, then resume the synthetic modem's normal memory read."""
    original = modem._reply
    remaining = iter(replies)

    def reply(cmd):
        if cmd == command and (memory is None or modem.selected[0] == memory):
            value = next(remaining, None)
            if value is None:
                modem.clear_script(cmd)
            else:
                modem.script(cmd, [value])
        return original(cmd)
    monkeypatch.setattr(modem, '_reply', reply)


def wrap_reply(monkeypatch, modem: PtyModem, hook) -> None:
    """Let hook(command, reply) change the synthetic modem's reply."""
    original = modem._reply
    monkeypatch.setattr(modem, '_reply', lambda command: hook(command, original(command)))
