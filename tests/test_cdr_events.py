import logging
from unittest.mock import AsyncMock

import pytest
from phonelet.ami_client import Message

from phonelet.contracts import MalformedCdr
from phonelet.cdr_events import CdrHandler
from phonelet.logging_privacy import install_log_privacy


@pytest.fixture
def handler():
    return CdrHandler(on_call_end=AsyncMock(), on_sms_failed=AsyncMock())


START = 1790337600.0  # the StartTime below, 2026-09-25 12:00:00 UTC


def cdr(**kwargs):
    return {'Channel': 'Dongle/dongle0-00000001', 'LastApplication': 'Dial',
            'UniqueID': '1770000000.1', 'StartTime': '2026-09-25 12:00:00',
            'Source': '+15551234567', 'Disposition': 'ANSWERED', 'BillableSeconds': '115',
            'UserField': '/var/lib/phonelet/recordings/test.mp3', **kwargs}


def report(reason='i', **kwargs):
    # A failed outgoing SMS: report@dongle-incoming keeps the CDR of ;1 (the half the driver
    # starts), src = recipient, marked in userfield (asterisk/extensions_custom.conf.tpl).
    return {**cdr(), 'Channel': 'Local/report@dongle-incoming-00000002;1', 'LastApplication': 'Hangup',
            'UniqueID': '1792667870.4', 'Source': '+15550000001', 'Disposition': 'NO ANSWER',
            'BillableSeconds': '0', 'UserField': 'phonelet-sms-report:' + reason, **kwargs}


@pytest.mark.parametrize('changes', [{'Channel': 'PJSIP/phone-123'},
                                     {'Channel': 'PJSIP/phone-123', 'UserField': ';phonelet-call-rejected'},
                                     # outgoing SMS report legs as Asterisk 20.18 logs them without report@
                                     {'Channel': 'Local/report@dongle-incoming-00000001;2', 'LastApplication': '',
                                      'Source': '', 'Disposition': 'FAILED', 'BillableSeconds': '0', 'UserField': ''},
                                     {'Channel': 'Local/report@dongle-incoming-00000001;1', 'LastApplication': 'Hangup',
                                      'Source': '+15550000001', 'Disposition': 'NO ANSWER'},
                                     # the other half of an r report when EXPORT() or IF() is missing
                                     {'Channel': 'Local/report@dongle-incoming-00000000;1', 'LastApplication': 'AppDial2',
                                      'Source': '+15550000001', 'Disposition': 'NO ANSWER', 'UserField': ''},
                                     # the Message channel that runs [sms-outbound]: one line for all SMS,
                                     # at Asterisk shutdown, even when its CDR is marked
                                     {'Channel': 'Message/ast_msg_queue', 'LastApplication': 'Hangup',
                                      'UniqueID': '1792716012.0', 'Source': '', 'BillableSeconds': '16',
                                      'UserField': ''},
                                     {'Channel': 'Message/ast_msg_queue', 'LastApplication': 'Hangup',
                                      'UniqueID': '1792716035.0', 'Source': '', 'BillableSeconds': '4',
                                      'UserField': 'phonelet-sms-report:r'}])
async def test_other_cdr_legs_ignored(handler, changes):
    await handler.handle_cdr(cdr(**changes))
    handler.on_call_end.assert_not_awaited()
    handler.on_sms_failed.assert_not_awaited()


@pytest.mark.parametrize('marker,reason', [('i', 'i'), ('e', 'e'), ('t', 't'), ('r', 'r'), ('x', 'unknown'),
                                           ('', 'unknown')])
async def test_failed_sms_report(handler, caplog, marker, reason):
    caplog.set_level(logging.DEBUG)
    install_log_privacy()
    await handler.handle_cdr(report(marker))
    handler.on_sms_failed.assert_awaited_once_with(event_key='sms-report:1792667870.4',
                                                   recipient='+15550000001', reason=reason)
    handler.on_call_end.assert_not_awaited()
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [(r.name, r.getMessage()) for r in records] == [
        ('phonelet.cdr_events', f'Outgoing SMS failed: reason={reason}')]
    assert '15550000001' not in caplog.text


async def test_replayed_sms_report_with_the_alert_already_queued_is_info(handler, caplog):
    # A restart replays the retained CDR journal: the receiver finds the alert already queued.
    caplog.set_level(logging.DEBUG)
    install_log_privacy()
    handler.on_sms_failed.return_value = False
    await handler.handle_cdr(report('i'))
    handler.on_sms_failed.assert_awaited_once()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == 'phonelet.cdr_events'] == [
        (logging.INFO, 'Outgoing SMS failed: reason=i; alert already queued')]
    assert '15550000001' not in caplog.text


async def test_sms_rejected_before_the_driver_queue_alerts_each_time(handler, caplog):
    # Two identical rejections, as [sms-send] leaves them via Originate: each its own Local
    # pair (;2 runs report@), uniqueid and line, src = recipient.
    caplog.set_level(logging.DEBUG)
    install_log_privacy()
    for uid, channel in (('1792716012.2', 'Local/report@dongle-incoming-00000000;2'),
                         ('1792716014.5', 'Local/report@dongle-incoming-00000001;2')):
        await handler.handle_cdr(report('r', Channel=channel, UniqueID=uid))
    assert [call.kwargs for call in handler.on_sms_failed.await_args_list] == [
        {'event_key': 'sms-report:1792716012.2', 'recipient': '+15550000001', 'reason': 'r'},
        {'event_key': 'sms-report:1792716014.5', 'recipient': '+15550000001', 'reason': 'r'}]
    assert '15550000001' not in caplog.text


async def test_failed_sms_report_whose_alert_fails_to_queue_is_not_consumed(caplog):
    # The CDR journal replay must not pass a marked line it cannot queue.
    caplog.set_level(logging.DEBUG)
    install_log_privacy()
    handler = CdrHandler(AsyncMock(), AsyncMock(side_effect=RuntimeError('database is locked')))
    with pytest.raises(RuntimeError) as error:
        await handler.handle_cdr(report())
    assert str(error.value) == 'database is locked'
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        'Outgoing SMS failed: reason=i; alert not queued yet']
    assert '15550000001' not in caplog.text


@pytest.mark.parametrize('start', ['', '2026-00-25 12:00:00'])
async def test_failed_sms_report_with_a_bad_start_time_still_alerts(handler, start):
    # The report does not require a clock: replay supplies its history status.
    await handler.handle_cdr(report(StartTime=start))
    assert 'history' not in handler.on_sms_failed.call_args.kwargs


async def test_failed_sms_report_without_recipient_or_id(handler):
    # channel originate runs the dialplan on ;2, which carries no caller ID.
    await handler.handle_cdr(report(Channel='Local/report@dongle-incoming-00000005;2', Source=''))
    assert handler.on_sms_failed.call_args.kwargs['recipient'] == 'unknown'
    with pytest.raises(ValueError):
        await handler.handle_cdr(report(UniqueID=''))
    handler.on_sms_failed.assert_awaited_once()


@pytest.mark.parametrize('disposition,status,path', [('ANSWERED', 'answered', '/var/lib/phonelet/recordings/test.mp3'),
                                                    ('NO ANSWER', 'missed', None), ('BUSY', 'rejected', None)])
async def test_real_incoming_cdr(handler, disposition, status, path):
    await handler.handle_cdr(cdr(Disposition=disposition))
    data = handler.on_call_end.call_args.kwargs
    assert data['status'] == status
    assert data['recording_path'] == path
    assert data['timestamp'] == '2026-09-25T12:00:00+00:00'
    assert data['source_id'] == 'cdr:1770000000.1'


@pytest.mark.parametrize('userfield,status', [
    (';phonelet-call-rejected', 'rejected'),
    ('/var/lib/phonelet/recordings/test.mp3;phonelet-call-rejected', 'rejected'),
    ('', 'missed'),
    ('/var/lib/phonelet/recordings/test.mp3', 'missed'),
    ('phonelet-call-rejected', 'missed'),
    (';phonelet-call-rejected;other', 'missed'),
    (';phonelet-call-rejected-extra', 'missed'),
])
async def test_incoming_no_answer_uses_only_the_final_userfield_marker(handler, userfield, status):
    await handler.handle_cdr(cdr(Disposition='NO ANSWER', BillableSeconds='0', UserField=userfield))
    data = handler.on_call_end.call_args.kwargs
    assert data['status'] == status
    assert data['recording_path'] is None


@pytest.mark.parametrize('recording_path', ['', '/var/lib/phonelet/recordings/test.mp3'])
@pytest.mark.parametrize('disposition,marker,status,post_dial', [
    pytest.param('NO ANSWER', 'phonelet-call-rejected', 'rejected', True, id='sip-603-cause-21'),
    pytest.param('BUSY', 'phonelet-call-rejected', 'rejected', True, id='sip-486-cause-17'),
    pytest.param('BUSY', 'phonelet-call-rejected', 'rejected', True, id='sip-600-cause-17'),
    pytest.param('NO ANSWER', 'phonelet-call-rejected', 'rejected', True, id='sip-480-cause-19'),
    pytest.param('NO ANSWER', '', 'missed', False, id='caller-hangup-cancel-sip-487'),
    pytest.param('NO ANSWER', '', 'missed', True, id='ring-out-noanswer'),
    pytest.param('NO ANSWER', '', 'missed', True, id='sip-403-cause-21'),
])
async def test_incoming_call_cdr_sequence_has_one_result(handler, recording_path, disposition, marker,
                                                      status, post_dial):
    # Synthetic Asterisk 20.22.0 rows, not a SIP or CDR-engine simulation. The
    # callee handler sets B's userfield; cdr.c joins A's path and B's marker.
    userfield = recording_path + (';' + marker if marker else '')
    await handler.handle_cdr(cdr(Disposition=disposition, BillableSeconds='0', UserField=userfield))
    if post_dial:
        # The following Hangup row inherits A's path, but not B's marker.
        await handler.handle_cdr(cdr(LastApplication='Hangup', Disposition='NO ANSWER',
                                     BillableSeconds='0', UserField=recording_path))
    # Every row is passed on; the database keeps the first one of a source_id (the Dial row).
    assert handler.on_call_end.await_count == (2 if post_dial else 1)
    assert {c.kwargs['source_id'] for c in handler.on_call_end.await_args_list} == {'cdr:1770000000.1'}
    assert handler.on_call_end.await_args_list[0].kwargs == dict(
        source_id='cdr:1770000000.1', timestamp='2026-09-25T12:00:00+00:00',
        caller='+15551234567', status=status, duration=0, recording_path=None,
    )
    handler.on_sms_failed.assert_not_awaited()


@pytest.mark.parametrize('recording_path', ['', '/var/lib/phonelet/recordings/test.mp3'])
@pytest.mark.parametrize('disposition', ['NO ANSWER', 'FAILED'])
async def test_call_that_never_reached_dial_is_missed(handler, recording_path, disposition):
    # The phone has no reachable contact (or the caller hung up before Dial): one row, lastapp Hangup.
    await handler.handle_cdr(cdr(LastApplication='Hangup', Disposition=disposition, BillableSeconds='0',
                                 UserField=recording_path))
    handler.on_call_end.assert_awaited_once_with(
        source_id='cdr:1770000000.1', timestamp='2026-09-25T12:00:00+00:00',
        caller='+15551234567', status='missed', duration=0, recording_path=None,
    )


async def test_answered_recording_path_excludes_party_b_userfield(handler):
    await handler.handle_cdr(cdr(UserField='/var/lib/phonelet/recordings/test.mp3;other'))
    data = handler.on_call_end.call_args.kwargs
    assert data['status'] == 'answered'
    assert data['recording_path'] == '/var/lib/phonelet/recordings/test.mp3'


@pytest.mark.parametrize('start,expected', [('2026-09-25 12:00:00', '2026-09-25T12:00:00+00:00'),
                                           ('2026-09-25T12:00:00+0200', '2026-09-25T10:00:00+00:00')])
async def test_cdr_offset_and_anonymous(handler, start, expected):
    await handler.handle_cdr(cdr(Source='', StartTime=start))
    assert handler.on_call_end.call_args.kwargs['timestamp'] == expected
    assert handler.on_call_end.call_args.kwargs['caller'] == 'anonymous'


@pytest.mark.parametrize('changes', [{'UniqueID': ''}, {'StartTime': ''}, {'StartTime': '2026-00-25 12:00:00'},
                                     {'BillableSeconds': ''}])
async def test_malformed_call_cdr_raises_for_the_replay_to_set_aside(handler, changes):
    # IngressReplay quarantines a MalformedCdr line (alert, offset moves on); nothing is stored.
    with pytest.raises(MalformedCdr):
        await handler.handle_cdr(cdr(**changes))
    handler.on_call_end.assert_not_awaited()


async def test_case_insensitive_event_headers(handler):
    # AMI Message mappings look headers up ignoring case.
    # Keep that behavior when describing callbacks with explicit event types.
    await handler.handle_cdr(Message({key.lower(): value for key, value in cdr().items()}))
    assert handler.on_call_end.call_args.kwargs['source_id'] == 'cdr:1770000000.1'
