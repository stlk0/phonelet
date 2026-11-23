"""A failed outgoing SMS: its marked CDR line -> durable alert, once per alert backend.

Real: main.Runtime (its CdrHandler, Database and backend list), IngressReplay and
deliver_pending. Fakes: recording notifiers. The CDR journal and the DB live in tmp_path;
no network, Asterisk or modem.
"""
import csv
import io
import logging
from unittest.mock import AsyncMock

import pytest

from phonelet import main
from phonelet.config import Settings
from phonelet.cdr_events import CdrHandler
from phonelet.ingress import IngressReplay
from phonelet.notification import deliver_pending
from tests.helpers import DeliversByMethod, cdr_line


def report_line(uid, userfield='phonelet-sms-report:i', channel='Local/report@dongle-incoming-00000002;1',
                source='+15550000001', app='Hangup', disposition='NO ANSWER'):
    # cdr_custom's phonelet.csv columns, as report@dongle-incoming leaves them in Asterisk 20.18.
    output = io.StringIO()
    csv.writer(output).writerow([uid, channel, app, source, disposition, '0', userfield, '2026-10-22T11:17:50+0000'])
    return output.getvalue()


class Recorder(DeliversByMethod):
    def __init__(self, backend_id):
        self.backend_id, self.calls = backend_id, []

    def __getattr__(self, name):
        if not name.startswith('notify_'):
            raise AttributeError(name)

        async def notify(**payload):
            self.calls.append((name, payload))
            return True
        return notify


async def sms_failed_jobs(db):
    return await db._all("SELECT event_key,backend,payload,status FROM outbox "
                         "WHERE method='notify_sms_failed' ORDER BY id")


@pytest.mark.parametrize('telegram_enabled', [False, True])
async def test_failed_sms_replay_does_not_notify_new_channels(tmp_path, caplog, telegram_enabled):
    caplog.set_level(logging.INFO, logger='phonelet.cdr_events')
    path = tmp_path/'phonelet.csv'
    path.write_text(report_line('synthetic-failure', source='+15550000001'))
    config = Settings(db_path=str(tmp_path/'phonelet.db'), recordings_dir=str(tmp_path),
                      sms_ingress_dir=str(tmp_path), cdr_log_path=str(path), api_key='fake', ami_secret='fake',
                      telegram_token='fake' if telegram_enabled else '',
                      telegram_chat_id='fake' if telegram_enabled else '')
    jobs = None
    for start in range(2):
        if start:
            config.telegram_token = 'fake'
            config.telegram_chat_id = 'fake'
            config.ha_url = 'http://ha.invalid'
            config.ha_token = 'fake'
        runtime = main.Runtime(config)
        await runtime.db.init()
        try:
            consumer = IngressReplay(runtime.db, runtime.handler, tmp_path, path)
            await consumer.replay_once()
            assert consumer.errors == 0
            current_jobs = await sms_failed_jobs(runtime.db)
            if jobs is None:
                jobs = current_jobs
                assert [job['backend'] for job in jobs] == (['telegram'] if telegram_enabled else [])
            assert current_jobs == jobs
        finally:
            await runtime.db.close()
    # Even without an accepting channel, the second encounter is only INFO.
    assert [r.getMessage() for r in caplog.records
            if r.name == 'phonelet.cdr_events' and r.levelno >= logging.WARNING] == [
                'Outgoing SMS failed: reason=i']
    assert [r.getMessage() for r in caplog.records
            if r.name == 'phonelet.cdr_events' and r.levelno == logging.INFO] == [
                'Outgoing SMS failed: reason=i; alert already queued']


async def test_failed_sms_report_queued_once_per_alert_backend_across_restart(tmp_path):
    # main.Runtime wires the handler to the outbox; the second Runtime is a restart that
    # replays the same retained CDR journal on the same DB.
    path = tmp_path/'phonelet.csv'
    path.write_text(report_line('1792667870.4') +
                    report_line('1792667873.7', 'phonelet-sms-report:t') +
                    # unmarked report lines, as on the server before the marker (22.10 08:20)
                    report_line('1792600000.1', '', channel='Local/report@dongle-incoming-00000000;2',
                                source='', app='', disposition='FAILED') +
                    report_line('1792600000.2', ''))
    config = Settings(db_path=str(tmp_path/'phonelet.db'), recordings_dir=str(tmp_path),
                      sms_ingress_dir=str(tmp_path), cdr_log_path=str(path), api_key='fake', ami_secret='fake',
                      telegram_token='replay-tg-token', telegram_chat_id='fake', ha_url='http://ha.invalid',
                      ha_token='replay-ha-token')  # notifiers add their tokens to the log redaction set
    expected = [{'event_key': 'sms-report:' + uid, 'backend': backend, 'status': 'pending',
                 'payload': '{"recipient": "+15550000001", "reason": "%s"}' % reason}
                for uid, reason in (('1792667870.4', 'i'), ('1792667873.7', 't'))
                for backend in ('telegram', 'homeassistant')]
    recorders = [Recorder('telegram'), Recorder('homeassistant')]
    for start in range(2):
        runtime = main.Runtime(config)
        assert runtime.handler.on_sms_failed == runtime.on_sms_failed
        assert runtime.db.backends == ('telegram', 'homeassistant', 'sip')
        await runtime.db.init()
        try:
            consumer = IngressReplay(runtime.db, runtime.handler, tmp_path, path)
            await consumer.replay_once()
            assert consumer.errors == 0
            assert await sms_failed_jobs(runtime.db) == expected
            with path.open('a') as f:  # the same line again, and an incoming call
                f.write(report_line('1792667870.4') + cdr_line('1770000000.1'))
            await consumer.replay_once()
            assert await sms_failed_jobs(runtime.db) == expected
            assert len(await runtime.db.list_calls()) == 1
            await deliver_pending(runtime.db, recorders)
            expected = [{**job, 'status': 'delivered'} for job in expected]
        finally:
            await runtime.db.close()
    for recorder in recorders:
        assert [call for call in recorder.calls if call[0] == 'notify_sms_failed'] == [
            ('notify_sms_failed', {'recipient': '+15550000001', 'reason': reason}) for reason in ('i', 't')]
        assert [call[0] for call in recorder.calls].count('notify_call') == 1


async def test_rejected_sms_alerts_each_time_and_once_across_restart(tmp_path, caplog):
    # Two identical SMS the driver rejected before its queue, as the isolated Asterisk 20.18
    # wrote them: [sms-send] starts report@ with Originate() per SMS, so each has its own ;2
    # line and uniqueid. The Message channel that ran [sms-outbound] for both writes one
    # unmarked line at shutdown, with one uniqueid for every SMS: it is ignored.
    caplog.set_level(logging.DEBUG)
    path = tmp_path/'phonelet.csv'
    path.write_text(report_line('1792716012.2', 'phonelet-sms-report:r', 'Local/report@dongle-incoming-00000000;2') +
                    report_line('1792716014.5', 'phonelet-sms-report:r', 'Local/report@dongle-incoming-00000001;2') +
                    report_line('1792716012.0', '', 'Message/ast_msg_queue', '', 'Hangup', 'ANSWERED'))
    config = Settings(db_path=str(tmp_path/'phonelet.db'), recordings_dir=str(tmp_path),
                      sms_ingress_dir=str(tmp_path), cdr_log_path=str(path), api_key='fake', ami_secret='fake',
                      telegram_token='replay-tg-token', telegram_chat_id='fake', ha_url='http://ha.invalid',
                      ha_token='replay-ha-token')
    expected = [{'event_key': 'sms-report:' + uid, 'backend': backend, 'status': 'pending',
                 'payload': '{"recipient": "+15550000001", "reason": "r"}'}
                for uid in ('1792716012.2', '1792716014.5') for backend in ('telegram', 'homeassistant')]
    recorders = [Recorder('telegram'), Recorder('homeassistant')]
    for start in range(2):  # the second is a restart on the same DB and journal
        runtime = main.Runtime(config)
        await runtime.db.init()
        try:
            consumer = IngressReplay(runtime.db, runtime.handler, tmp_path, path)
            await consumer.replay_once()
            with path.open('a') as f:  # the same line again
                f.write(report_line('1792716014.5', 'phonelet-sms-report:r', 'Local/report@dongle-incoming-00000001;2'))
            await consumer.replay_once()
            assert consumer.errors == 0
            assert await sms_failed_jobs(runtime.db) == expected
            assert await runtime.db.list_calls() == []
            await deliver_pending(runtime.db, recorders)
            expected = [{**job, 'status': 'delivered'} for job in expected]
        finally:
            await runtime.db.close()
    for recorder in recorders:
        assert recorder.calls == [('notify_sms_failed', {'recipient': '+15550000001', 'reason': 'r'})] * 2
    # One WARNING per new alert; the repeated line and the restart's replay are INFO only.
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        'Outgoing SMS failed: reason=r'] * 2
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO
            and r.name == 'phonelet.cdr_events'] == ['Outgoing SMS failed: reason=r; alert already queued'] * 5
    assert '15550000001' not in caplog.text


async def test_marked_line_whose_alert_fails_to_queue_stays_in_the_journal(db, tmp_path):
    # A receiver that cannot queue the alert raises: the replay keeps the line and retries it.
    path = tmp_path/'phonelet.csv'
    path.write_text(report_line('1792667870.4'))
    handler = CdrHandler(AsyncMock(), AsyncMock(side_effect=RuntimeError('database is locked')))
    consumer = IngressReplay(db, handler, tmp_path, path)
    await consumer.replay_once()
    assert consumer.errors == 1
    consumer.cdr_handler.on_sms_failed = AsyncMock()
    await consumer.replay_once()
    assert consumer.errors == 0
    consumer.cdr_handler.on_sms_failed.assert_awaited_once_with(
        event_key='sms-report:1792667870.4', recipient='+15550000001', reason='i')


@pytest.mark.history_rebuild
async def test_failed_sms_report_in_first_pass_is_history(tmp_path):
    # main.Runtime passes the replay's history flag: time never decides.
    path = tmp_path/'phonelet.csv'
    path.write_text(report_line('1792667870.4'))  # 2026-10-22, before the database
    config = Settings(db_path=str(tmp_path/'phonelet.db'), recordings_dir=str(tmp_path),
                      sms_ingress_dir=str(tmp_path), cdr_log_path=str(path), api_key='fake', ami_secret='fake',
                      telegram_token='fake', telegram_chat_id='fake')
    runtime = main.Runtime(config)
    await runtime.db.init()
    try:
        replay = IngressReplay(runtime.db, runtime.handler, tmp_path, path)
        await replay.replay_once()
        assert replay.errors == 0 and await sms_failed_jobs(runtime.db) == []
        earlier = '2000-01-01T00:00:00+0000'
        with path.open('a') as stream:
            stream.write(report_line('1792667999.1').replace('2026-10-22T11:17:50+0000', earlier))
        await replay.replay_once()
        assert [job['event_key'] for job in await sms_failed_jobs(runtime.db)] == ['sms-report:1792667999.1']
        await IngressReplay(runtime.db, runtime.handler, tmp_path, path).replay_once()
        assert [job['event_key'] for job in await sms_failed_jobs(runtime.db)] == ['sms-report:1792667999.1']
    finally:
        await runtime.db.close()
