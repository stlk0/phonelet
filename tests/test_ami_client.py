import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from phonelet import ami_client, modem_status
from phonelet.ami_client import AmiClient, Message, encode_action, parse_message
from phonelet.modem_status import ModemStatusMonitor

SECRET = 'fake-ami-secret'


def test_framing_round_trip_and_case_insensitive_lookup():
    data = encode_action({'Action': 'MessageSend', 'To': 'sip:phone@192.0.2.1:5060'})
    assert data == b'Action: MessageSend\r\nTo: sip:phone@192.0.2.1:5060\r\n\r\n'
    message = parse_message(b'Asterisk Call Manager/9.0.0\r\nResponse: Success\r\nActionId: 7\r\n'
                            b'Message: a: b\r\n\r\n')
    assert message == {'Response': 'Success', 'ActionId': '7', 'Message': 'a: b'}
    assert message['actionid'] == message.get('ActionID') == '7' and 'RESPONSE' in message
    assert message.get('Missing', '') == ''


@pytest.mark.parametrize('action', [{'Secret': SECRET + '\r\nAction: Logoff'}, {'Ac:tion': 'x'}, {'': 'x'}])
def test_header_injection_is_refused_without_the_value(action):
    with pytest.raises(ValueError) as caught:
        encode_action(action)
    assert SECRET not in str(caught.value)


class FakeAmi:
    """AMI peer on 127.0.0.1 that records actions; it never reaches Asterisk."""

    def __init__(self):
        self.connections = []
        self.writers = []
        self.handlers = []
        self.hold = set()  # Actions received without any reply.
        self.held = []  # (action, ActionID) of held actions, to answer later.
        self.drop = {}  # One-shot: action -> partial reply written before closing.
        self.login_errors = []  # Rejections followed by a server-side disconnect.
        self.banner = True

    async def handle(self, reader, writer):
        actions = []
        self.connections.append(actions)
        self.writers.append(writer)
        self.handlers.append(asyncio.current_task())
        if self.banner:
            writer.write(b'Asterisk Call Manager/9.0.0\r\n')
        try:
            while True:
                raw = await reader.readuntil(b'\r\n\r\n')
                headers = dict(line.split(': ', 1) for line in raw.decode().strip().split('\r\n'))
                actions.append(headers)
                name, action_id = headers['Action'], headers['ActionID']
                if name == 'Login' and self.login_errors:
                    message = self.login_errors.pop(0)
                    writer.write((f'Response: Error\r\nActionID: {action_id}\r\n'
                                  f'Message: {message}\r\n\r\n').encode())
                    await writer.drain()
                    return
                if name in self.drop:
                    writer.write(self.drop.pop(name)(action_id).encode())
                    await writer.drain()
                    return
                if name in self.hold:
                    self.held.append((name, action_id))
                else:
                    writer.write(self.reply(name, action_id).encode())
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    def reply(name, action_id):
        if name == 'Login':
            # Events before and after the answer: neither is taken for it.
            return ('Event: FullyBooted\r\nPrivilege: system,all\r\n\r\n'
                    f'Response: Success\r\nActionID: {action_id}\r\n'
                    'Message: Authentication accepted\r\n\r\n'
                    'Event: FullyBooted\r\nPrivilege: system,all\r\nStatus: Fully Booted\r\n\r\n')
        if name == 'DongleShowDevices':
            return FakeAmi.device_list_start(action_id) + (
                f'Event: DongleShowDevicesComplete\r\nActionID: {action_id}\r\n'
                'EventList: Complete\r\nListItems: 1\r\n\r\n')
        return f'Response: Success\r\nActionID: {action_id}\r\nMessage: Message successfully sent\r\n\r\n'

    @staticmethod
    def device_list_start(action_id):
        return (f'Response: Success\r\nActionID: {action_id}\r\nEventList: start\r\n'
                'Message: Device status list will follow\r\n\r\n'
                f'Event: DongleDeviceEntry\r\nActionID: {action_id}\r\nDevice: dongle0\r\n'
                'SMSJournalVersion: 1\r\nSMSJournalStatus: ok\r\n\r\n')

    def actions(self, index):
        return [action['Action'] for action in self.connections[index]]

    async def write(self, data):
        self.writers[-1].write(data)
        await self.writers[-1].drain()


async def eventually(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


async def serve(fake, port=0):
    server = await asyncio.start_server(fake.handle, '127.0.0.1', port)
    fake.port = server.sockets[0].getsockname()[1]
    return server


async def shut(server, fake):
    server.close()
    for writer in fake.writers:
        writer.close()
    async with asyncio.timeout(2):
        await asyncio.gather(*fake.handlers)
        await server.wait_closed()


async def closed_port():
    fake = FakeAmi()
    await shut(await serve(fake), fake)
    return fake.port


@pytest_asyncio.fixture
async def fake_ami():
    fake = FakeAmi()
    server = await serve(fake)
    yield fake
    await shut(server, fake)


async def stop(ami):
    """stop() and prove the connection loop has ended: no task outlives the client."""
    ami.stop()
    if ami._task:
        async with asyncio.timeout(2):
            await asyncio.gather(ami._task, return_exceptions=True)
        assert ami._task.cancelled()
    assert not ami._pending and not ami.connected


def new_client(port, **kwargs):
    return AmiClient('127.0.0.1', port, 'test', SECRET, **{'reconnect_timeout': 0, **kwargs})


async def start_client(fake, **kwargs):
    ami = new_client(fake.port, **kwargs)
    try:
        await ami.start()
        await eventually(lambda: ami.authenticated)
    except BaseException:
        await stop(ami)
        raise
    return ami


@pytest_asyncio.fixture
async def live_client(fake_ami):
    ami = await start_client(fake_ami)
    try:
        yield ami
    finally:
        await stop(ami)


MESSAGE = {'Action': 'MessageSend', 'Destination': 'pjsip:phone', 'Base64Body': 'aGk='}


async def test_login_disables_events_and_device_list_arrives(fake_ami, live_client):
    login = fake_ami.connections[0][0]
    assert login == {'Action': 'Login', 'Username': 'test', 'Secret': SECRET, 'Events': 'off',
                     'ActionID': login['ActionID']}
    response = await live_client.send_action({'Action': 'DongleShowDevices', 'Device': 'dongle0'})
    assert [message.get('Event') for message in response] == [
        None, 'DongleDeviceEntry', 'DongleShowDevicesComplete']
    monitor = ModemStatusMonitor(live_client, alert_sink=AsyncMock())
    await monitor._poll_once()
    assert monitor.driver_ingress == {'version': '1', 'status': 'ok'}


async def test_single_response_is_one_message(fake_ami, live_client):
    response = await live_client.send_action({**MESSAGE, 'ActionID': 'caller-id'})
    assert isinstance(response, Message) and response['response'] == 'Success'
    sent = fake_ami.connections[0][1]
    assert sent['ActionID'] != 'caller-id' and response['ActionID'] == sent['ActionID']


async def test_error_answer_to_a_list_action_is_one_message(fake_ami, live_client):
    fake_ami.hold.add('DongleShowDevices')
    pending = asyncio.create_task(live_client.send_action({'Action': 'DongleShowDevices'}))
    await eventually(lambda: fake_ami.held)
    await fake_ami.write(f'Response: Error\r\nActionID: {fake_ami.held[0][1]}\r\n'
                         'Message: Device not found\r\n\r\n'.encode())
    assert (await asyncio.wait_for(pending, timeout=2)).get('Response') == 'Error'


async def test_concurrent_actions_are_matched_by_action_id(fake_ami, live_client):
    fake_ami.hold.update({'MessageSend', 'DongleShowDevices'})
    first = asyncio.create_task(live_client.send_action({'Action': 'DongleShowDevices'}))
    second = asyncio.create_task(live_client.send_action(dict(MESSAGE)))
    await eventually(lambda: len(fake_ami.held) == 2)
    # Answered in the reverse order, the list interleaved with the other answer.
    (_, devices_id), (_, message_id) = fake_ami.held
    await fake_ami.write((FakeAmi.device_list_start(devices_id) + FakeAmi.reply('MessageSend', message_id)
                          + f'Event: DongleShowDevicesComplete\r\nActionID: {devices_id}\r\n'
                          'EventList: Complete\r\n\r\n').encode())
    devices, sent = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert [m.get('Event') for m in devices] == [None, 'DongleDeviceEntry', 'DongleShowDevicesComplete']
    assert {m['ActionID'] for m in devices} == {devices_id}
    assert sent['ActionID'] == message_id and sent.get('Message') == 'Message successfully sent'


async def test_action_timeout_starts_a_fresh_session(fake_ami, caplog):
    # Asterisk answers a session's actions in turn: a stuck one would hold every later action.
    ami = await start_client(fake_ami, action_timeout=.2)
    try:
        fake_ami.hold.add('DongleShowDevices')
        with pytest.raises(TimeoutError):
            await ami.send_action({'Action': 'DongleShowDevices'})
        assert not ami._pending and not ami.authenticated
        await eventually(lambda: len(fake_ami.connections) == 2 and ami.authenticated)
        response = await ami.send_action(dict(MESSAGE))
        assert response['Response'] == 'Success'
        assert fake_ami.actions(1) == ['Login', 'MessageSend']
        records = [r for r in caplog.records if r.name == 'phonelet.ami_client']
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.WARNING, 'AMI action timed out; reconnect scheduled')]
        assert SECRET not in caplog.text
    finally:
        await stop(ami)


async def test_monitor_poll_timeout_is_the_clients_and_reconnects(fake_ami):
    # The client's own expiry, not an earlier one of the monitor, ends a stuck poll: a fresh session follows.
    assert modem_status._CMD_TIMEOUT > new_client(fake_ami.port)._action_timeout  # the defaults
    ami = await start_client(fake_ami, action_timeout=.2)
    try:
        monitor = ModemStatusMonitor(ami, alert_sink=AsyncMock())
        fake_ami.hold.add('DongleShowDevices')
        await monitor.refresh()
        assert monitor._reason == 'poll_timeout'
        await eventually(lambda: len(fake_ami.connections) == 2 and ami.authenticated)
        fake_ami.hold.clear()
        await monitor._poll_once()
        assert fake_ami.actions(1) == ['Login', 'DongleShowDevices']
        assert monitor.driver_ingress == {'version': '1', 'status': 'ok'}
    finally:
        await stop(ami)


async def test_unexpected_error_reconnects_instead_of_ending_the_loop(fake_ami, live_client, monkeypatch, caplog):
    # A defect while an answer is handled (inside _dispatch, which uses _is).
    is_ = ami_client._is

    def failing_once(*args):
        monkeypatch.setattr(ami_client, '_is', is_)
        raise RuntimeError('private ' + SECRET)

    monkeypatch.setattr(ami_client, '_is', failing_once)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(live_client.send_action(dict(MESSAGE)), timeout=2)
    await eventually(lambda: len(fake_ami.connections) == 2 and live_client.authenticated)
    assert (await live_client.send_action(dict(MESSAGE)))['Response'] == 'Success'
    assert 'AMI client error RuntimeError; reconnect scheduled' in caplog.text
    assert SECRET not in caplog.text and 'private' not in caplog.text


async def test_overlong_event_list_fails_the_action(fake_ami, live_client, monkeypatch):
    monkeypatch.setattr(ami_client, 'MAX_LIST_MESSAGES', 3)
    fake_ami.hold.add('DongleShowDevices')
    pending = asyncio.create_task(live_client.send_action({'Action': 'DongleShowDevices'}))
    await eventually(lambda: fake_ami.held)
    action_id = fake_ami.held[0][1]
    entry = f'Event: DongleDeviceEntry\r\nActionID: {action_id}\r\nDevice: dongle0\r\n\r\n'
    await fake_ami.write((FakeAmi.device_list_start(action_id) + entry * 5).encode())
    with pytest.raises(ValueError, match='too long'):
        await asyncio.wait_for(pending, timeout=2)
    assert not live_client._pending and live_client.authenticated
    fake_ami.hold.clear()
    assert (await live_client.send_action(dict(MESSAGE)))['Response'] == 'Success'


@pytest.mark.parametrize('action,partial', [
    (MESSAGE, lambda action_id: ''),
    ({'Action': 'DongleShowDevices'}, FakeAmi.device_list_start),
])
async def test_lost_connection_fails_pending_action_without_resend(fake_ami, live_client, action, partial):
    fake_ami.drop[action['Action']] = partial
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(live_client.send_action(dict(action)), timeout=2)
    assert not live_client._pending
    await eventually(lambda: len(fake_ami.connections) == 2 and live_client.authenticated)
    await live_client.send_action({'Action': 'DongleShowDevices'})
    # The outbox decides on retries; the transport sends nothing old.
    assert fake_ami.actions(1) == ['Login', 'DongleShowDevices']


async def test_not_authenticated_action_is_refused_at_once():
    ami = new_client(1)
    with pytest.raises(ConnectionError):
        await ami.send_action(dict(MESSAGE))


async def test_reconnects_after_server_restart(caplog):
    fake = FakeAmi()
    server = await serve(fake)
    ami = await start_client(fake)
    try:
        await shut(server, fake)
        await eventually(lambda: not ami.connected)
        server = await serve(fake, fake.port)
        await eventually(lambda: ami.authenticated)
        assert (await ami.send_action(dict(MESSAGE)))['Response'] == 'Success'
        assert fake.actions(-1) == ['Login', 'MessageSend']
        assert any('AMI connection lost' in r.getMessage() for r in caplog.records)
    finally:
        await stop(ami)
        await shut(server, fake)


async def test_offline_start_returns_and_connects_later(caplog):
    port = await closed_port()
    ami = new_client(port, reconnect_timeout=.05)
    fake = FakeAmi()
    server = None
    try:
        await asyncio.wait_for(ami.start(), timeout=2)
        assert not ami.connected
        assert 'AMI offline at startup' in caplog.text
        server = await serve(fake, port)
        await eventually(lambda: ami.authenticated)
    finally:
        await stop(ami)
        if server:
            await shut(server, fake)


async def test_stop_fails_pending_action_without_reconnect(fake_ami, live_client):
    fake_ami.hold.add('MessageSend')
    pending = asyncio.create_task(live_client.send_action(dict(MESSAGE)))
    await eventually(lambda: fake_ami.held)
    live_client.stop()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(pending, timeout=2)
    await stop(live_client)
    await asyncio.sleep(.1)
    assert len(fake_ami.connections) == 1
    await live_client.start()  # stop() is final
    assert live_client._task.cancelled() and not live_client.connected


async def test_stop_during_login_does_not_reconnect(fake_ami):
    fake_ami.hold.add('Login')
    ami = new_client(fake_ami.port)
    await ami.start()
    await eventually(lambda: fake_ami.held)
    assert ami.connected and not ami.authenticated
    await stop(ami)
    await asyncio.sleep(.1)
    assert len(fake_ami.connections) == 1


async def test_stop_during_connect_does_not_reconnect(monkeypatch, caplog):
    attempts = []

    async def hung_connect(*args, **kwargs):
        attempts.append(args)
        await asyncio.Event().wait()

    monkeypatch.setattr(ami_client.asyncio, 'open_connection', hung_connect)
    ami = new_client(1)
    start = asyncio.create_task(ami.start())
    await eventually(lambda: attempts)
    await stop(ami)
    await asyncio.wait_for(start, timeout=2)  # a waiting start() returns
    assert len(attempts) == 1
    assert 'AMI offline at startup' not in caplog.text


async def test_login_timeout_and_missing_banner_reconnect(fake_ami):
    fake_ami.banner = False
    ami = new_client(fake_ami.port, action_timeout=.1)
    try:
        await ami.start()
        await eventually(lambda: len(fake_ami.connections) >= 2)
        assert not ami.authenticated and fake_ami.actions(0) == []
        fake_ami.banner = True
        await eventually(lambda: ami.authenticated)
    finally:
        await stop(ami)


async def test_oversized_message_closes_the_connection(fake_ami, live_client):
    fake_ami.hold.add('MessageSend')
    pending = asyncio.create_task(live_client.send_action(dict(MESSAGE)))
    await eventually(lambda: fake_ami.held)
    # The server keeps the connection open: the client refuses the message itself.
    await fake_ami.write(f'Response: Success\r\nActionID: {fake_ami.held[0][1]}\r\n'
                         f'Message: {"x" * ami_client.MAX_MESSAGE_BYTES}\r\n\r\n'.encode())
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(pending, timeout=2)
    await eventually(lambda: len(fake_ami.connections) == 2 and live_client.authenticated)


async def test_connection_lost_during_login_reconnects_quietly(fake_ami, caplog):
    fake_ami.drop['Login'] = lambda action_id: ''
    ami = await start_client(fake_ami)
    try:
        assert fake_ami.actions(0) == ['Login'] and fake_ami.actions(1) == ['Login']
        assert not [record for record in caplog.records if record.name == 'asyncio']
    finally:
        await stop(ami)


async def test_rejected_login_is_logged_once_until_a_login_succeeds(fake_ami, caplog):
    caplog.set_level(logging.DEBUG, logger='phonelet.ami_client')
    rejection = 'private rejection: test / ' + SECRET
    fake_ami.login_errors = [rejection, rejection]
    ami = await start_client(fake_ami)
    try:
        assert len(fake_ami.connections) == 3
        records = [r for r in caplog.records if r.name == 'phonelet.ami_client']
        assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]
        assert all('AMI login rejected' in r.getMessage() for r in records)
        assert all('secret' in r.getMessage() and 'account' in r.getMessage()
                   and 'manager_custom.conf' in r.getMessage() for r in records)

        fake_ami.login_errors = [rejection]
        fake_ami.writers[-1].close()
        await eventually(lambda: len(fake_ami.connections) == 5 and ami.authenticated)
        records = [r for r in caplog.records if r.name == 'phonelet.ami_client']
        assert [(r.levelno, r.getMessage().split(';')[0]) for r in records][2:] == [
            (logging.WARNING, 'AMI connection lost'), (logging.WARNING, 'AMI login rejected')]
        assert SECRET not in caplog.text
        assert 'private rejection' not in caplog.text
    finally:
        await stop(ami)


async def test_unavailable_is_logged_once_and_secret_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    ami = new_client(await closed_port(), reconnect_timeout=.02)
    try:
        await ami.start()
        await eventually(lambda: len([r for r in caplog.records if r.name == 'phonelet.ami_client']) >= 4)
        records = [r for r in caplog.records if r.name == 'phonelet.ami_client']
        assert [r.levelno for r in records[:2]] == [logging.WARNING, logging.WARNING]  # unavailable, offline
        assert {r.levelno for r in records[2:]} == {logging.DEBUG}
        assert SECRET not in caplog.text
    finally:
        await stop(ami)
