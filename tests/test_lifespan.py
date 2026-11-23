import asyncio
import logging
import time

import pytest
from httpx import AsyncClient, ASGITransport

from phonelet.config import Settings
from phonelet import main
from phonelet.notifiers import registry
from phonelet.constants import NotificationMethod
from phonelet.notifiers.telegram import TelegramNotifier
from phonelet.notifiers.homeassistant import HomeAssistantNotifier
from phonelet.notification import deliver_pending
from phonelet.notifiers.sip import SipDelivery
from phonelet.sms_commands.commands import registry as command_registry
from tests.helpers import local_paths, store_sms


class OfflineAmi:
    def __init__(self, **kwargs):
        self.loop = asyncio.get_running_loop()
        self.connected = self.authenticated = False
        self.stop_called = False

    async def start(self):
        await asyncio.Event().wait()  # even a hung connect must not block API

    def stop(self):
        self.stop_called = True

    async def send_action(self, action):
        raise ConnectionError('offline')


class QuietMonitor:
    def __init__(self, **kwargs):
        self.status = {}
        self.readiness = {'ready':False}
        self.driver_ingress = {}

    async def run(self):
        await asyncio.Event().wait()


@pytest.mark.parametrize('key,expected', [('', 'PC1 synthetic-text'), ('AA' * 21 + 'A', '[command]')],
                         ids=['commands-off', 'commands-on'])
async def test_runtime_enables_command_reception_only_with_a_key(tmp_path, isolated, key, expected):
    from phonelet.ingress import parse_fragment
    from tests.helpers import journal_record

    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake', sms_command_key=key)
    runtime = main.Runtime(config)
    await runtime.db.init()
    try:
        await runtime.db.ingest_fragment({**parse_fragment(journal_record('PC1 synthetic-text')),
                                           'received_epoch': time.time()})
        assert (await runtime.db.list_sms())[0]['text'] == expected
        assert bool(await runtime.db.next_sms_command()) == bool(key)
    finally:
        await runtime.db.close()


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(main, 'AmiClient', OfflineAmi)
    monkeypatch.setattr(main, 'ModemStatusMonitor', QuietMonitor)
    monkeypatch.setattr(command_registry, 'COMMANDS', [])


@pytest.mark.parametrize('key', ['', 'AA' * 21 + 'A'], ids=['commands-off', 'commands-on'])
@pytest.mark.parametrize('broken_db', [False, True], ids=['database-opens', 'database-fails'])
async def test_all_command_start_hooks_precede_database_open(
        tmp_path, isolated, monkeypatch, key, broken_db):
    from tests.test_sms_commands import EchoCommand

    events, built = [], []

    class FirstCommand(EchoCommand):
        @classmethod
        def from_settings(cls, config):
            command = cls()
            built.append((command, config))
            return command

        async def recover_at_start(self):
            events.append(self.name)
            await asyncio.sleep(0)

    class SecondCommand(FirstCommand):
        code, name = 201, 'second'

    class DefaultHookCommand(EchoCommand):
        code, name = 202, 'default'

    class UnavailableCommand(FirstCommand):
        @classmethod
        def from_settings(cls, config):
            return None

        async def recover_at_start(self):
            pytest.fail('unavailable command must not start')

    real_init = main.Database.init

    async def init(db):
        events.append('database')
        assert events == ['echo', 'second', 'database']
        if broken_db:
            raise RuntimeError('database does not open')
        await real_init(db)

    monkeypatch.setattr(command_registry, 'COMMANDS',
                        [FirstCommand, UnavailableCommand, DefaultHookCommand, SecondCommand])
    monkeypatch.setattr(main.Database, 'init', init)
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake', sms_command_key=key)
    app = main.create_application(config)

    async def start():
        async with app.router.lifespan_context(app):
            assert events == ['echo', 'second', 'database']

    if broken_db:
        with pytest.raises(RuntimeError, match='database does not open'):
            await start()
    else:
        await start()
    assert events == ['echo', 'second', 'database']
    runtime = app.state.runtime
    assert list(runtime.commands) == [200, 202, 201]
    assert [runtime.commands[code] for code in (200, 201)] == [command for command, _ in built]
    assert all(settings is config for _, settings in built)
    assert runtime.db._conn is None
    assert all(task.done() for task in runtime.tasks)


@pytest.mark.parametrize('history', [False, True], ids=['live', 'history'])
async def test_runtime_records_cdr_through_database_callback(tmp_path, isolated, history):
    from tests.test_cdr_events import cdr

    runtime = main.Runtime(Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake'))
    runtime.db.backends = ('telegram',)
    await runtime.db.init()
    try:
        event = cdr(Source='+15555550123', UserField='')
        await runtime.handler.handle_cdr(event, history=history)
        await runtime.handler.handle_cdr(event, history=history)
        [call] = await runtime.db.list_calls()
        assert call['caller'] == '+15555550123'
        assert call['duration'] == 115 and call['status'] == 'answered'
        assert call['timestamp'] == '2026-09-25T12:00:00+00:00'
        assert call['source_id'] == 'cdr:1770000000.1'
        assert call['recording_path'] is None
        jobs = await runtime.db._all('SELECT method FROM outbox')
        assert jobs == ([] if history else [{'method': NotificationMethod.CALL}])
    finally:
        await runtime.db.close()


async def test_two_offline_applications_have_isolated_runtime_and_cleanup(tmp_path,isolated):
    configs = [Settings(**local_paths(tmp_path/str(n)),api_key=f'api{n}',ami_secret='fake')
               for n in range(2)]
    apps = [main.create_application(c) for c in configs]
    assert not hasattr(apps[0].state,'runtime')
    async with apps[0].router.lifespan_context(apps[0]), apps[1].router.lifespan_context(apps[1]):
        runtimes = [a.state.runtime for a in apps]
        assert runtimes[0].db is not runtimes[1].db
        assert runtimes[0].ami is not runtimes[1].ami
        assert runtimes[0].ami.loop is asyncio.get_running_loop()
        await store_sms(runtimes[0].db, '+7','first')
        await store_sms(runtimes[1].db, '+8','second')
        for n,app in enumerate(apps):
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test',
                                   headers={'X-API-Key':f'api{n}'}) as client:
                response = await client.get('/api/sms')
                assert response.status_code == 200
                assert response.json()[0]['text'] == ['first','second'][n]
                assert (await client.get('/ready')).status_code == 503
    for runtime in runtimes:
        assert runtime.ami.stop_called
        assert runtime.db._conn is None
        assert all(t.done() for t in runtime.tasks)


async def test_partial_notifier_start_cleans_open_resources(tmp_path,isolated,monkeypatch):
    class Broken(TelegramNotifier):
        async def start(self):
            self.opened = True
            raise RuntimeError('startup failed')
        async def close(self):
            self.opened = False
    monkeypatch.setattr(registry, 'CHANNELS', [Broken])
    config = Settings(**local_paths(tmp_path),api_key='fake',ami_secret='fake',
                      telegram_token='fake',telegram_chat_id='fake')
    app = main.create_application(config)
    with pytest.raises(RuntimeError,match='startup failed'):
        async with app.router.lifespan_context(app):
            pytest.fail('must not yield')
    runtime = app.state.runtime
    assert not runtime.notifiers[0].opened
    assert runtime.db._conn is None
    assert runtime.ami.stop_called
    assert not runtime.tasks


async def test_dead_background_task_ends_the_process_but_shutdown_does_not(tmp_path, isolated, monkeypatch):
    # systemd (Restart=always) restarts the service; a silently dead monitor would end all alerts.
    ended = []
    monkeypatch.setattr(main, 'end_process', lambda: ended.append(True))
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake')
    app = main.create_application(config)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
    assert ended == []  # cancelled at shutdown

    class DyingMonitor(QuietMonitor):
        async def run(self):
            raise RuntimeError('monitor bug')
    monkeypatch.setattr(main, 'ModemStatusMonitor', DyingMonitor)
    app = main.create_application(Settings(**local_paths(tmp_path / 'second'), api_key='fake', ami_secret='fake'))
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if ended:
                break
            await asyncio.sleep(0.01)
    assert ended == [True]


@pytest.mark.parametrize('destination', ['', 'pjsip:phone'])
async def test_sip_destination_controls_sms_queue_and_delivery_loops(
        tmp_path, isolated, monkeypatch, caplog, destination):
    class Telegram(TelegramNotifier):
        async def start(self): pass
        async def close(self): pass

    class HomeAssistant(HomeAssistantNotifier):
        async def start(self): pass
        async def close(self): pass

    started = []

    async def delivery_loop(db, backends):
        started.extend(backend.backend_id for backend in backends)
        await asyncio.Event().wait()

    monkeypatch.setattr(main, 'delivery_loop', delivery_loop)
    sip_instances = []

    class Sip(SipDelivery):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sip_instances.append(self)

    monkeypatch.setattr(registry, 'CHANNELS', [Telegram, HomeAssistant, Sip])
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake',
                      sip_message_destination=destination,
                      telegram_token='fake', telegram_chat_id='fake',
                      ha_url='http://ha.invalid', ha_token='fake')
    app = main.create_application(config)
    with caplog.at_level(logging.INFO, logger='phonelet.notifiers.sip'):
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            expected = {'telegram', 'homeassistant'} | ({'sip'} if destination else set())
            assert set(runtime.db.backends) == expected
            assert len(sip_instances) == bool(destination)
            sip = next((n for n in runtime.notifiers if n.backend_id == 'sip'), None)
            if destination:
                assert sip is sip_instances[0]
                assert sip.destination == destination
            else:
                assert sip is None
                assert 'SIP SMS delivery disabled' in caplog.text
            await asyncio.sleep(0)
            assert sorted(started) == sorted(expected)
            await store_sms(runtime.db, '+15550000001', 'test SMS')
            assert await runtime.db.outbox_stats() == [
                {'backend': backend, 'status': 'pending', 'count': 1, 'attempts': 0}
                for backend in sorted(expected)]


async def test_disabling_sip_preserves_existing_pending_jobs(tmp_path, isolated):
    paths = local_paths(tmp_path)
    db = main.Database(paths['db_path'], backends=['sip'])
    await db.init()
    try:
        await store_sms(db, '+15550000001', 'previous SMS')
        before = await db._all('SELECT * FROM outbox')
        assert len(before) == 1
    finally:
        await db.close()

    config = Settings(**paths, api_key='fake', ami_secret='fake', sip_message_destination='')
    app = main.create_application(config)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        runtime = app.state.runtime
        await store_sms(runtime.db, '+15550000001', 'new SMS')
        assert await runtime.db._all('SELECT * FROM outbox') == before
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',
                               headers={'X-API-Key': 'fake'}) as client:
            response = await client.get('/api/stats')
        assert response.status_code == 200
        assert response.json()['notification_queue'] == [
            {'backend': 'sip', 'status': 'pending', 'count': 1, 'attempts': 0}]


async def test_registry_extension_builds_routes_and_delivers_without_runtime_changes(tmp_path, isolated, monkeypatch):
    built, loops, receivers = [], [], []

    class ExtraChannel(TelegramNotifier):
        backend_id = 'extra'
        secret_settings = ()

        @classmethod
        def from_settings(cls, config, *, ami):
            instance = cls(config, ami)
            built.append(instance)
            return instance

        def __init__(self, config, ami):
            self.config, self.ami = config, ami
            self.started = self.closed = False
            self.delivered = []

        async def start(self):
            self.started = True

        async def close(self):
            self.closed = True

        async def deliver(self, method, payload, checkpoint):
            self.delivered.append((method, payload))
            return True

        async def push_modem_sensor(self, status):
            return True

    class SmsOnlyChannel(ExtraChannel):
        backend_id = 'extra-sms'
        accepted_methods = frozenset({NotificationMethod.SMS})

    class Monitor(QuietMonitor):
        def __init__(self, *, status_receivers, **kwargs):
            super().__init__(**kwargs)
            receivers.extend(status_receivers)

    async def idle_delivery(db, backends):
        loops.extend(backends)
        await asyncio.Event().wait()

    monkeypatch.setattr(registry, 'CHANNELS', [*registry.CHANNELS, ExtraChannel, SmsOnlyChannel])
    monkeypatch.setattr(main, 'ModemStatusMonitor', Monitor)
    monkeypatch.setattr(main, 'delivery_loop', idle_delivery)
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake', sip_message_destination='')
    app = main.create_application(config)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        assert runtime.notifiers == built
        assert runtime.db.backends == ('extra', 'extra-sms')
        assert receivers == built
        assert all(n.config is config and n.ami is runtime.ami and n.started for n in built)
        await asyncio.sleep(0)
        assert loops == built
        await store_sms(runtime.db, '+100', 'synthetic SMS')
        for method in NotificationMethod:
            if method != NotificationMethod.SMS:
                await runtime.db.enqueue_notification(method, {})
        jobs = await runtime.db._all('SELECT backend, method FROM outbox ORDER BY id')
        assert {j['method'] for j in jobs if j['backend'] == 'extra'} == set(NotificationMethod)
        assert [j['method'] for j in jobs if j['backend'] == 'extra-sms'] == [NotificationMethod.SMS]
        await deliver_pending(runtime.db, runtime.notifiers)
        assert {method for method, _ in built[0].delivered} == set(NotificationMethod)
        assert built[1].delivered == [(NotificationMethod.SMS, {'sender': '+100', 'text': 'synthetic SMS'})]
        assert {row['status'] for row in await runtime.db.outbox_stats()} == {'delivered'}
    assert all(n.closed for n in built)
