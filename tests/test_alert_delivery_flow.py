"""Modem state change -> durable alert -> delivery, one independent job per backend.

Goes through the production wiring: main.create_application and its lifespan build the
Database, the monitor with its alert sink, IngressReplay and the backends handed to the
delivery loops. Real: that wiring, the monitor, IngressReplay, the SQLite outbox,
deliver_pending, SipDelivery and the Telegram and Home Assistant notifiers, whose HTTP
requests go to an httpx.MockTransport. Fakes: the HTTP APIs, the AMI (scripted answers) and
the delivery loops (they only record their backends; the test delivers explicitly).
All paths are under tmp_path; no network, Asterisk or modem.
"""
import asyncio
import base64
import functools
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from phonelet import main
from phonelet.config import Settings
from phonelet.modem_status import SMS_JOURNAL_ALERT_POLLS
from phonelet.notification import deliver_pending
from phonelet.notifiers.base import BaseNotifier
from phonelet.notifiers import registry
from phonelet.notifiers.homeassistant import HomeAssistantNotifier
from phonelet.constants import NotificationMethod
from phonelet.notifiers.telegram import TelegramNotifier
from phonelet.notifiers.sip import SipDelivery
from tests.helpers import local_paths, store_sms

HOME = {'Event': 'DongleDeviceEntry', 'Device': 'dongle0', 'GSMRegistrationStatus': 'Registered, home network',
        'RSSI': '18, -77 dBm', 'ProviderName': 'MTS', 'SMSJournalVersion': '1', 'SMSJournalStatus': 'ok'}
LOST = {**HOME, 'GSMRegistrationStatus': 'Not registered, but searching', 'RSSI': '99, Unknown'}
SENSOR_PATH = '/api/states/sensor.phonelet_modem'  # pushed by the monitor directly, not through the outbox


class HttpApis:
    """The Telegram Bot API and the Home Assistant REST API behind an httpx.MockTransport.

    Records (path, JSON body) of every notification request per backend; answers 500 to a
    backend in `failing`.
    """

    def __init__(self):
        self.requests = {'telegram': [], 'homeassistant': []}
        self.failing = set()

    def __call__(self, request):
        backend = 'telegram' if request.url.host == 'api.telegram.org' else 'homeassistant'
        if request.url.path == SENSOR_PATH:
            return httpx.Response(200, json={})
        self.requests[backend].append((request.url.path, json.loads(request.content)))
        if backend in self.failing:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={'ok': True, 'result': {}} if backend == 'telegram' else [])


class FakeAmi:
    """Answers the monitor's DongleShowDevices and records SIP MessageSend actions."""

    def __init__(self, **_kwargs):
        self.loop = asyncio.get_running_loop()
        self.connected = self.authenticated = True
        self.entry = dict(HOME)
        self.messages = []
        self.reject_messages = False

    async def start(self):
        await asyncio.Event().wait()

    def stop(self):
        pass

    async def send_action(self, action):
        if action['Action'] == 'DongleShowDevices':
            return [dict(self.entry)]
        assert action['Action'] == 'MessageSend'
        self.messages.append(base64.b64decode(action['Base64Body']).decode())
        return {'Response': 'Error' if self.reject_messages else 'Success'}


@pytest.fixture
async def flow(tmp_path, monkeypatch):
    recorded = []

    async def fake_delivery_loop(db, backends):
        recorded.append((db, list(backends)))
        await asyncio.Event().wait()

    apis = HttpApis()
    # The notifiers' own start() builds the client; only its transport is replaced.
    monkeypatch.setattr(httpx, 'AsyncClient',
                        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(apis)))
    monkeypatch.setattr(main, 'AmiClient', FakeAmi)
    monkeypatch.setattr(main, 'delivery_loop', fake_delivery_loop)
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake',
                      telegram_token='bot-token', telegram_chat_id='4242',
                      ha_url='http://ha.invalid', ha_token='fake',
                      ingress_poll_interval=0.05,
                      modem_poll_interval=3600)  # only the startup poll; the test refreshes by hand
    (tmp_path / 'cdr' / 'phonelet.csv').touch()
    app = main.create_application(config)
    async with app.router.lifespan_context(app):
        monitor = app.state.modem_monitor
        for _ in range(500):  # the startup poll and the delivery tasks start on the first loop turns
            if len(recorded) == 3 and monitor.status['availability'] == 'available':
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail('application did not finish starting')
        assert all(db is app.state.db for db, _ in recorded)
        by_id = {backend.backend_id: backend for _, backends in recorded for backend in backends}
        # One delivery loop per backend: a slow one cannot hold the others back.
        assert [len(backends) for _, backends in recorded] == [1, 1, 1]
        assert set(by_id) == {'telegram', 'homeassistant', 'sip'}
        assert isinstance(by_id['telegram'], TelegramNotifier)
        assert isinstance(by_id['homeassistant'], HomeAssistantNotifier)
        assert isinstance(by_id['sip'], SipDelivery)
        backends = list(by_id.values())
        yield SimpleNamespace(
            app=app, db=app.state.db, ami=app.state.ami_client, monitor=monitor, apis=apis,
            telegram=apis.requests['telegram'], homeassistant=apis.requests['homeassistant'],
            deliver=lambda: deliver_pending(app.state.db, backends))


async def stats(db):
    return {(row['backend'], row['status']): row['count'] for row in await db.outbox_stats()}


async def due_again(db):
    await db._conn.execute('UPDATE outbox SET next_attempt=0')
    await db._conn.commit()


def test_every_accepted_method_has_a_delivery_path():
    # BaseNotifier.deliver dispatches by method name: a channel that keeps it needs a notify_* per accepted method.
    for channel in registry.CHANNELS:
        for method in channel.accepted_methods:
            assert (inspect.iscoroutinefunction(getattr(channel, method, None))
                    or channel.deliver is not BaseNotifier.deliver), (channel, method)


async def test_notifier_methods_accept_queued_payloads_and_sip_gets_only_sms(flow):
    # Produce the payloads through db.py, modem_status.py and main.py, rather than
    # copying their keyword lists into the contract test.
    await store_sms(flow.db, '+100', 'hello')
    await flow.db.insert_call('+200', 0, 'missed', source_id='contract:call', timestamp='2026-09-25T12:00:00+00:00')
    await flow.app.state.runtime.on_sms_failed('contract:failed', '+300', 'i')
    flow.ami.connected = False
    await flow.monitor.refresh()
    flow.ami.connected = True
    flow.ami.entry = dict(LOST)
    await flow.monitor.refresh()
    flow.ami.entry = {**HOME, 'SMSJournalStatus': 'error'}
    for _ in range(SMS_JOURNAL_ALERT_POLLS):
        await flow.monitor.refresh()

    jobs = await flow.db._all('SELECT backend,method,payload FROM outbox ORDER BY id')
    payloads = [job for job in jobs if job['backend'] == 'telegram']
    for notifier_type in (BaseNotifier, TelegramNotifier, HomeAssistantNotifier):
        for method in NotificationMethod:
            if method == NotificationMethod.SMS and notifier_type is not HomeAssistantNotifier:
                continue  # no notify_sms: TelegramNotifier.deliver sends SMS jobs itself
            handler = getattr(notifier_type, method)
            assert inspect.iscoroutinefunction(handler), (notifier_type, method)
            assert any(job['method'] == method for job in payloads), method
            for job in payloads:
                if job['method'] == method:
                    inspect.signature(handler).bind(None, **json.loads(job['payload']))
    telegram_sms = next(job for job in payloads if job['method'] == NotificationMethod.SMS)
    telegram = next(n for n in flow.app.state.runtime.notifiers if n.backend_id == 'telegram')
    with patch.object(telegram, '_send', AsyncMock(return_value=True)) as send:
        assert await telegram.deliver(NotificationMethod.SMS, json.loads(telegram_sms['payload']), AsyncMock())
    send.assert_awaited_once_with('SMS from +100:\nhello')

    # Database._enqueue must route every event to the alert backends, and only
    # SMS to SIP, whose dispatcher rejects every other NotificationMethod.
    for backend in ('telegram', 'homeassistant'):
        assert {job['method'] for job in jobs if job['backend'] == backend} == set(NotificationMethod)
    sip_jobs = [job for job in jobs if job['backend'] == 'sip']
    assert [job['method'] for job in sip_jobs] == [NotificationMethod.SMS]
    sip = next(n for n in flow.app.state.runtime.notifiers if n.backend_id == 'sip')
    payload = json.loads(sip_jobs[0]['payload'])
    assert await sip.deliver(NotificationMethod.SMS, payload, AsyncMock())
    for method in NotificationMethod:
        if method != NotificationMethod.SMS:
            job = next(job for job in jobs if job['method'] == method)
            with pytest.raises(ValueError, match='SIP delivery supports SMS only'):
                await sip.deliver(method, json.loads(job['payload']), AsyncMock())
    assert flow.ami.messages == ['hello']


TELEGRAM_PATH = '/botbot-token/sendMessage'


def telegram(text):
    return (TELEGRAM_PATH, {'chat_id': '4242', 'text': text,
                            'link_preview_options': {'is_disabled': True}})


def ha(event, **body):
    return ('/api/events/' + event, body)


LOST_TELEGRAM = telegram('SIM network lost (was: MTS, signal rssi: 99)')
LOST_HA = ha('sim_network_lost', operator='MTS', signal_rssi=99)
SMS_TELEGRAM = telegram('SMS from +100:\nhello')
SMS_HA = ha('sim_sms_received', sender='+100', text='hello')


async def test_alert_sms_and_call_reach_each_backend_api_once(flow):
    assert await stats(flow.db) == {}  # the startup poll found a registered modem: no alert
    flow.ami.entry = dict(LOST)
    await flow.monitor.refresh()
    await store_sms(flow.db, '+100', 'hello')
    # SIP carries SMS only: an alert must never be queued for it (SipDelivery rejects alerts).
    assert await stats(flow.db) == {('telegram', 'pending'): 2, ('homeassistant', 'pending'): 2,
                                    ('sip', 'pending'): 1}
    await flow.deliver()
    assert flow.telegram == [LOST_TELEGRAM, SMS_TELEGRAM]
    assert flow.homeassistant == [LOST_HA, SMS_HA]
    assert flow.ami.messages == ['hello']
    assert await stats(flow.db) == {('telegram', 'delivered'): 2, ('homeassistant', 'delivered'): 2,
                                    ('sip', 'delivered'): 1}
    # Recovery is a new alert for the alert backends, delivered exactly once; a call and a
    # failed outgoing SMS go to the alert backends only.
    flow.ami.entry = dict(HOME)
    await flow.monitor.refresh()
    await flow.db.insert_call('+200', 0, 'missed', source_id='cdr:1', timestamp='2026-09-25T12:00:00+00:00')
    await flow.db.enqueue_notification('notify_sms_failed', {'recipient': '+300', 'reason': 'i'},
                                       event_key='sms-report:1')
    await flow.deliver()
    await flow.deliver()
    assert flow.telegram[2:] == [telegram('SIM network restored (MTS, signal rssi: 18)'),
                                 telegram('Missed call from +200'),
                                 telegram('Outgoing SMS to +300 was not sent or only partly sent '
                                          '(modem send error)')]
    assert flow.homeassistant[2:] == [
        ha('sim_network_restored', operator='MTS', signal_rssi=18),
        ha('sim_call_received', caller='+200', status='missed', duration=0),
        ha('sim_health_alert', message='Outgoing SMS to +300 was not sent or only partly sent (modem send error)')]
    assert flow.ami.messages == ['hello']
    assert {row['status'] for row in await flow.db.outbox_stats()} == {'delivered'}


@pytest.mark.parametrize('failing', ['telegram', 'homeassistant', 'sip'])
async def test_one_failing_backend_does_not_block_or_repeat_the_others(flow, failing):
    flow.ami.entry = dict(LOST)
    await flow.monitor.refresh()
    await store_sms(flow.db, '+100', 'hello')
    if failing == 'sip':
        flow.ami.reject_messages = True
    else:
        flow.apis.failing.add(failing)

    await flow.deliver()
    jobs = {'telegram': 2, 'homeassistant': 2, 'sip': 1}
    assert await stats(flow.db) == {**{(name, 'delivered'): count for name, count in jobs.items()
                                       if name != failing},
                                    (failing, 'pending'): jobs[failing]}
    # Every job was attempted, whatever its neighbours did.
    assert flow.telegram == [LOST_TELEGRAM, SMS_TELEGRAM]
    assert flow.homeassistant == [LOST_HA, SMS_HA]
    assert flow.ami.messages == ['hello']
    assert {row['backend']: row['attempts'] for row in await flow.db.outbox_stats()
            if row['status'] == 'pending'} == {failing: 1}

    # The failing backend recovers: only it retries, the delivered ones are not sent again.
    flow.apis.failing.clear()
    flow.ami.reject_messages = False
    await due_again(flow.db)
    await flow.deliver()
    assert await stats(flow.db) == {('telegram', 'delivered'): 2, ('homeassistant', 'delivered'): 2,
                                    ('sip', 'delivered'): 1}
    assert flow.telegram == [LOST_TELEGRAM, SMS_TELEGRAM] * (2 if failing == 'telegram' else 1)
    assert flow.homeassistant == [LOST_HA, SMS_HA] * (2 if failing == 'homeassistant' else 1)
    assert flow.ami.messages == ['hello'] * (2 if failing == 'sip' else 1)


async def test_ready_with_the_real_monitor_and_ingress(flow):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=flow.app), base_url='http://test',
                                 headers={'X-API-Key': 'fake'}) as client:
        for _ in range(200):  # the first ingress pass runs on the first loop turns
            response = await client.get('/ready')
            if response.status_code == 200:
                break
            await asyncio.sleep(0.01)
        body = response.json()
        assert response.status_code == 200, body
        assert body['software_ready'] is True and body['modem_ready'] is True
        assert body['driver_ingress'] == {'version': '1', 'status': 'ok'}
        assert body['ingress_errors'] == 0 and body['ingress_fresh'] is True
        # Lost registration: the software stays ready, the modem does not.
        flow.ami.entry = dict(LOST)
        await flow.monitor.refresh()
        response = await client.get('/ready')
        assert response.status_code == 503
        assert response.json()['software_ready'] is True and response.json()['modem_ready'] is False


async def test_hung_telegram_does_not_delay_the_sms_to_the_phone(tmp_path, monkeypatch):
    # The real delivery loops: Telegram's request hangs (a board without internet); the SIP
    # copy of the SMS must not wait for its 30 s delivery timeout.
    hung = asyncio.Event()

    async def apis(request):
        hung.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(httpx, 'AsyncClient',
                        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(apis)))
    monkeypatch.setattr(main, 'AmiClient', FakeAmi)
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake',
                      telegram_token='bot-token', telegram_chat_id='4242', modem_poll_interval=3600)
    app = main.create_application(config)
    async with app.router.lifespan_context(app):
        await store_sms(app.state.db, '+100', 'hello')
        async with asyncio.timeout(5):
            await hung.wait()
            while await stats(app.state.db) != {('telegram', 'sending'): 1, ('sip', 'delivered'): 1}:
                await asyncio.sleep(0.01)
        assert app.state.ami_client.messages == ['hello']


async def test_announced_modem_state_survives_restarts(tmp_path, monkeypatch):
    # The board's modem hangs and the service restarts meanwhile: the last announced state is in
    # the database, so a restart neither repeats network_lost nor misses the restoration.
    async def idle_delivery(db, backends):
        await asyncio.Event().wait()  # jobs stay in the outbox for the test to read

    monkeypatch.setattr(main, 'delivery_loop', idle_delivery)
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake',
                      telegram_token='bot-token', telegram_chat_id='4242', modem_poll_interval=3600)

    async def start_with(entry):
        class Ami(FakeAmi):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.entry = dict(entry)
        monkeypatch.setattr(main, 'AmiClient', Ami)
        app = main.create_application(config)
        async with app.router.lifespan_context(app):
            monitor = app.state.modem_monitor
            for _ in range(500):  # the startup poll has loaded the stored state and sampled
                if monitor.status['availability'] == 'available':
                    break
                await asyncio.sleep(0.01)
            await monitor.refresh()  # waits for the startup poll to finish its alerts
            return [row['method'] for row in await app.state.db._all(
                "SELECT method FROM outbox WHERE backend='telegram' ORDER BY id")]

    assert await start_with(HOME) == []  # the very first poll: no false restored
    assert await start_with(LOST) == ['notify_network_lost']
    assert await start_with(LOST) == ['notify_network_lost']  # restart while lost: no duplicate
    assert await start_with(HOME) == ['notify_network_lost', 'notify_network_restored']
    assert await start_with(HOME) == ['notify_network_lost', 'notify_network_restored']
