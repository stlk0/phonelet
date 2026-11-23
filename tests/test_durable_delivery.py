import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from phonelet.config import Settings
from phonelet.constants import NotificationMethod
from phonelet.db import Database
from phonelet.notification import deliver_pending, delivery_loop
from phonelet.notifiers.base import BaseNotifier
from phonelet.notifiers.sip import SipDelivery, split_utf8
from tests.helpers import DeliversByMethod, deliver_sms, store_sms


@pytest.mark.parametrize('configured, expected', [('', 'ami.example'), ('sip.example', 'sip.example')])
def test_sip_domain_uses_runtime_settings(configured, expected):
    config = Settings(sip_domain=configured, ami_host='ami.example',
                      sip_message_destination='pjsip:phone')
    sip = SipDelivery.from_settings(config, ami=AsyncMock())
    assert sip.domain == expected


@pytest.mark.parametrize('destination, expected_to', [
    ('pjsip:phone', 'sip:phone@192.0.2.1'),
    ('pjsip:desk-2', 'sip:desk-2@192.0.2.1'),
    ('pjsip:desk/sip:desk@192.0.2.9', 'sip:phone@192.0.2.1'),
    ('pjsip:desk@192.0.2.9', 'sip:phone@192.0.2.1'),
])
async def test_sip_to_uses_endpoint_of_destination(destination, expected_to):
    ami = AsyncMock()
    ami.send_action.return_value = {'Response': 'Success'}
    sip = SipDelivery(ami, '192.0.2.1', destination=destination)
    assert await deliver_sms(sip, '+15550100', 'hi')
    action = ami.send_action.call_args.args[0]
    assert (action['Destination'], action['To']) == (destination, expected_to)


class Fake(DeliversByMethod):
    def __init__(self, name, success=True):
        self.backend_id = name
        self.notify_sms = AsyncMock(return_value=success)
        self.notify_health = AsyncMock(return_value=success)


async def test_base_notifier_delivers_unavailable_and_next_alert_in_order(db):
    class MinimalNotifier(BaseNotifier):
        backend_id = 'minimal'

        def __init__(self):
            super().__init__()
            self.messages = []

        async def notify_sms(self, sender, text):
            raise AssertionError('unexpected SMS')

        async def notify_call(self, caller, status, duration=0):
            raise AssertionError('unexpected call')

        async def notify_health(self, message):
            self.messages.append(message)
            return True

        async def notify_network_lost(self, operator, signal_rssi):
            raise AssertionError('unexpected network loss')

        async def notify_network_restored(self, operator, signal_rssi):
            raise AssertionError('unexpected network restoration')

    notifier = MinimalNotifier()
    db.backends = (notifier.backend_id,)
    await db.enqueue_notification(NotificationMethod.MODEM_UNAVAILABLE, {'reason': 'ami_disconnected'})
    await db.enqueue_notification(NotificationMethod.HEALTH, {'message': 'next alert'})
    task = asyncio.create_task(delivery_loop(db, [notifier]))
    try:
        async with asyncio.timeout(5):
            while await db.outbox_stats() != [
                {'backend': 'minimal', 'status': 'delivered', 'attempts': 1, 'count': 2},
            ]:
                await asyncio.sleep(0.01)
        assert notifier.messages == ['Modem status unavailable (ami_disconnected)', 'next alert']
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_independent_delivery_and_unlimited_retry(db):
    db.backends = ('telegram', 'homeassistant')
    good, bad = Fake('telegram'), Fake('homeassistant', False)
    await store_sms(db, '+7', 'hello', source_id='one')
    await deliver_pending(db, [good, bad])
    assert good.notify_sms.await_count == bad.notify_sms.await_count == 1
    for _ in range(12):
        await db._conn.execute('UPDATE outbox SET next_attempt=0')
        await db._conn.commit()
        await deliver_pending(db, [good, bad])
    assert good.notify_sms.await_count == 1
    assert bad.notify_sms.await_count == 13
    bad.notify_sms.return_value = True
    await db._conn.execute('UPDATE outbox SET next_attempt=0')
    await db._conn.commit()
    await deliver_pending(db, [good, bad])
    assert {row['status'] for row in await db.outbox_stats()} == {'delivered'}


async def test_atomic_claim_across_connections_and_crash_lease(db):
    db.backends = ('telegram',)
    await store_sms(db, '+7', 'one')
    second = Database(db.db_path)
    await second.init()
    try:
        jobs = await asyncio.gather(db.claim_delivery(('telegram',), now=100),
                                   second.claim_delivery(('telegram',), now=100))
        assert sum(j is not None for j in jobs) == 1
        old = next(j for j in jobs if j)
        assert await second.claim_delivery(('telegram',), now=219) is None
        new = await second.claim_delivery(('telegram',), now=221)
        assert old['owner'] != new['owner']
        await db.finish_delivery(old, True)
        assert (await db.outbox_stats())[0]['status'] == 'sending'
        with pytest.raises(RuntimeError):
            await db.checkpoint_delivery(old, {'sender': '+7', 'text': 'one', '_next_chunk': 1})
        await second.finish_delivery(new, True)
        assert (await db.outbox_stats())[0]['status'] == 'delivered'
    finally:
        await second.close()


async def test_health_restart_and_source_dedup(tmp_path):
    path = str(tmp_path/'db')
    db = Database(path, ('telegram', 'homeassistant'))
    await db.init()
    await db.enqueue_notification('notify_health', {'message': 'offline'}, event_key='transition:1')
    await store_sms(db, '+7', 'same', source_id='source1')
    await store_sms(db, '+7', 'same', source_id='source1')
    await db.close()
    db = Database(path, ('telegram', 'homeassistant'))
    await db.init()
    try:
        backends = [Fake('telegram'), Fake('homeassistant')]
        await deliver_pending(db, backends)
        for b in backends:
            b.notify_health.assert_awaited_once_with(message='offline')
            b.notify_sms.assert_awaited_once()
        assert len(await db.list_sms()) == 1
    finally:
        await db.close()


@pytest.mark.parametrize('method,payload,event_key', [
    (NotificationMethod.HEALTH, {'message': 'Invalid journal record'}, 'sms-journal-invalid:synthetic'),
    (NotificationMethod.SMS_FAILED, {'recipient': '+15550000001', 'reason': 'i'}, 'sms-report:synthetic'),
])
@pytest.mark.parametrize('backends,history,expected_backends', [
    (('telegram', 'sip'), False, ['telegram']),
    (('sip',), False, []),
    ((), False, []),
    (('telegram', 'sip'), True, []),
    (('telegram', 'homeassistant', 'sip'), False, ['telegram', 'homeassistant']),
])
async def test_alert_is_new_only_on_first_encounter_across_restart(
        tmp_db_path, method, payload, event_key, backends, history, expected_backends):
    # Replays log a WARNING only for the first encounter, even without accepting channels.
    db = Database(tmp_db_path, backends, backend_methods={'sip': SipDelivery.accepted_methods})
    await db.init()
    try:
        assert await db.enqueue_notification(method, payload, event_key=event_key, history=history) is True
        assert await db.enqueue_notification(method, payload, event_key=event_key, history=history) is False
        jobs = await db._all('SELECT * FROM outbox ORDER BY id')
        assert [job['backend'] for job in jobs] == expected_backends
    finally:
        await db.close()

    db = Database(tmp_db_path, ('telegram', 'homeassistant', 'sip'),
                  backend_methods={'sip': SipDelivery.accepted_methods})
    await db.init()
    try:
        # A historical alert stays suppressed even when replay no longer marks it as history.
        assert await db.enqueue_notification(method, payload, event_key=event_key) is False
        assert await db._all('SELECT * FROM outbox ORDER BY id') == jobs
    finally:
        await db.close()


@pytest.mark.parametrize('history', [False, True])
async def test_alert_commit_failure_rolls_back_key_and_jobs(db, monkeypatch, history):
    db.backends = ('telegram', 'homeassistant')
    real_commit = db._conn.commit
    monkeypatch.setattr(db._conn, 'commit', AsyncMock(side_effect=sqlite3.OperationalError('disk full')))
    with pytest.raises(sqlite3.OperationalError, match='disk full'):
        await db.enqueue_notification(NotificationMethod.HEALTH, {'message': 'synthetic alert'},
                                      event_key='alert:synthetic', history=history)
    monkeypatch.setattr(db._conn, 'commit', real_commit)
    assert await db._all('SELECT * FROM alert_events') == []
    assert await db.outbox_stats() == []
    assert await db.enqueue_notification(NotificationMethod.HEALTH, {'message': 'synthetic alert'},
                                         event_key='alert:synthetic', history=history) is True
    assert await db._all('SELECT * FROM alert_events') == [{'event_key': 'alert:synthetic'}]
    assert sum(row['count'] for row in await db.outbox_stats()) == (0 if history else 2)


async def test_commit_failure_retains_source_and_rolls_back_outbox(db, monkeypatch):
    db.backends = ('telegram',)
    real = db._conn.commit
    monkeypatch.setattr(db._conn, 'commit', AsyncMock(side_effect=sqlite3.OperationalError('disk full')))
    with pytest.raises(sqlite3.OperationalError):
        await store_sms(db, '+7', 'hello', source_id='same')
    monkeypatch.setattr(db._conn, 'commit', real)
    assert await db.list_sms() == []
    assert await db.outbox_stats() == []
    await store_sms(db, '+7', 'hello', source_id='same')
    assert len(await db.list_sms()) == 1


async def test_delivery_checkpoint_restart(db):
    db.backends = ('telegram',)
    await store_sms(db, '+7', 'long')
    sent = []
    class Chunked:
        backend_id = 'telegram'
        fail = True
        async def deliver(self, method, payload, checkpoint):
            for i in range(payload.get('_next_chunk', 0), 3):
                if i == 1 and self.fail:
                    self.fail = False
                    return False
                sent.append(i)
                await checkpoint({**payload, '_next_chunk': i + 1})
            return True
    notifier = Chunked()
    await deliver_pending(db, [notifier])
    await db._conn.execute('UPDATE outbox SET next_attempt=0')
    await db._conn.commit()
    await deliver_pending(db, [notifier])
    assert sent == [0, 1, 2]


@pytest.mark.parametrize('sender, expected_from', [
    ('T-Bank', '"T-Bank" <sip:T-Bank@192.0.2.1>'),
    ('+15550100', '"+15550100" <sip:+15550100@192.0.2.1>'),
    ('Test "A"\\B\\', r'"Test \"A\"\\B\\" <sip:TestAB@192.0.2.1>'),
    ('A\\\\B', r'"A\\\\B" <sip:AB@192.0.2.1>'),
    (' \r\nA\tB\x00\x7f\x85< C>;,\u2028\u2029\u202e ', '"AB< C>;," <sip:ABC@192.0.2.1>'),
    ('Тест-Банк', '"Тест-Банк" <sip:-@192.0.2.1>'),
    ('Банк', '"Банк" <sip:anonymous@192.0.2.1>'),
    ('', 'sip:anonymous@192.0.2.1'),
    (' \r\n\t\x00\x7f\x85\u2028\u2029\u202e\ud800 ', 'sip:anonymous@192.0.2.1'),
    ('Я' * 81, f'"{"Я" * 80}" <sip:anonymous@192.0.2.1>'),
    ('Б' * 79 + '"\\', f'"{"Б" * 79}' + r'\"" <sip:anonymous@192.0.2.1>'),
])
async def test_sip_sender_display_name_on_every_chunk(sender, expected_from):
    ami = AsyncMock()
    ami.send_action.return_value = {'Response': 'Success'}
    sip = SipDelivery(ami, '192.0.2.1')
    assert await deliver_sms(sip, sender, 'x' * 1300)
    assert [call.args[0]['From'] for call in ami.send_action.call_args_list] == [expected_from] * 3


async def test_sip_safe_body_and_chunk_checkpoint():
    import base64
    ami = AsyncMock()
    ami.send_action.side_effect = [{'Response': 'Success'}, {'Response': 'Error'},
                                   {'Response': 'Success'}, {'Response': 'Success'}]
    sender = '+7\r\nAction: bad'
    text = '😀"comma,\n' * 150
    payload = {'sender': sender, 'text': text}
    async def checkpoint(updated):
        payload.update(updated)
    sip = SipDelivery(ami, 'localhost')
    assert not await sip.deliver('notify_sms', payload, checkpoint)
    assert payload['_next_chunk'] == 1
    ami.send_action.side_effect = None
    ami.send_action.return_value = {'Response': 'Success'}
    assert await sip.deliver('notify_sms', payload, checkpoint)
    calls = ami.send_action.call_args_list
    bodies = [base64.b64decode(c.args[0]['Base64Body']).decode() for c in calls]
    assert bodies[0] != bodies[2]  # acknowledged first chunk was not resent
    assert ''.join(split_utf8(text)) == text
    for c in calls:
        assert c.args[0]['From'] == '"+7Action: bad" <sip:+7Actionbad@localhost>'
        assert '\r' not in c.args[0]['From'] and '\n' not in c.args[0]['From']
        assert len(c.args[0]['Base64Body']) < 1000


@pytest.mark.parametrize('next_chunk', [True, False, -1, 4])
async def test_sip_rejects_invalid_chunk_checkpoint(next_chunk):
    ami = AsyncMock()
    sip = SipDelivery(ami, 'localhost')
    payload = {'sender': '+7', 'text': 'x' * 1300, '_next_chunk': next_chunk}  # three chunks
    with pytest.raises(ValueError, match='Invalid SIP chunk checkpoint'):
        await sip.deliver('notify_sms', payload, AsyncMock())
    ami.send_action.assert_not_awaited()


async def test_history_reader_waits_for_transaction_commit(db,monkeypatch):
    pending, release = asyncio.Event(), asyncio.Event()
    original = db._conn.commit
    async def slow_commit():
        pending.set()
        await release.wait()
        await original()
    monkeypatch.setattr(db._conn,'commit',slow_commit)
    writer = asyncio.create_task(store_sms(db, '+7','committed'))
    await pending.wait()
    reader = asyncio.create_task(db.list_sms())
    await asyncio.sleep(0)
    assert not reader.done()
    release.set()
    await writer
    assert (await reader)[0]['text'] == 'committed'


async def test_history_reader_does_not_see_rolled_back_record(db, monkeypatch):
    pending, release = asyncio.Event(), asyncio.Event()

    async def failed_commit():
        pending.set()
        await release.wait()
        raise sqlite3.OperationalError('synthetic commit failure')

    monkeypatch.setattr(db._conn, 'commit', failed_commit)
    writer = asyncio.create_task(store_sms(db, '+15550000001', 'rolled back'))
    reader = waiter = None
    try:
        async with asyncio.timeout(5):
            await pending.wait()  # The row exists on the shared connection, but is uncommitted.
            waiting_for_lock = asyncio.Event()
            acquire = db._lock.acquire

            async def observed_acquire():
                waiting_for_lock.set()
                return await acquire()

            monkeypatch.setattr(db._lock, 'acquire', observed_acquire)
            reader = asyncio.create_task(db.list_sms())
            waiter = asyncio.create_task(waiting_for_lock.wait())
            # A locked reader waits; an unlocked reader finishes against the dirty row.
            await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
            release.set()
            with pytest.raises(sqlite3.OperationalError, match='synthetic commit failure'):
                await writer
            assert await reader == []
    finally:
        release.set()
        if waiter is not None:
            waiter.cancel()
        await asyncio.gather(*(task for task in (writer, reader, waiter) if task is not None),
                             return_exceptions=True)


async def test_network_recovery_waits_for_outage_retry_across_restart(db,monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('phonelet.db.time.time',lambda: clock[0])
    db.backends = ('telegram','homeassistant')
    attempts = {'telegram':[],'homeassistant':[]}
    accepted = {'telegram':[],'homeassistant':[]}

    class Alerts(DeliversByMethod):
        def __init__(self,name):
            self.backend_id = name
            self.fail = name == 'telegram'

        async def notify_network_lost(self,**payload):
            attempts[self.backend_id].append('lost')
            if self.fail:
                self.fail = False
                return False
            accepted[self.backend_id].append('lost')
            return True

        async def notify_network_restored(self,**payload):
            attempts[self.backend_id].append('restored')
            accepted[self.backend_id].append('restored')
            return True

        async def notify_sms(self,**payload):
            attempts[self.backend_id].append('sms')
            return True

    targets = [Alerts('telegram'),Alerts('homeassistant')]
    payload = {'operator':'test','signal_rssi':10}
    await db.enqueue_notification('notify_network_lost',payload,event_key='lost')
    await db.enqueue_notification('notify_network_restored',payload,event_key='restored')
    await store_sms(db, '+7','unrelated')
    await deliver_pending(db,targets)
    assert attempts['telegram'] == ['lost','sms']
    assert accepted['homeassistant'] == ['lost','restored']
    assert accepted['telegram'] == []

    await db.close()
    await db.init()
    clock[0] = 101.0  # persisted backoff still blocks recovery after restart
    await deliver_pending(db,targets)
    assert attempts['telegram'] == ['lost','sms']
    clock[0] = 103.0
    await deliver_pending(db,targets)
    assert attempts['telegram'] == ['lost','sms','lost','restored']
    assert accepted['telegram'] == ['lost','restored']
    assert accepted['homeassistant'] == ['lost','restored']
    assert all(row['status'] == 'delivered' for row in await db.outbox_stats())


async def test_active_and_expired_alert_leases_preserve_order_across_workers(db):
    db.backends = ('telegram',)
    await db.enqueue_notification('notify_modem_unavailable',{'reason':'offline'})
    await db.enqueue_notification('notify_network_restored',{'operator':'test','signal_rssi':10})
    await db.enqueue_notification('notify_health',{'message':'latest'})
    await store_sms(db, '+7','unrelated')
    second = Database(db.db_path)
    await second.init()
    try:
        first = await db.claim_delivery(('telegram',),now=100)
        assert first['method'] == 'notify_modem_unavailable'
        sms = await second.claim_delivery(('telegram',),now=100)
        assert sms['method'] == 'notify_sms'
        await second.finish_delivery(sms,True,now=100)
        assert await second.claim_delivery(('telegram',),now=100) is None
        retry = await second.claim_delivery(('telegram',),now=221)
        assert retry['id'] == first['id']  # expired outage reclaimed before recovery
        await db.finish_delivery(first,True,now=221)  # stale owner cannot release barrier
        assert await db.claim_delivery(('telegram',),now=221) is None
        await second.finish_delivery(retry,True,now=221)
        restored = await db.claim_delivery(('telegram',),now=221)
        assert restored['method'] == 'notify_network_restored'
        await db.finish_delivery(restored,True,now=221)
        assert (await second.claim_delivery(('telegram',),now=221))['method'] == 'notify_health'
    finally:
        await second.close()


class Raising(DeliversByMethod):
    """notify_sms raises once, then succeeds; notify_health always succeeds."""
    def __init__(self, error):
        self.backend_id, self.error, self.sent = 'telegram', error, []

    async def notify_sms(self, sender, text):
        if self.error:
            error, self.error = self.error, None
            raise error
        self.sent.append(text)
        return True

    async def notify_health(self, message):
        self.sent.append(message)
        return True


async def test_backend_that_raises_is_retried_and_does_not_stop_the_batch(db):
    db.backends = ('telegram',)
    backend = Raising(ConnectionError('offline'))
    await store_sms(db, '+7', 'first')
    await db.enqueue_notification('notify_health', {'message': 'later'}, event_key='health:1')
    await deliver_pending(db, [backend])
    assert backend.sent == ['later']  # the next job of the pass still went out
    job = await db._one("SELECT status,attempts,next_attempt FROM outbox WHERE event_key='sms:1'")
    assert job['status'] == 'pending' and job['attempts'] == 1 and job['next_attempt'] > time.time()
    await db._conn.execute('UPDATE outbox SET next_attempt=0')
    await db._conn.commit()
    await deliver_pending(db, [backend])
    assert backend.sent == ['later', 'first']
    assert {row['status'] for row in await db.outbox_stats()} == {'delivered'}


async def test_sip_backend_with_ami_down_is_retried(db):
    # The usual case "SMS arrived, AMI did not": AmiClient.send_action raises ConnectionError.
    db.backends = ('sip',)
    ami = AsyncMock()
    ami.send_action.side_effect = ConnectionError('AMI is not authenticated')
    await store_sms(db, '+7', 'hello')
    await deliver_pending(db, [SipDelivery(ami, 'phone.local')])
    assert [(r['status'], r['attempts']) for r in await db.outbox_stats()] == [('pending', 1)]
    ami.send_action.side_effect = None
    ami.send_action.return_value = {'Response': 'Success'}
    await db._conn.execute('UPDATE outbox SET next_attempt=0')
    await db._conn.commit()
    await deliver_pending(db, [SipDelivery(ami, 'phone.local')])
    assert [r['status'] for r in await db.outbox_stats()] == ['delivered']


async def test_hung_backend_times_out_and_is_retried(db, monkeypatch):
    monkeypatch.setattr('phonelet.notification.DELIVERY_TIMEOUT_SECONDS', 0.05)
    db.backends = ('telegram',)

    class Hung(DeliversByMethod):
        backend_id = 'telegram'
        async def notify_sms(self, sender, text):
            await asyncio.Event().wait()

    await store_sms(db, '+7', 'hello')
    async with asyncio.timeout(5):
        await deliver_pending(db, [Hung()])
    job = await db._one('SELECT status,attempts,owner FROM outbox')
    assert job == {'status': 'pending', 'attempts': 1, 'owner': None}


async def test_cancelled_transaction_start_leaves_no_open_transaction(db):
    """A wait_for timeout can cancel a writer while its BEGIN is in flight; later writers must still work."""
    for steps in range(6):  # cancel at each of the first event-loop steps of the transaction
        task = asyncio.create_task(store_sms(db, '+15550000001', f'cancelled at step {steps}'))
        for _ in range(steps):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert await store_sms(db, '+15550000001', f'after step {steps}')  # no "transaction within a transaction"
        assert not db._conn.in_transaction
