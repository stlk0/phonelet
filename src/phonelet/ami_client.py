"""Asterisk Manager Interface client: login with Events: off, then requests and their answers only.

The message framing lives here once as pure functions (encode_action, parse_message); the
deploy preflight's synchronous check reuses them. There is no keepalive Ping: the modem
monitor polls on its own interval, and a write to a dead peer fails the same way a Ping would.
"""
import asyncio
import itertools
import logging
from collections.abc import Mapping

from phonelet.contracts import AmiAction, AmiResult

logger = logging.getLogger(__name__)

BANNER = b'Asterisk Call Manager/'
MAX_MESSAGE_BYTES = 65536  # one AMI message; a device list entry is well under 2 KiB
MAX_LIST_MESSAGES = 1024  # messages of one event list
_END = b'\r\n\r\n'


class Message(dict):
    """AMI headers in wire order; lookups ignore the case of the key."""

    def _key(self, key):
        if isinstance(key, str) and not dict.__contains__(self, key):
            folded = key.casefold()
            for name in self:
                if name.casefold() == folded:
                    return name
        return key

    def __getitem__(self, key):
        return dict.__getitem__(self, self._key(key))

    def __contains__(self, key):
        return dict.__contains__(self, self._key(key))

    def get(self, key, default=None):
        return dict.get(self, self._key(key), default)


def encode_action(action: Mapping[str, str]) -> bytes:
    lines = []
    for key, value in action.items():
        key, value = str(key), str(value)
        if not key or any(c in key for c in ':\r\n') or any(c in value for c in '\r\n'):
            # No header text in the error: values may be secrets or message bodies.
            raise ValueError('Invalid AMI header')
        lines.append(f'{key}: {value}\r\n')
    return (''.join(lines) + '\r\n').encode('utf-8')


def parse_message(block: bytes) -> Message:
    """`Key: Value` lines of one message; values may contain ':'; lines without one (the banner) are skipped."""
    message = Message()
    for line in block.decode('utf-8', errors='replace').split('\r\n'):
        key, colon, value = line.partition(':')
        if colon and key.strip():
            message[key.strip()] = value.strip()
    return message


def _is(message, key, value):
    return message.get(key, '').lower() == value


class AmiClient:
    def __init__(self, host, port, username, secret, reconnect_timeout=5, action_timeout=10):
        # Data arrives from durable journals; AMI only answers queries (Events: off).
        self._host, self._port = host, port
        self._username, self._secret = username, secret
        self._reconnect_timeout = reconnect_timeout
        self._action_timeout = action_timeout  # also bounds connect and login
        self._ids = (str(n) for n in itertools.count(1))
        self._pending = {}  # ActionID -> (future, messages so far)
        self._writer = None
        self._authenticated = False
        self._stopped = False
        self._task = None
        self._first_attempt = None
        self._last_failure = None

    @property
    def connected(self):
        return bool(not self._stopped and self._writer and not self._writer.is_closing())

    @property
    def authenticated(self):
        return self.connected and self._authenticated

    async def start(self):
        """Start the connection loop; return after its first connection attempt."""
        if self._stopped or self._task:
            return
        self._first_attempt = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name='ami-client')
        if await self._first_attempt is False:
            # The loop keeps reconnecting. History and replay remain available.
            logger.warning('AMI offline at startup; history remains available')

    def stop(self):
        """Final: no reconnect after it; pending actions fail at once; a waiting start() returns."""
        self._stopped = True
        if self._task:
            self._task.cancel()
            self._attempted(None)
        self._close()

    async def send_action(self, action: AmiAction) -> AmiResult:
        """One response Message, or the list of an event list's messages up to its Complete event."""
        if not self.authenticated:
            raise ConnectionError('AMI is not authenticated')
        action_id = next(self._ids)
        data = encode_action({**{k: v for k, v in action.items() if k.lower() != 'actionid'},
                              'ActionID': action_id})
        future = asyncio.get_running_loop().create_future()
        self._pending[action_id] = (future, [])
        writer = self._writer
        try:
            writer.write(data)
            async with asyncio.timeout(self._action_timeout):
                return await future
        except TimeoutError:
            # Asterisk answers a session's actions in turn: one stuck action would hold every
            # later one. A fresh session frees them; the loop reconnects.
            if writer is self._writer:
                logger.warning('AMI action timed out; reconnect scheduled')
                self._close()
            raise
        finally:
            self._pending.pop(action_id, None)

    def _log_failure(self, reason, message):
        log = logger.debug if self._last_failure == reason else logger.warning
        self._last_failure = reason
        log(message)

    def _attempted(self, connected):
        if not self._first_attempt.done():  # start() may have been cancelled
            self._first_attempt.set_result(connected)

    def _close(self):
        """Drop the connection; unanswered actions fail now and are never resent."""
        self._authenticated = False
        writer, self._writer = self._writer, None
        if writer:
            writer.close()
        pending = list(self._pending.values())
        self._pending.clear()
        for future, _ in pending:
            if not future.done():
                future.set_exception(ConnectionError('AMI connection closed'))

    async def _run(self):
        while not self._stopped:
            try:
                await self._connection()
            except Exception as error:
                # A defect must not end reconnecting for good; the type only, never the text.
                logger.error('AMI client error %s; reconnect scheduled', type(error).__name__)
                self._attempted(False)
            finally:
                self._close()
            await asyncio.sleep(self._reconnect_timeout)

    async def _connection(self):
        """One connection: connect, log in, dispatch answers until the connection ends."""
        try:
            async with asyncio.timeout(self._action_timeout):
                reader, self._writer = await asyncio.open_connection(
                    self._host, self._port, limit=MAX_MESSAGE_BYTES)
        except OSError:  # TimeoutError included
            self._log_failure('unavailable', 'AMI unavailable; reconnect scheduled')
            self._attempted(False)
            return
        self._attempted(True)
        try:
            await self._session(reader)
        except (OSError, EOFError, ValueError, asyncio.LimitOverrunError):
            # Lost connection, login timeout, an oversized or truncated message.
            if self._writer is None:
                pass  # closed here after an action timeout, already logged
            elif self._authenticated:
                self._last_failure = None
                logger.warning('AMI connection lost; reconnect scheduled')
            elif self._last_failure != 'login_rejected':
                self._log_failure('unavailable', 'AMI unavailable; reconnect scheduled')

    async def _read(self, reader):
        return parse_message(await reader.readuntil(_END))

    async def _session(self, reader):
        async with asyncio.timeout(self._action_timeout):
            if not (await reader.readline()).startswith(BANNER):
                raise ConnectionError('Not an AMI server')
            login_id = next(self._ids)
            self._writer.write(encode_action({'Action': 'Login', 'Username': self._username,
                                              'Secret': self._secret, 'Events': 'off',
                                              'ActionID': login_id}))
            while (response := await self._read(reader)).get('ActionID') != login_id:
                pass  # an event before the answer
        if not _is(response, 'Response', 'success'):
            self._log_failure('login_rejected',
                              'AMI login rejected; check the secret and account in manager_custom.conf')
            return
        self._last_failure = None
        self._authenticated = True
        while True:
            self._dispatch(await self._read(reader))

    def _dispatch(self, message):
        action_id = message.get('ActionID')
        entry = self._pending.get(action_id)
        if entry is None:
            return  # an event, or the late answer of an action that timed out
        future, messages = entry
        messages.append(message)
        if len(messages) == 1:
            if _is(message, 'Response', 'success') and _is(message, 'EventList', 'start'):
                return
            result = message
        elif _is(message, 'EventList', 'complete'):
            result = messages
        elif len(messages) > MAX_LIST_MESSAGES:
            result = ValueError('AMI event list too long')
        else:
            return
        del self._pending[action_id]
        if future.done():
            return
        if isinstance(result, Exception):
            future.set_exception(result)
        else:
            future.set_result(result)
