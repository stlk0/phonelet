"""Periodic SMS recovery through the production monitor and parser on a PTY."""
import time

import pytest

from tests.pty_modem import (
    CAPACITY, INIT_ME, KEEP_SM, READ_ME, READ_SM, DriverSession, PtyModem, cmgr_reply, journal_before_delete, logs,
    scripted_reads, storage, texts, until, wrap_reply,
)
from tests.sms_pdu import deliver_pdu

pytestmark = pytest.mark.driver


def advance(driver, seconds):
    driver.adapt(f'ADVANCE={seconds}')


def finished(driver):
    return sum('SMS rescan finished:' in line for line in logs(driver))


def settle(driver, count):
    until(lambda: finished(driver) == count)
    driver.wait('IDLE_WAIT')


@pytest.mark.parametrize('memory', ['SM', 'ME'])
def test_lost_cmti_is_recovered_only_after_interval(driver_binary, tmp_path, memory):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            marker = len(modem.commands)
            modem.put_sms(7, deliver_pdu('lost notification'), memory=memory)
            advance(driver, 599)
            time.sleep(.15)
            assert storage(modem.commands[marker:]) == []
            assert not modem.deletions and not list(spool.glob('*.sms'))
            advance(driver, 1)
            settle(driver, 2)
            assert texts(spool) == ['lost notification']
            assert modem.deletions == [(memory, 7)] and not violations
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]
            assert modem.selected == ['ME', 'ME', 'ME']
            assert f'[dongle0] SMS rescan finished: SM={int(memory == "SM")} ME={int(memory == "ME")}' in logs(driver)
            advance(driver, 599)
            time.sleep(.15)
            assert finished(driver) == 2
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]


def test_interleaved_urc_is_recovered_on_next_pass(driver_binary, tmp_path, monkeypatch):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            modem.put_sms(3, deliver_pdu('retained intact'))
            reply = cmgr_reply(modem.sms[3])
            head = reply.index(b'\r\n', 2) + 2
            bad = reply[:head] + b'\r\n+CMTI: "ME",4\r\n' + reply[head:]
            scripted_reads(monkeypatch, modem, 'AT+CMGR=3', [bad, cmgr_reply('00FF')])
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: any('failed two inconsistent reads' in line for line in logs(driver)))
            assert not modem.deletions and not list(spool.glob('*.sms'))
            modem.clear_script('AT+CMGR=3')
            advance(driver, 600)
            settle(driver, 2)
            assert texts(spool) == ['retained intact']
            assert modem.deletions == [('ME', 3)] and not violations


@pytest.mark.parametrize('state', [
    '1,0,0,0,1,1,0',  # active Asterisk channel
    '0,0,0,0,0,1,0',  # not initialized
    '0,0,0,0,1,0,0',  # no SMS support
    '0,0,0,0,1,1,1',  # SMS disabled
])
def test_rescan_waits_for_idle_state(driver_binary, tmp_path, state):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            marker = len(modem.commands)
            driver.adapt(f'STATE={state}')
            advance(driver, 1200)
            time.sleep(.15)
            assert storage(modem.commands[marker:]) == []
            driver.adapt('STATE=0,0,0,0,1,1,0')
            settle(driver, 2)
            time.sleep(.15)
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]
            assert finished(driver) == 2


def test_ring_without_a_call_defers_a_pass_only_briefly(driver_binary, tmp_path):
    """RING leaves pvt->ring set when no call follows (no CLCC, no channel).

    Within 60 s of the last RING no pass starts; after that the pass runs.
    """
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            marker = len(modem.commands)
            advance(driver, 570)
            modem.send_data(b'\r\nRING\r\n')
            driver.wait('RING')
            advance(driver, 30)
            time.sleep(.15)
            advance(driver, 29)
            time.sleep(.15)
            assert storage(modem.commands[marker:]) == [] and finished(driver) == 1
            advance(driver, 1)
            settle(driver, 2)
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]


def test_pending_slots_and_command_queue_defer_one_pass(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=2000) as driver:
            settle(driver, 1)
            marker = len(modem.commands)
            modem.script('AT+BUSY', [b'\r\nOK\r\n'], delay=.4)
            driver.command('AT+BUSY', timeout_ms=2000)
            driver.wait('WRITTEN')
            modem.put_sms(3, deliver_pdu('queued three'))
            modem.put_sms(7, deliver_pdu('queued seven'))
            modem.script('AT+CMGR=3', [cmgr_reply(modem.sms[3])], delay=.4)
            # The script ignores the memories: drop it once slot 3 is deleted.
            modem.on_delete = lambda _memory, slot: slot == 3 and modem.clear_script('AT+CMGR=3')
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n\r\n+CMTI: "ME",7\r\n')
            until(lambda: any(event[:2] == ('RESPONSE', '+CMTI') for event in driver.events))
            advance(driver, 1800)
            time.sleep(.15)
            assert finished(driver) == 1
            assert storage(modem.commands[marker:]) == []
            settle(driver, 2)
            assert modem.commands.index(READ_SM, marker) > modem.commands.index('AT+CMGD=7')
            assert texts(spool) == ['queued seven', 'queued three']
            time.sleep(.15)
            assert finished(driver) == 2


def read_errors(driver):
    return sum('Error reading SMS message' in line for line in logs(driver))


FULL = [(memory, slot) for memory in ('SM', 'ME') for slot in range(CAPACITY[memory])]


def test_cpms_total_bounds_every_full_poll(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        modem.put_sms(3, deliver_pdu('three'))
        modem.put_sms(7, deliver_pdu('seven'))
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            assert modem.reads == FULL
            assert texts(spool) == ['seven', 'three']
            modem.put_sms(22, deliver_pdu('last slot'))
            marker = len(modem.reads)
            advance(driver, 600)
            settle(driver, 2)
            # At most total (15 SM, 23 ME) CMGR per memory, and the last slot is read.
            assert modem.reads[marker:] == FULL
            assert texts(spool) == ['last slot', 'seven', 'three']
            assert read_errors(driver) == 0


@pytest.mark.parametrize('slot,cmti', [(0, True), (3, True), (3, False)])
def test_sms_arriving_during_a_pass_does_not_end_it_early(driver_binary, tmp_path, monkeypatch, slot, cmti):
    """+CPMS used is a count, not slots: a new SMS takes a low free slot.

    The older SMS in slot 20 is still read in the same pass.
    """
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            modem.put_sms(20, deliver_pdu('old in slot twenty'))
            arrived = []

            def hook(command, reply):
                if command == 'AT+CMGR=1' and modem.selected[0] == 'ME' and not arrived:
                    arrived.append(slot)
                    modem.put_sms(slot, deliver_pdu('new during the pass'))
                    if cmti:
                        modem.send_data(f'\r\n+CMTI: "ME",{slot}\r\n'.encode())
                return reply

            wrap_reply(monkeypatch, modem, hook)
            advance(driver, 600)
            settle(driver, 2)
            assert arrived == [slot]
            assert texts(spool) == ['new during the pass', 'old in slot twenty']
            assert sorted(modem.deletions) == [('ME', slot), ('ME', 20)] and not violations
            assert '[dongle0] SMS rescan finished: SM=0 ME=2' in logs(driver)
            # Empty slots of the full read stay quiet.
            assert read_errors(driver) == 0


def test_cmti_inside_a_selection_reply_is_read_in_this_pass(driver_binary, tmp_path, monkeypatch):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            arrived = []

            def hook(command, reply):
                if command == READ_ME and not arrived:
                    arrived.append(command)
                    text, = reply.chunks
                    assert text.startswith(b'\r\n+CPMS: 0,23,')
                    modem.put_sms(5, deliver_pdu('between counts and OK'))
                    reply = type(reply)((text.replace(b'\r\n\r\nOK', b'\r\n\r\n+CMTI: "ME",5\r\n\r\nOK'),))
                return reply

            wrap_reply(monkeypatch, modem, hook)
            advance(driver, 600)
            settle(driver, 2)
            assert arrived and any(event[:2] == ('RESPONSE', '+CMTI') for event in driver.events)
            assert texts(spool) == ['between counts and OK']
            assert modem.deletions == [('ME', 5)] and not violations
            assert '[dongle0] SMS rescan finished: SM=0 ME=1' in logs(driver)


@pytest.mark.parametrize('counts', [b'', b'+CPMS: broken\r\n', b'+CPMS: 24,23\r\n',
                                  b'+CPMS: -1,23\r\n', b'+CPMS: 0,23junk\r\n',
                                  b'+CPMS: 4294967296,23\r\n'])
def test_missing_or_invalid_cpms_counts_fall_back_without_stale_bounds(driver_binary, tmp_path, monkeypatch, counts):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)  # earlier counts said total=23

            def hook(command, reply):
                if command == READ_ME:
                    return type(reply)((b'\r\n' + counts + (b'\r\n' if counts else b'') + b'OK\r\n',))
                return reply

            wrap_reply(monkeypatch, modem, hook)
            modem.put_sms(7, deliver_pdu('fallback recovery'))
            marker = len(modem.reads)
            advance(driver, 600)
            settle(driver, 2)
            assert modem.reads[marker:] == [('SM', slot) for slot in range(15)] + [('ME', slot) for slot in range(256)]
            assert texts(spool) == ['fallback recovery']
            assert read_errors(driver) == 0


def test_rejected_sim_selection_reads_modem_memory_once_and_retries_next_interval(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            modem.refused = {'SM'}
            modem.put_sms(6, deliver_pdu('read although the sim was refused'))
            marker, reads = len(modem.commands), len(modem.reads)
            advance(driver, 600)
            settle(driver, 2)
            # The rejection leaves ME selected: it is read once, without counts.
            assert storage(modem.commands[marker:]) == [READ_SM]
            assert modem.reads[reads:] == [('ME', slot) for slot in range(256)]
            assert texts(spool) == ['read although the sim was refused']
            assert modem.selected == ['ME', 'ME', 'ME']
            advance(driver, 599)
            time.sleep(.15)
            assert finished(driver) == 2
            assert storage(modem.commands[marker:]) == [READ_SM]
            modem.refused.clear()
            advance(driver, 1)
            settle(driver, 3)
            assert storage(modem.commands[marker:]) == [READ_SM, READ_SM, READ_ME]


def test_rejected_sim_selection_reads_modem_memory_at_start_reconnect_and_pass(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    full_me = [('ME', slot) for slot in range(256)]
    with PtyModem(tmp_path / 'modem') as modem:
        modem.refused = {'SM'}
        modem.put_sms(5, deliver_pdu('at startup'))
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            assert storage(modem.commands) == [INIT_ME, READ_SM]
            assert modem.reads == full_me and texts(spool) == ['at startup']
            modem.disconnect()
            driver.wait('DISCONNECT')
            modem.put_sms(9, deliver_pdu('after reconnect'))
            marker, reads = len(modem.commands), len(modem.reads)
            modem.reappear()
            settle(driver, 2)
            assert storage(modem.commands[marker:]) == [INIT_ME, READ_SM]
            assert modem.reads[reads:] == full_me
            modem.put_sms(12, deliver_pdu('by a periodic pass'))
            marker, reads = len(modem.commands), len(modem.reads)
            advance(driver, 600)
            settle(driver, 3)
            assert storage(modem.commands[marker:]) == [READ_SM]
            assert modem.reads[reads:] == full_me
            assert texts(spool) == ['after reconnect', 'at startup', 'by a periodic pass']
            assert modem.deletions == [('ME', 5), ('ME', 9), ('ME', 12)] and not violations
            assert modem.selected == ['ME', 'ME', 'ME'] and read_errors(driver) == 0
            advance(driver, 599)
            time.sleep(.15)
            assert finished(driver) == 3 and storage(modem.commands[marker:]) == [READ_SM]


def test_both_memories_rejected_at_start_still_read_the_sim(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        modem.refused = {'ME', 'SM'}
        modem.put_sms(4, deliver_pdu('kept on the sim'), memory='SM')
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            assert storage(modem.commands) == [INIT_ME, KEEP_SM]
            assert modem.reads == [('SM', slot) for slot in range(256)]
            assert modem.deletions == [('SM', 4)] and not violations
            assert texts(spool) == ['kept on the sim']
            modem.put_sms(8, deliver_pdu('found by a pass'), memory='SM')
            marker, reads = len(modem.commands), len(modem.reads)
            advance(driver, 600)
            settle(driver, 2)
            # Both selections fail once each; the SIM, still selected, is read once.
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]
            assert modem.reads[reads:] == [('SM', slot) for slot in range(256)]
            assert modem.deletions == [('SM', 4), ('SM', 8)] and not violations
            assert modem.selected == ['SM', 'SM', 'SM']
            time.sleep(.15)
            assert finished(driver) == 2


def test_modem_memory_rejected_at_start_is_read_by_the_next_pass(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        modem.script(READ_ME, [b'\r\nERROR\r\n'])
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            assert storage(modem.commands) == [INIT_ME, READ_SM, READ_ME]
            # The SIM was read in this pass already: it is not read again.
            assert modem.reads == [('SM', slot) for slot in range(15)]
            modem.clear_script(READ_ME)
            modem.put_sms(6, deliver_pdu('waited for the pass'))
            marker = len(modem.commands)
            advance(driver, 600)
            settle(driver, 2)
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]
            assert texts(spool) == ['waited for the pass']
            assert modem.deletions == [('ME', 6)] and not violations
            assert modem.selected == ['ME', 'ME', 'ME']


def test_unsent_selection_ends_the_pass_until_the_next_interval(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            driver.adapt('FAIL_CPMS=1')
            modem.put_sms(6, deliver_pdu('after a failed selection'))
            marker, events = len(modem.commands), len(driver.events)
            advance(driver, 600)
            until(lambda: any('SMS rescan stopped' in line for line in logs(driver, events)))
            time.sleep(.15)
            assert logs(driver, events) == [
                '[dongle0] SMS rescan started: SM=0 ME=0',
                '[dongle0] Error queueing SMS storage SM',
                '[dongle0] SMS rescan stopped: storage selection not sent',
            ]
            assert storage(modem.commands[marker:]) == [] and finished(driver) == 1
            advance(driver, 599)
            time.sleep(.15)
            assert storage(modem.commands[marker:]) == [] and finished(driver) == 1
            advance(driver, 1)
            settle(driver, 2)
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]
            assert texts(spool) == ['after a failed selection']


def test_unparsed_retries_count_each_occupied_slot_once(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        modem.put_sms(3, '00FF')
        modem.put_sms(7, deliver_pdu('after quarantine'))
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            assert modem.reads == (FULL[:CAPACITY['SM']] + [('ME', i) for i in range(4)] + [('ME', 3)]
                                   + [('ME', i) for i in range(4, 23)])
            assert modem.deletions == [('ME', 3), ('ME', 7)] and not violations
            records = [path.read_bytes() for path in spool.glob('*.sms')]
            assert len(records) == 2 and sum(record.startswith(b'U1\t') for record in records) == 1
            assert '[dongle0] SMS rescan finished: SM=0 ME=2' in logs(driver)


def test_journal_retry_holds_the_memory_and_blocks_new_rescan(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            spool.rmdir()  # empty test directory only
            modem.put_sms(3, deliver_pdu('retained on journal failure'))
            advance(driver, 600)
            until(lambda: any('retaining SMS on SIM' in line for line in logs(driver)))
            assert modem.selected == ['ME', 'ME', 'ME'] and not modem.deletions
            advance(driver, 1200)
            until(lambda: sum('retaining SMS on SIM' in line for line in logs(driver)) == 2)
            time.sleep(.15)
            assert sum('SMS rescan started:' in line for line in logs(driver)) == 2
            assert finished(driver) == 1
            spool.mkdir()
            reads = len(modem.reads)
            advance(driver, 30)
            settle(driver, 2)
            assert texts(spool) == ['retained on journal failure']
            assert modem.deletions == [('ME', 3)]
            # The retry reads slot 3, then the full read goes on, quietly.
            assert modem.reads[reads:] == [('ME', slot) for slot in range(3, 23)]
            assert read_errors(driver) == 0
            time.sleep(.15)
            assert sum('SMS rescan started:' in line for line in logs(driver)) == 2


@pytest.mark.parametrize('state', [
    '0,0,1,0,1,1,0',  # dialing left set without a channel
    '0,0,0,1,1,1,0',  # call waiting left set without a channel
])
def test_stuck_call_flags_without_a_channel_do_not_block_a_pass(driver_binary, tmp_path, state):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            marker = len(modem.commands)
            driver.adapt(f'STATE={state}')
            modem.put_sms(6, deliver_pdu('read despite a stuck flag'))
            advance(driver, 600)
            settle(driver, 2)
            assert storage(modem.commands[marker:]) == [READ_SM, READ_ME]
            assert texts(spool) == ['read despite a stuck flag']


def test_read_that_was_not_queued_is_retried_and_the_pass_finishes(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            modem.put_sms(6, deliver_pdu('after a failed read'))
            driver.adapt('FAIL_CMGR=1')
            advance(driver, 600)
            time.sleep(.3)
            # The first SIM read was not queued: the slot waits with nothing in flight.
            assert texts(spool) == [] and finished(driver) == 1
            advance(driver, 31)
            settle(driver, 2)
            assert texts(spool) == ['after a failed read']


def test_unsent_second_selection_stops_the_pass_until_the_next_interval(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            settle(driver, 1)
            driver.adapt('FAIL_CPMS_ME=1')
            modem.put_sms(6, deliver_pdu('after a failed second selection'))
            marker, events = len(modem.commands), len(driver.events)
            advance(driver, 600)
            until(lambda: any('SMS rescan stopped' in line for line in logs(driver, events)))
            time.sleep(.15)
            assert storage(modem.commands[marker:]) == [READ_SM] and finished(driver) == 1
            assert not any('SMS rescan finished' in line for line in logs(driver, events))
            advance(driver, 600)
            settle(driver, 2)
            assert texts(spool) == ['after a failed second selection']
