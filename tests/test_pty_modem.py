"""Exercise the actual C monitor on one isolated PTY with adversarial timing.

data= is PCUI: URCs and command replies are interleaved in one stream.
"""
import threading
import time

import pytest

from phonelet.ingress import parse_fragment
from tests.pty_modem import (
    CNMI, DRIVER_PATCHES, INIT_ME, KEEP_SM, READ_ME, READ_SM, DriverSession, PtyModem, cmgr_reply,
    compile_driver, fragments, journal_before_delete, logs, scripted_reads, storage, texts, until,
)
from tests.sms_pdu import deliver_pdu

pytestmark = pytest.mark.driver


@pytest.fixture(scope="module")
def sanitized_driver(tmp_path_factory):
    return compile_driver(tmp_path_factory.mktemp("pty-sanitized-driver"), sanitize=True)


@pytest.fixture(scope="module")
def driver_without_readable_deadline(tmp_path_factory):
    assert "readable_deadline" in DRIVER_PATCHES
    return compile_driver(tmp_path_factory.mktemp("pty-upstream-deadline-driver"),
                          patches=tuple(name for name in DRIVER_PATCHES if name != "readable_deadline"))


@pytest.mark.parametrize("binary_fixture", ["driver_binary", "sanitized_driver"])
@pytest.mark.parametrize("reply,classification", [
    (b"ERROR", "ERROR"),
    (b"+CME ERROR: 10", "UNKNOWN"),
    (b"+CMS ERROR: 500", "+CMS ERROR"),
])
def test_idle_data_error_preserves_command_and_sms(request, binary_fixture, tmp_path,
                                                   reply, classification):
    executable = request.getfixturevalue(binary_fixture)
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(executable, modem, spool) as driver:
            driver.wait("READY")
            marker = len(driver.events)
            # In one read, OK removes the ping before the extra reply is parsed.
            modem.script("AT", [b"\r\nOK\r\n\r\n" + reply + b"\r\n"])
            until(lambda: ("RESPONSE", classification, reply.hex()) in driver.events[marker:])
            modem.clear_script("AT")
            # RESPONSE is printed before dispatch; RING confirms it completed.
            modem.send_data(b"\r\nRING\r\n")
            driver.wait("RING", timeout=1)
            events = driver.events[marker:]
            assert any(event[:2] == ("RESPONSE", classification) for event in events)
            unexpected = any("Received unexpected 'ERROR'" in field
                             for event in events for field in event)
            assert unexpected == (classification != "UNKNOWN")
            driver.command("AT+AFTERERROR", timeout_ms=1000)
            assert driver.wait("WRITTEN")[0] == "AT+AFTERERROR"
            driver.wait("COMMAND_OK")
            modem.put_sms(9, deliver_pdu("SMS after an idle data error"))
            modem.send_data(b'\r\n+CMTI: "ME",9\r\n')
            until(lambda: 9 in modem.deleted)
            fragments = [parse_fragment(path.read_bytes()) for path in spool.glob("*.sms")]
            assert len(fragments) == 1
            assert fragments[0]["text"] == "SMS after an idle data error"
            assert driver.process.poll() is None
            assert not any(event[0] == "DISCONNECT" for event in driver.events)
            assert not any("Sanitizer" in field or "runtime error:" in field
                           for event in driver.events for field in event)


def test_command_written_during_idle_wait_keeps_own_deadline(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.script("AT+DELAYED", [b"\r\nOK\r\n"], delay=.3)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            driver.wait("IDLE_WAIT")
            driver.command("AT+DELAYED", timeout_ms=1000)
            assert driver.wait("WRITTEN")[0] == "AT+DELAYED"
            driver.wait("IDLE_EXPIRED_WITH_COMMAND", timeout=1.5)
            driver.wait("COMMAND_OK", timeout=1.5)
            assert driver.process.poll() is None
            assert not any(event[0] == "DISCONNECT" for event in driver.events)
            assert not any("timedout" in field for event in driver.events for field in event)


def test_command_written_during_idle_wait_still_expires(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.silence("AT+SILENT")
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            driver.wait("IDLE_WAIT")
            driver.command("AT+SILENT", timeout_ms=1000)
            command, written_ms = driver.wait("WRITTEN")
            assert command == "AT+SILENT"
            disconnected_ms, = driver.wait("DISCONNECT", timeout=5)
            elapsed_ms = int(disconnected_ms) - int(written_ms)
            assert 950 <= elapsed_ms < 1700
            assert any("timedout while waiting" in field
                       for event in driver.events for field in event)


def test_partial_garbage_and_ring_while_reading_sms(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    pdu = deliver_pdu("partial lines survive concurrent RING")
    response = cmgr_reply(pdu)
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=800) as driver:
            driver.wait("READY")
            # Garbage has its own framing boundary. Follow it with a fragmented
            # real URC on the same port, while CMGR is queued there.
            send = modem.send_data
            send(b"\xff\x00noise\r\n")
            modem.script("AT+CMGR=4", [response[:9], response[9:31], response[31:]],
                         delay=.06, chunk_delay=.025)
            modem.put_sms(4, pdu)
            send(b'\r\n+CMTI: "ME",4\r\n')
            until(lambda: "AT+CMGR=4" in modem.commands)
            # These bytes arrive before the first CMGR header; the stream
            # retains its partial line through the monitor loop.
            send(b"\r\nRI")
            time.sleep(.005)
            send(b"NG\r\n")
            assert driver.wait("RING") == ("1",)
            until(lambda: 4 in modem.deleted)
            fragments = [parse_fragment(path.read_bytes()) for path in spool.glob("*.sms")]
            assert len(fragments) == 1
            assert fragments[0]["text"] == "partial lines survive concurrent RING"
            assert driver.process.poll() is None


def test_slow_split_initialization_succeeds_before_deadline(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.script("AT", [b"\r", b"\nO", b"K\r\n"], delay=.04, chunk_delay=.025)
        with DriverSession(driver_binary, modem, spool, timeout_ms=250) as driver:
            driver.wait("READY")
            assert not any(event[0] == "DISCONNECT" for event in driver.events)
            assert modem.commands[0] == "AT"
            # The SIM is read through, then ME, each up to its +CPMS total.
            assert [memory for memory, _slot in modem.reads] == ["SM"] * 15 + ["ME"] * 23
            assert storage(modem.commands) == [INIT_ME, READ_SM, READ_ME]


@pytest.mark.parametrize("reply", [b"OK", b"ERROR", b"+CMS ERROR: 500"])
def test_cmgr_acknowledgment_advances_queued_sms(driver_binary, tmp_path, reply):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=500) as driver:
            driver.wait("READY")
            initial_reads = modem.commands.count("AT+CMGR=6")
            modem.put_sms(6, deliver_pdu("slot remains on SIM without a PDU reply"))
            modem.put_sms(7, deliver_pdu("next queued SMS is retrieved"))
            modem.script("AT+CMGR=6", [b"\r\n" + reply + b"\r\n"], delay=.15)
            modem.send_data(b'\r\n+CMTI: "ME",6\r\n')
            until(lambda: modem.commands.count("AT+CMGR=6") > initial_reads)
            modem.send_data(b'\r\n+CMTI: "ME",7\r\n\r\nRING\r\n')
            driver.wait("RING")
            until(lambda: 7 in modem.deleted)
            fragments = [parse_fragment(path.read_bytes()) for path in spool.glob("*.sms")]
            assert len(fragments) == 1 and fragments[0]["text"] == "next queued SMS is retrieved"
            assert 6 in modem.sms and 6 not in modem.deleted
            assert modem.commands.count("AT+CMGR=6") == initial_reads + 1
            assert not any(event[0] == "DISCONNECT" for event in driver.events)
            assert not any("timedout" in field for event in driver.events for field in event)


def test_stray_cmgr_reply_never_deletes_pending_slot(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=500) as driver:
            driver.wait("READY")
            initial_reads = modem.commands.count("AT+CMGR=5")
            modem.put_sms(4, deliver_pdu("first SMS is journaled"))
            modem.put_sms(5, deliver_pdu("unread SMS stays on SIM"))
            # A stale CMGR reply arrives while CMGD(4) is the head and CMGR(5)
            # is already queued; the real read of slot 5 then fails.
            stale = cmgr_reply(deliver_pdu("stale reply is never journaled"))
            modem.script("AT+CMGD=4", [stale, b"\r\nOK\r\n"])
            modem.script("AT+CMGR=5", [b"\r\n+CMS ERROR: 500\r\n"])
            modem.send_data(b'\r\n+CMTI: "ME",4\r\n\r\n+CMTI: "ME",5\r\n')
            until(lambda: modem.commands.count("AT+CMGR=5") > initial_reads)
            modem.send_data(b"\r\nRING\r\n")
            driver.wait("RING")
            fragments = [parse_fragment(path.read_bytes()) for path in spool.glob("*.sms")]
            assert [fragment["text"] for fragment in fragments] == ["first SMS is journaled"]
            assert "AT+CMGD=5" not in modem.commands
            assert 5 in modem.sms and 5 not in modem.deleted
            assert not any(event[0] == "DISCONNECT" for event in driver.events)


def test_continuous_urcs_do_not_extend_command_deadline(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=120) as driver:
            driver.wait("READY")
            modem.put_sms(7, deliver_pdu("held by timed out read"))
            modem.silence("AT+CMGR=7")
            modem.send_data(b'\r\n+CMTI: "ME",7\r\n')
            until(lambda: "AT+CMGR=7" in modem.commands)
            stop = threading.Event()

            def flood():
                while not stop.wait(.002):
                    modem.send_data(b"\r\n^RSSI: 15\r\n")

            thread = threading.Thread(target=flood)
            thread.start()
            started = time.monotonic()
            try:
                driver.wait("DISCONNECT", timeout=.6)
                assert time.monotonic() - started < .5
            finally:
                stop.set()
                thread.join(timeout=1)
            assert 7 in modem.sms and 7 not in modem.deleted
            assert not list(spool.glob("*.sms"))


def urcs_after_idle_command_deadline(executable, tmp_path):
    """Milliseconds from writing a command to closing the port.

    Another thread writes the command during the 100 ms idle wait; it gets no
    reply, its 10 ms deadline passes inside that wait, and from 20 ms on ^RSSI
    arrives on the same port every 3 ms for about 0.9 s.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.script("AT+LOST", [b"\r\n^RSSI: 15\r\n"] * 300, delay=.02, chunk_delay=.003)
        with DriverSession(executable, modem, spool) as driver:
            driver.wait("READY")
            marker = len(driver.events)
            until(lambda: ("IDLE_WAIT",) in driver.events[marker:])
            driver.command("AT+LOST", timeout_ms=10)
            command, written_ms = driver.wait("WRITTEN")
            assert command == "AT+LOST"
            disconnected_ms, = driver.wait("DISCONNECT", timeout=3)
            assert any("timedout while waiting" in field
                       for event in driver.events for field in event)
            return int(disconnected_ms) - int(written_ms)


def test_urcs_do_not_extend_a_deadline_passed_in_the_idle_wait(driver_binary, tmp_path):
    assert urcs_after_idle_command_deadline(driver_binary, tmp_path) < 300


def test_upstream_loop_lets_urcs_extend_a_deadline_passed_in_the_idle_wait(
        driver_without_readable_deadline, tmp_path):
    """Why readable_deadline exists: upstream waits pvt->timeout again after the
    deadline, so every URC within that wait keeps the command pending."""
    assert urcs_after_idle_command_deadline(driver_without_readable_deadline, tmp_path) > 900


def test_hangup_after_complete_results_closes_at_once_and_recovers(driver_binary, tmp_path):
    """On a PTY one port needs no drain patch for POLLHUP/POLLERR.

    at_wait returns on any revents, the results already read are handled, and
    the next loop closes the hung-up port without waiting for the deadline.
    On hangup the kernel flushes the PTY's unread input, so a drain would find
    nothing to read. Not verified on the real USB tty.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=3000) as driver:
            driver.wait("READY")
            modem.put_sms(5, deliver_pdu("read again after the hangup"))
            modem.silence("AT+CMGR=5")
            modem.send_data(b'\r\n+CMTI: "ME",5\r\n')
            until(lambda: "AT+CMGR=5" in modem.commands)
            # A complete RING and a partial line arrive in one write, then the hangup.
            modem.send_data(b"\r\nRING\r\n\r\n+CM")
            assert driver.wait("RING") == ("1",)
            started = time.monotonic()
            modem.disconnect()
            driver.wait("DISCONNECT", timeout=1)
            assert time.monotonic() - started < .5
            assert not any("timedout" in field for event in driver.events for field in event)
            modem.clear_script("AT+CMGR=5")
            modem.reappear()
            driver.wait("READY")
            until(lambda: 5 in modem.deleted)
            assert texts(spool) == ["read again after the hangup"]


def test_timeout_reopens_and_recovers_without_repeating_urc(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=100) as driver:
            driver.wait("READY")
            modem.put_sms(5, deliver_pdu("recover command timeout"))
            modem.silence("AT+CMGR=5")
            modem.send_data(b'\r\n+CMTI: "ME",5\r\n')
            driver.wait("DISCONNECT")
            modem.clear_script("AT+CMGR=5")
            driver.wait("READY")
            assert 5 in modem.deleted
            fragments = [parse_fragment(path.read_bytes()) for path in spool.glob("*.sms")]
            assert len(fragments) == 1 and fragments[0]["text"] == "recover command timeout"


def test_replacing_the_port_discards_partial_stream_state(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            modem.send_data(b"\r\n+CMT")
            time.sleep(.005)
            modem.send_data(b'I: "SM",')
            time.sleep(.01)
            modem.disconnect()
            driver.wait("DISCONNECT")
            assert not modem.data_path.exists()
            modem.put_sms(8, deliver_pdu("arrived while the port was absent"))
            # Linux can reuse PTY numbers: actual close/new open is the property,
            # while the persistent symlink name remains stable for driver discovery.
            modem.reappear()
            driver.wait("READY")
            assert 8 in modem.deleted
            modem.send_data(b"\r\nRING\r\n")
            assert driver.wait("RING") == ("1",)
            assert len(list(spool.glob("*.sms"))) == 1


def test_cmgr_queue_deallocation_is_memory_safe(sanitized_driver, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(sanitized_driver, modem, spool) as driver:
            driver.wait("READY")
            # The actual CMGR handler dequeues and frees its command before
            # decoding. Any later ecmd->flags dereference is detected by ASan.
            for slot in (1, 2):
                modem.put_sms(slot, deliver_pdu(f"memory safety part {slot}"))
                modem.send_data(f'\r\n+CMTI: "ME",{slot}\r\n'.encode())
                until(lambda: slot in modem.deleted)
            assert driver.process.poll() is None
            assert not any("Sanitizer" in field or "runtime error:" in field
                           for event in driver.events for field in event)
            assert len(list(spool.glob("*.sms"))) == 2


@pytest.mark.parametrize("result", [b"BUSY", b"NO CARRIER", b"NO DIALTONE"])
def test_idle_call_result_without_queue_task_is_safe(driver_binary, tmp_path, result):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            modem.send_data(b"\r\n" + result + b"\r\n\r\nRING\r\n")
            assert driver.wait("RING") == ("1",)
            assert driver.process.poll() is None
            assert not any(event[0] in ("CONTROL", "DISCONNECT") for event in driver.events)


def test_ring_and_sms_on_the_data_port(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            modem.send_data(b'\r\nRING\r\n\r\n+CLIP: "+15551234567",145,,,,0\r\n')
            assert driver.wait("RING") == ("1",)
            modem.put_sms(3, deliver_pdu("single port"))
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: 3 in modem.deleted)
            assert texts(spool) == ["single port"]
            assert driver.process.poll() is None


def test_long_sms_parts_in_quick_succession(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=800) as driver:
            driver.wait("READY")
            for part in (1, 2, 3):
                modem.put_sms(part, deliver_pdu(f"part {part}", ref=7, total=3, part=part,
                                                stamp=f"2026-10-17T12:00:0{part}"))
            # Later notifications arrive while the first CMGR is being answered.
            modem.script("AT+CMGR=1", [cmgr_reply(modem.sms[1])], delay=.05)
            modem.send_data(b'\r\n+CMTI: "ME",1\r\n')
            time.sleep(.02)
            modem.send_data(b'\r\n+CMTI: "ME",2\r\n')
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: {1, 2, 3} <= set(modem.deleted), timeout=5)
            assert texts(spool) == ["part 1", "part 2", "part 3"]


def test_cmti_right_after_a_reply_in_one_read(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            modem.put_sms(3, deliver_pdu("first"))
            modem.put_sms(4, deliver_pdu("second"))
            modem.script("AT+CMGR=3", [cmgr_reply(modem.sms[3]) + b'\r\n+CMTI: "ME",4\r\n'])
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: {3, 4} <= set(modem.deleted))
            assert texts(spool) == ["first", "second"]


@pytest.mark.parametrize('position', ['before-pdu', 'after-pdu'])
def test_cmti_inside_a_cmgr_reply_leaves_both_sms_unread(driver_binary, tmp_path, position):
    """Two contaminated reads retain both SMS; a reconnect recovers them."""
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            reads = {slot: modem.commands.count(f"AT+CMGR={slot}") for slot in (3, 4)}
            modem.put_sms(3, deliver_pdu("first"))
            modem.put_sms(4, deliver_pdu("second"))
            reply = cmgr_reply(modem.sms[3])
            head = reply.index(b"\r\n", 2) + 2 if position == "before-pdu" else reply.index(b"\r\n\r\nOK")
            modem.script("AT+CMGR=3", [reply[:head], b'\r\n+CMTI: "ME",4\r\n', reply[head:]],
                         chunk_delay=.02)
            marker = len(driver.events)
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: ("RESPONSE", "+CMGR") in [event[:2] for event in driver.events[marker:]])
            until(lambda: any('failed two inconsistent reads' in ' '.join(event)
                              for event in driver.events[marker:]))
            modem.send_data(b"\r\nRING\r\n")
            assert driver.wait("RING") == ("1",)
            assert not any(event[:2] == ("RESPONSE", "+CMTI") and "4" in bytes.fromhex(event[2]).decode()
                           for event in driver.events[marker:])
            assert modem.commands.count("AT+CMGR=4") == reads[4]
            assert modem.commands.count("AT+CMGR=3") == reads[3] + 2
            assert {3, 4} <= set(modem.sms) and not modem.deleted
            assert not list(spool.glob("*.sms"))
            assert driver.process.poll() is None
            assert not any(event[0] == "DISCONNECT" for event in driver.events)
            # The next driver start reads both memories and finds both SMS.
            modem.clear_script("AT+CMGR=3")
            modem.disconnect()
            driver.wait("DISCONNECT")
            modem.reappear()
            driver.wait("READY")
            until(lambda: {3, 4} <= set(modem.deleted))
            assert texts(spool) == ["first", "second"]


@pytest.mark.parametrize("header", ["cmgr", "clcc"])
def test_unterminated_multiline_reply_ends_at_the_command_deadline(driver_binary, tmp_path, header):
    """A multiline reply that never ends is cut off by the command deadline.

    A +CMGR: or +CLCC: header answers the read; +CMTI, RING and ^RSSI follow,
    never OK. The multiline reply absorbs all of them (none is dispatched), the
    read expires at its deadline although ^RSSI keeps the port readable, the
    driver reconnects and its start scan reads both SMS from the SIM.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=500) as driver:
            driver.wait("READY")
            until(lambda: any("SMS rescan finished:" in line for line in logs(driver)))
            modem.put_sms(6, deliver_pdu("first"))
            modem.put_sms(7, deliver_pdu("second"))
            reply = cmgr_reply(modem.sms[6])
            head = (reply[:reply.index(b"\r\n\r\nOK")] if header == "cmgr"
                    else b'\r\n+CLCC: 1,1,4,0,0,"+15550000003",145\r\n')
            # ^RSSI every 10 ms for 2 s, far past the 500 ms deadline.
            modem.script("AT+CMGR=6", [head, b'\r\n+CMTI: "ME",7\r\n', b"\r\nRING\r\n",
                                       *[b"\r\n^RSSI: 15\r\n"] * 200], chunk_delay=.01)
            marker, sent = len(driver.events), len(modem.commands)
            modem.send_data(b'\r\n+CMTI: "ME",6\r\n')
            until(lambda: "AT+CMGR=6" in modem.commands[sent:])
            started = time.monotonic()
            read = modem.commands.index("AT+CMGR=6", sent)
            modem.clear_script("AT+CMGR=6")  # the queued chunks still flow; the next read gets the SMS
            driver.wait("DISCONNECT", timeout=3)
            assert time.monotonic() - started < 1.2
            before = []
            for event in driver.events[marker:]:
                if event[0] == "DISCONNECT":
                    break
                before.append(event)
            # Only the +CMTI that started the read is dispatched (no RING, no +CMGR), then the deadline expires.
            assert [event[:2] for event in before if event[0] != "IDLE_WAIT"] == [
                ("RESPONSE", "+CMTI"), ("LOG", "[dongle0] timedout while waiting '+CMGR' in response to 'test command'")]
            driver.wait("READY")
            until(lambda: {6, 7} <= set(modem.deleted))
            # Nothing was deleted or read between the cut-off read and the reconnect's AT.
            assert modem.commands[read:read + 2] == ["AT+CMGR=6", "AT"]
            assert texts(spool) == ["first", "second"]


def test_harness_retries_short_and_blocked_writes(driver_binary, tmp_path, monkeypatch):
    import os

    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            write = os.write
            attempts = 0
            target_fd = modem._master

            def interrupted_write(fd, content):
                nonlocal attempts
                if fd == target_fd:
                    attempts += 1
                    if attempts == 1:
                        return write(fd, content[:2])
                    if attempts == 2:
                        raise BlockingIOError("synthetic full PTY buffer")
                return write(fd, content)

            with monkeypatch.context() as patcher:
                patcher.setattr(os, "write", interrupted_write)
                modem.send_data(b"\r\nRING\r\n")
                assert driver.wait("RING") == ("1",)
                assert attempts >= 3
                assert len([event for event in driver.events if event[0] == "RING"]) == 1


def test_driver_rejects_another_harness_pty_before_open(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with PtyModem(tmp_path / "other-modem") as other:
            modem.data_path.unlink()
            modem.data_path.symlink_to(other.data_path.resolve())
            with pytest.raises(AssertionError, match="not this harness PTY"):
                DriverSession(driver_binary, modem, spool)


def test_start_journals_and_clears_the_sim_then_modem_memory(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.put_sms(3, deliver_pdu("sim three"), memory="SM")
        modem.put_sms(14, deliver_pdu("sim fourteen"), memory="SM")
        modem.put_sms(3, deliver_pdu("modem three"), memory="ME")
        modem.put_sms(5, deliver_pdu("modem five"), memory="ME")
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            assert modem.deletions == [("SM", 3), ("SM", 14), ("ME", 3), ("ME", 5)]
            assert violations == []
            assert modem.memories == {"SM": {}, "ME": {}}
            assert modem.selected == ["ME", "ME", "ME"]
            assert texts(spool) == ["modem five", "modem three", "sim fourteen", "sim three"]
            # ATZ, ME for all three memories before CNMI, then only the read
            # memory changes: the SIM is read through, then ME.
            commands = modem.commands
            assert storage(commands) == [INIT_ME, READ_SM, READ_ME]
            assert commands.index("ATZ") < commands.index(INIT_ME) < commands.index(CNMI) < commands.index(READ_SM)
            assert all(not command.startswith(("AT+CMGR", "AT+CMGD"))
                       for command in commands[:commands.index(READ_SM)])
            assert [memory for memory, _slot in modem.reads] == ["SM"] * 15 + ["ME"] * 23
            assert commands[commands.index(READ_ME) - 1] == "AT+CMGD=14"
            assert [line for line in logs(driver) if "SMS storage" in line][:3] == [
                "[dongle0] SMS storage ME selected for read, write and receive",
                "[dongle0] SMS storage SM selected for reading",
                "[dongle0] SMS storage ME selected for reading",
            ]
            assert not any(event[0] == "DISCONNECT" for event in driver.events)


def test_new_sms_in_modem_memory_is_read_without_another_switch(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            marker = len(modem.commands)
            assert modem.deliver(deliver_pdu("new in modem memory")) == ("ME", 0)
            until(lambda: ("ME", 0) in modem.deletions)
            assert modem.commands[marker:] == ["AT+CMGR=0", "AT+CMGD=0"]
            assert modem.reads[-1] == ("ME", 0) and violations == []
            assert texts(spool) == ["new in modem memory"]


def test_cmti_for_modem_memory_while_the_sim_is_read_waits_for_modem_memory(driver_binary, tmp_path):
    """+CMTI "ME",9 arrives while the read memory is SM and SIM slot 9 is unread.

    The modem still receives into ME and names it. The driver reads ME slot 9
    only after AT+CPMS="ME"; SIM slot 9 is read and deleted once, as the SIM.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.put_sms(9, deliver_pdu("sim nine"), memory="SM")
        for slot in range(5):
            modem.put_sms(slot, deliver_pdu(f"modem {slot}"), memory="ME")
        # Slot 5 is empty in both memories; its read answers late on the SIM.
        modem.script("AT+CMGR=5", [b"\r\n+CMS ERROR: 321\r\n"], delay=.2)
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            until(lambda: "AT+CMGR=5" in modem.commands)
            with modem._lock:
                assert modem.selected == ["SM", "ME", "ME"]
                assert modem.deliver(deliver_pdu("new modem nine"), slot=9) == ("ME", 9)
            driver.wait("READY")
            assert modem.reads.count(("SM", 9)) == 1 and modem.reads.count(("ME", 9)) == 1
            assert modem.reads.index(("ME", 9)) > modem.reads.index(("SM", 9))
            assert modem.deletions == [("SM", 9), *(("ME", slot) for slot in range(5)), ("ME", 9)]
            assert violations == []
            assert modem.memories == {"SM": {}, "ME": {}}
            assert storage(modem.commands) == [INIT_ME, READ_SM, READ_ME]
            assert texts(spool) == sorted(["sim nine", "new modem nine",
                                           *(f"modem {slot}" for slot in range(5))])


def test_late_cmti_for_a_slot_already_read_is_harmless(driver_binary, tmp_path):
    """A +CMTI the modem sends late, for an ME slot the start already took."""
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.put_sms(2, deliver_pdu("modem two"), memory="ME")
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            assert modem.deletions == [("ME", 2)]
            marker, events = len(modem.commands), len(driver.events)
            modem.send_data(b'\r\n+CMTI: "ME",2\r\n')
            until(lambda: "AT+CMGR=2" in modem.commands[marker:])
            modem.send_data(b"\r\nRING\r\n")
            driver.wait("RING")
            # The empty slot's bare OK is followed by AT before any further read (C2 guard).
            until(lambda: "AT" in modem.commands[marker:])
            assert modem.commands[marker:] == ["AT+CMGR=2", "AT"]
            assert modem.deletions == [("ME", 2)] and texts(spool) == ["modem two"]
            assert not any(event[0] == "DISCONNECT" for event in driver.events[events:])


def test_cmti_for_the_sim_reads_the_sim_and_returns_to_modem_memory(driver_binary, tmp_path):
    """+CMTI "SM" while ME is read: ME slot 2 is not taken for SIM slot 2.

    The driver selects SM for reading, reads all of it, selects ME again and
    reads all of ME, so the SMS that ME slot 2 holds is also journaled before
    its delete. Reception stays in ME throughout.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            marker, reads = len(modem.commands), len(modem.reads)
            modem.put_sms(2, deliver_pdu("class two on the sim"), memory="SM")
            modem.put_sms(2, deliver_pdu("modem slot two"), memory="ME")
            modem.send_data(b'\r\n+CMTI: "SM",2\r\n')
            until(lambda: len(modem.reads) == reads + 38 and len(modem.deletions) == 2)
            modem.send_data(b"\r\nRING\r\n")
            driver.wait("RING")
            commands = modem.commands[marker:]
            assert commands[0] == READ_SM
            assert storage(commands) == [READ_SM, READ_ME]
            # The last empty SIM slot's error is followed by AT before ME is selected.
            assert commands[commands.index("AT+CMGR=14"):commands.index(READ_ME)] == ["AT+CMGR=14", "AT"]
            assert modem.reads[reads:] == [("SM", slot) for slot in range(15)] + [("ME", slot) for slot in range(23)]
            assert modem.deletions == [("SM", 2), ("ME", 2)] and violations == []
            assert modem.memories == {"SM": {}, "ME": {}}
            assert modem.selected == ["ME", "ME", "ME"]
            assert texts(spool) == ["class two on the sim", "modem slot two"]


def test_sim_refused_for_reading_rereads_modem_memory_and_is_tried_again(driver_binary, tmp_path):
    """AT+CPMS="SM" fails while a +CMTI "ME" waits: ME is read again in full.

    The SIM is selected once more for the next +CMTI "SM", and then read.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            until(lambda: any("SMS rescan finished:" in line for line in logs(driver)))
            marker, reads, events = len(modem.commands), len(modem.reads), len(driver.events)
            modem.put_sms(2, deliver_pdu("waits on the sim"), memory="SM")
            modem.script(READ_SM, [b"\r\n+CMS ERROR: 302\r\n"], delay=.2)
            modem.send_data(b'\r\n+CMTI: "SM",2\r\n')
            until(lambda: READ_SM in modem.commands[marker:])
            assert modem.deliver(deliver_pdu("arrived meanwhile")) == ("ME", 0)
            until(lambda: len(modem.reads) == reads + 256)
            assert modem.reads[reads:] == [("ME", slot) for slot in range(256)]
            assert modem.deletions == [("ME", 0)]
            assert modem.memories["SM"] == {2: deliver_pdu("waits on the sim").upper()}
            modem.clear_script(READ_SM)
            modem.send_data(b'\r\n+CMTI: "SM",2\r\n')
            until(lambda: len(modem.reads) == reads + 256 + 38)
            modem.send_data(b"\r\nRING\r\n")
            driver.wait("RING")
            assert storage(modem.commands[marker:]) == [READ_SM, READ_SM, READ_ME]
            assert modem.reads[reads + 256:] == ([("SM", slot) for slot in range(15)]
                                                 + [("ME", slot) for slot in range(23)])
            assert modem.deletions == [("ME", 0), ("SM", 2)] and violations == []
            assert modem.memories == {"SM": {}, "ME": {}}
            assert texts(spool) == ["arrived meanwhile", "waits on the sim"]
            assert logs(driver, events) == [
                "[dongle0] Modem rejected SMS storage SM for reading, keeping ME",
                "[dongle0] SMS storage SM selected for reading",
                "[dongle0] SMS storage ME selected for reading",
            ]
            assert modem.selected == ["ME", "ME", "ME"]


def test_modem_memory_refused_after_the_sim_is_tried_again_on_the_next_cmti(driver_binary, tmp_path):
    """AT+CPMS="ME" fails once after the start has read the SIM.

    Reading stays on the SIM while the modem receives into ME; the next
    +CMTI "ME" selects ME once more and ME is read in full.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.put_sms(3, deliver_pdu("waited in modem memory"), memory="ME")
        modem.script(READ_ME, [b"\r\nERROR\r\n"])
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            assert storage(modem.commands) == [INIT_ME, READ_SM, READ_ME]
            assert "[dongle0] Modem rejected SMS storage ME for reading, keeping SM" in logs(driver)
            assert modem.selected == ["SM", "ME", "ME"] and modem.deletions == []
            until(lambda: any('SMS rescan finished:' in line for line in logs(driver)))
            modem.clear_script(READ_ME)
            marker, events = len(modem.commands), len(driver.events)
            assert modem.deliver(deliver_pdu("new in modem memory")) == ("ME", 0)
            until(lambda: len(modem.deletions) == 2)
            modem.send_data(b"\r\nRING\r\n")
            driver.wait("RING")
            assert modem.commands[marker] == READ_ME and storage(modem.commands[marker:]) == [READ_ME]
            assert modem.deletions == [("ME", 0), ("ME", 3)] and violations == []
            assert modem.memories == {"SM": {}, "ME": {}}
            assert modem.selected == ["ME", "ME", "ME"]
            assert texts(spool) == ["new in modem memory", "waited in modem memory"]
            assert logs(driver, events) == ["[dongle0] SMS storage ME selected for reading"]


@pytest.mark.parametrize("error", [b"\r\n+CMS ERROR: 302\r\n", b"\r\nERROR\r\n"], ids=["cms-error", "error"])
def test_modem_memory_refused_keeps_receiving_on_the_sim(driver_binary, tmp_path, error):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.refused = {"ME"}
        modem.storage_error = error
        # An earlier start left the receive memory at ME.
        modem.selected = ["SM", "SM", "ME"]
        modem.put_sms(1, deliver_pdu("old sim sms"), memory="SM")
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            assert "[dongle0] Modem rejected SMS storage ME, keeping SM" in logs(driver)
            assert "[dongle0] SMS storage SM selected for read, write and receive" in logs(driver)
            # The init list goes on (CNMI), and the SIM is selected for all three.
            commands = modem.commands
            assert storage(commands) == [INIT_ME, KEEP_SM]
            assert commands.index(INIT_ME) < commands.index(CNMI) < commands.index(KEEP_SM)
            assert modem.selected == ["SM", "SM", "SM"]
            assert modem.deletions == [("SM", 1)]
            until(lambda: any("SMS rescan finished:" in line for line in logs(driver)))
            # SMS reception stays on the SIM, with no reconnect.
            assert modem.deliver(deliver_pdu("received on the sim")) == ("SM", 0)
            until(lambda: ("SM", 0) in modem.deletions)
            modem.put_sms(7, deliver_pdu("left in modem memory"), memory="ME")
            marker, events = len(modem.commands), len(driver.events)
            # A +CMTI naming ME selects it once more; the modem refuses again.
            modem.send_data(b'\r\n+CMTI: "ME",7\r\n')
            until(lambda: len(logs(driver, events)) == 1)
            modem.send_data(b'\r\n+CMTI: "SR",1\r\n\r\nRING\r\n')
            driver.wait("RING")
            assert modem.commands[marker:] == [READ_ME]
            assert logs(driver, events) == [
                "[dongle0] Modem rejected SMS storage ME for reading, keeping SM",
                "[dongle0] New SMS in slot 1 of a memory that is not read; it stays there",
            ]
            assert modem.memories == {"SM": {}, "ME": {7: deliver_pdu("left in modem memory").upper()}}
            assert storage(modem.commands) == [INIT_ME, KEEP_SM, READ_ME]
            assert texts(spool) == ["old sim sms", "received on the sim"] and violations == []
            assert not any(event[0] == "DISCONNECT" for event in driver.events)


def test_reconnect_selects_modem_memory_again_and_clears_both(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            modem.disconnect()
            driver.wait("DISCONNECT")
            # The modem keeps its memories while the port is absent.
            assert modem.selected == ["ME", "ME", "ME"]
            modem.put_sms(4, deliver_pdu("waited on the sim"), memory="SM")
            modem.put_sms(9, deliver_pdu("waited in modem memory"), memory="ME")
            marker, deletions = len(modem.commands), len(modem.deletions)
            modem.reappear()
            driver.wait("READY")
            commands = modem.commands[marker:]
            assert storage(commands) == [INIT_ME, READ_SM, READ_ME]
            assert commands.index("ATZ") < commands.index(INIT_ME)
            assert not any(command.startswith(("AT+CMGR", "AT+CMGD"))
                           for command in commands[:commands.index(READ_SM)])
            assert modem.deletions[deletions:] == [("SM", 4), ("ME", 9)] and violations == []
            assert modem.selected == ["ME", "ME", "ME"]
            assert texts(spool) == ["waited in modem memory", "waited on the sim"]


def test_reconnect_tries_modem_memory_the_last_connection_refused(driver_binary, tmp_path):
    """What one connection learned about the memories does not outlive it."""
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        modem.refused = {"ME"}
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            assert storage(modem.commands) == [INIT_ME, KEEP_SM]
            modem.disconnect()
            driver.wait("DISCONNECT")
            modem.refused = set()
            modem.put_sms(4, deliver_pdu("waited on the sim"), memory="SM")
            modem.put_sms(9, deliver_pdu("waited in modem memory"), memory="ME")
            marker = len(modem.commands)
            modem.reappear()
            driver.wait("READY")
            assert storage(modem.commands[marker:]) == [INIT_ME, READ_SM, READ_ME]
            assert modem.deletions == [("SM", 4), ("ME", 9)] and violations == []
            assert modem.selected == ["ME", "ME", "ME"]
            assert texts(spool) == ["waited in modem memory", "waited on the sim"]



def test_retry_after_reconnect_reads_the_slot_a_journal_failure_held(driver_binary, tmp_path):
    """A slot held by a journal failure is retried after a reconnect.

    The journal fails for ME slot 11 and the port goes away with the slot
    marked and its retry pending; production keeps both in its one pvt. The
    full scan of the next connection reaches slot 11 again: with the journal
    still gone it stops there again. Once the journal is back, the retry 30 s later reads the slot,
    journals and deletes it, and the scan goes on to the slots after it. The
    start scan hides whether the retry was carried or set anew: this covers the
    recovery, not the carry itself.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    held = lambda: sum("retaining SMS on SIM" in " ".join(event) for event in driver.events)
    with PtyModem(tmp_path / "modem") as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            spool.rmdir()  # only the empty test-owned directory; never real storage
            modem.put_sms(11, deliver_pdu("held over the reconnect"))
            modem.send_data(b'\r\n+CMTI: "ME",11\r\n')
            until(lambda: held() == 1)
            modem.disconnect()
            driver.wait("DISCONNECT")
            modem.put_sms(15, deliver_pdu("after the held slot"))
            modem.reappear()
            driver.wait("READY")
            until(lambda: held() == 2)
            reads = len(modem.reads)
            assert modem.reads[-1] == ("ME", 11)
            assert sorted(modem.sms) == [11, 15] and modem.deletions == []
            spool.mkdir()
            driver.adapt('ADVANCE=29')
            time.sleep(.15)
            assert len(modem.reads) == reads and not modem.deletions
            driver.adapt('ADVANCE=1')
            until(lambda: ("ME", 15) in modem.deletions)
            assert modem.reads[reads:reads + 2] == [("ME", 11), ("ME", 12)]
            assert modem.deletions == [("ME", 11), ("ME", 15)] and violations == []
            assert texts(spool) == ["after the held slot", "held over the reconnect"]


def test_journal_retry_waits_30_monotonic_seconds_whatever_the_wall_clock_does(driver_binary, tmp_path):
    """A wall-clock step (NTP, a board without RTC) neither hurries nor delays the journal retry."""
    spool = tmp_path / "spool"
    spool.mkdir()
    held = lambda: sum("retaining SMS on SIM" in " ".join(event) for event in driver.events)
    with PtyModem(tmp_path / "modem") as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            spool.rmdir()  # only the empty test-owned directory; never real storage
            modem.put_sms(11, deliver_pdu("held across a clock step"))
            modem.send_data(b'\r\n+CMTI: "ME",11\r\n')
            until(lambda: held() == 1)
            reads = len(modem.reads)
            spool.mkdir()
            driver.adapt('WALL=3600')  # forward an hour: no early retry
            time.sleep(.15)
            assert len(modem.reads) == reads and not modem.deletions
            driver.adapt('WALL=-86400')  # back a day: the retry stays due 30 s after the failure
            driver.adapt('ADVANCE=29')
            time.sleep(.15)
            assert len(modem.reads) == reads and not modem.deletions
            driver.adapt('ADVANCE=1')
            until(lambda: ("ME", 11) in modem.deletions)
            assert modem.reads[reads] == ("ME", 11) and violations == []
            assert texts(spool) == ["held across a clock step"]


@pytest.mark.parametrize('urc', [b'^RSSI: 20', b'RING', b'+CLIP: "123",129', b'^BOOT: 1',
                                b'+CMTI: "ME",4'])
@pytest.mark.parametrize('position', ['before-pdu', 'after-pdu'])
def test_one_contaminated_cmgr_is_retried_without_quarantine(driver_binary, tmp_path, monkeypatch,
                                                           urc, position):
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            initial = modem.commands.count('AT+CMGR=3')
            modem.put_sms(3, deliver_pdu('intact SMS'))
            good = cmgr_reply(modem.sms[3])
            offset = good.index(b'\r\n', 2) + 2 if position == 'before-pdu' else good.index(b'\r\n\r\nOK')
            bad = good[:offset] + b'\r\n' + urc + b'\r\n' + good[offset:]
            scripted_reads(monkeypatch, modem, 'AT+CMGR=3', [bad])
            def before_delete(memory, slot):
                assert memory == 'ME' and slot == 3
                assert texts(spool) == ['intact SMS']
            modem.on_delete = before_delete
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: 3 in modem.deleted)
            assert modem.commands.count('AT+CMGR=3') == initial + 2
            assert modem.deleted == [3] and texts(spool) == ['intact SMS']
            assert len(list(spool.glob('*.sms'))) == 1


@pytest.mark.parametrize('binary_fixture', ['driver_binary', 'sanitized_driver'])
def test_identical_unparseable_pdu_is_quarantined_before_delete(request, binary_fixture, tmp_path):
    executable = request.getfixturevalue(binary_fixture)
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        modem.put_sms(1, deliver_pdu('before'))
        modem.put_sms(2, '00FF')  # malformed PDU, stable across reads
        modem.put_sms(3, deliver_pdu('after'))
        published = []

        def before_delete(memory, slot):
            records = [parse_fragment(path.read_bytes()) for path in spool.glob('*.sms')]
            assert any(row['slot'] == slot for row in records)
            published.append(slot)
        modem.on_delete = before_delete
        with DriverSession(executable, modem, spool) as driver:
            driver.wait('READY')
            assert modem.deletions == [('ME', 1), ('ME', 2), ('ME', 3)]
            assert published == [1, 2, 3]
            assert modem.reads.count(('ME', 2)) == 2
            assert not any('00FF' in ' '.join(event) for event in driver.events if event[0] == 'LOG')
            assert not any('Sanitizer' in field or 'runtime error:' in field
                           for event in driver.events for field in event)
    records = [parse_fragment(path.read_bytes()) for path in spool.glob('*.sms')]
    [bad] = [row for row in records if row['source_id'].startswith('unparsed:')]
    assert bad['pdu'] == '00FF' and bad['slot'] == 2
    assert sorted(row['text'] for row in records if 'text' in row) == ['after', 'before']


@pytest.mark.parametrize('header', ['0', '0,{length}', '0 {length}'],
                         ids=['status-only', 'one-comma', 'no-comma'])
def test_cmgr_header_without_two_commas_is_quarantined(driver_binary, tmp_path, monkeypatch, header):
    spool = tmp_path/'spool'
    spool.mkdir()
    pdu = deliver_pdu('short header').upper()
    length = len(bytes.fromhex(pdu)) - 1 - int(pdu[:2], 16)
    bad = f'\r\n+CMGR: {header.format(length=length)}\r\n{pdu}\r\n\r\nOK\r\n'.encode()
    with PtyModem(tmp_path/'modem') as modem:
        modem.put_sms(1, deliver_pdu('before'))
        modem.put_sms(2, pdu)
        modem.put_sms(3, deliver_pdu('after'))
        scripted_reads(monkeypatch, modem, 'AT+CMGR=2', [bad, bad], memory='ME')
        published = []

        def before_delete(memory, slot):
            assert any(row['slot'] == slot for row in fragments(spool))
            published.append(slot)
        modem.on_delete = before_delete
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            until(lambda: len(modem.deletions) == 3)
            assert modem.deletions == [('ME', 1), ('ME', 2), ('ME', 3)]
            assert published == [1, 2, 3]
            # Both reads of slot 2 got the scripted reply: one retry, then quarantine.
            assert ('ME', 2) not in modem.reads
            assert any('unparsed after two identical reads' in ' '.join(event) for event in driver.events)
            modem.send_data(b'\r\nRING\r\n')
            driver.wait('RING')
    records = fragments(spool)
    [unparsed] = [row for row in records if row['source_id'].startswith('unparsed:')]
    assert unparsed['pdu'] == pdu and unparsed['slot'] == 2
    assert sorted(row['text'] for row in records if 'text' in row) == ['after', 'before']


def test_inconsistent_failed_reads_remain_in_modem(driver_binary, tmp_path, monkeypatch):
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            initial = modem.commands.count('AT+CMGR=3')
            modem.put_sms(3, deliver_pdu('not corrupted in storage'))
            scripted_reads(monkeypatch, modem, 'AT+CMGR=3', [cmgr_reply('00FF'), cmgr_reply('00FE')])
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: any('failed two inconsistent reads' in ' '.join(event) for event in driver.events))
            modem.send_data(b'\r\nRING\r\n')
            driver.wait('RING')
            assert modem.commands.count('AT+CMGR=3') == initial + 2
            assert 3 in modem.sms and not modem.deleted and not list(spool.glob('*.sms'))
            # Another independent arrival remains readable after the failed pair.
            modem.put_sms(4, deliver_pdu('unrelated SMS'))
            modem.send_data(b'\r\n+CMTI: "ME",4\r\n')
            until(lambda: 4 in modem.deleted)
            assert modem.commands.count('AT+CMGR=3') == initial + 2
            assert texts(spool) == ['unrelated SMS']


def test_unparsed_journal_failure_retains_slot_and_recovers(driver_binary, tmp_path):
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            spool.rmdir()
            modem.put_sms(3, '00FF')
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: any('retaining SMS on SIM' in ' '.join(event) for event in driver.events))
            assert 3 in modem.sms and not modem.deleted
            spool.mkdir()
            modem.disconnect()
            driver.wait('DISCONNECT')
            modem.reappear()
            driver.wait('READY')
            assert modem.deleted == [3]
            [record] = list(spool.glob('*.sms'))
            assert parse_fragment(record.read_bytes())['pdu'] == '00FF'


def test_ucs2_embedded_nul_is_journaled_in_full(driver_binary, tmp_path):
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        modem.put_sms(3, deliver_pdu('before\x00after'))
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            assert modem.deleted == [3]
    assert texts(spool) == ['before\x00after']


@pytest.mark.parametrize('pending', [False, True], ids=['empty-queue', 'different-command'])
@pytest.mark.parametrize('binary_fixture', ['driver_binary', 'sanitized_driver'])
def test_late_cmgs_does_not_touch_missing_or_unrelated_task(request, binary_fixture, tmp_path, pending):
    executable = request.getfixturevalue(binary_fixture)
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        with DriverSession(executable, modem, spool) as driver:
            driver.wait('READY')
            if pending:
                modem.silence('AT+WAIT')
                driver.command('AT+WAIT', timeout_ms=2000)
                driver.wait('WRITTEN')
                modem.send_data(b'\r\n+CMGS: 7\r\n')
            else:
                # The preceding OK removes the idle ping before +CMGS arrives.
                modem.script('AT', [b'\r\nOK\r\n\r\n+CMGS: 7\r\n'])
            until(lambda: any('Unexpected +CMGS' in ' '.join(event) for event in driver.events))
            modem.clear_script('AT')
            if pending:
                modem.send_data(b'\r\nOK\r\n')
                driver.wait('COMMAND_OK')
            modem.send_data(b'\r\nRING\r\n')
            driver.wait('RING')
            assert driver.process.poll() is None
            assert not any(event[0] in ('DISCONNECT', 'CMGS_STORED') for event in driver.events)
            assert not any('Sanitizer' in field or 'runtime error:' in field
                           for event in driver.events for field in event)


@pytest.mark.parametrize('binary_fixture', ['driver_binary', 'sanitized_driver'])
def test_cmgs_full_send_exchange_stores_task_uid(request, binary_fixture, tmp_path):
    executable = request.getfixturevalue(binary_fixture)
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        modem.script('AT+CMGS=1', [b'\r\n> '])
        modem.script('00', [b'\r\n+CMGS: 7\r\n\r\nOK\r\n'])
        with DriverSession(executable, modem, spool) as driver:
            driver.wait('READY')
            driver.command('SEND_SMS', timeout_ms=2000)
            driver.wait('WRITTEN')
            assert driver.wait('CMGS_STORED') == ('4242', '7')
            until(lambda: ('RESPONSE', 'OK', '4f4b') in driver.events[
                next(i for i, event in enumerate(driver.events) if event[0] == 'CMGS_STORED'):])
            driver.command('AT+AFTERSEND', timeout_ms=1000)
            driver.wait('WRITTEN')
            driver.wait('COMMAND_OK')
            assert modem.commands.index('AT+CMGS=1') < modem.commands.index('00')
            assert [event for event in driver.events if event[0] == 'CMGS_STORED'] == [('CMGS_STORED', '4242', '7')]
            assert not any('Unexpected +CMGS' in ' '.join(event) for event in driver.events)
            assert not any('Sanitizer' in field or 'runtime error:' in field
                           for event in driver.events for field in event)


async def test_split_ucs2_surrogates_are_journaled_and_assembled(driver_binary, tmp_path, db):
    from tests.test_ingress_replay import replay

    spool = tmp_path/'spool'
    spool.mkdir()
    # Replace a BMP code unit in each synthetic PDU with half of U+1F600.
    first = deliver_pdu('before X', ref=42, total=2, part=1)[:-4] + 'D83D'
    second = deliver_pdu('X after 😀', ref=42, total=2, part=2).replace('0058', 'DE00', 1)
    with PtyModem(tmp_path/'modem') as modem:
        modem.put_sms(3, first)
        modem.put_sms(4, second)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            assert modem.deletions == [('ME', 3), ('ME', 4)]
            assert modem.reads.count(('ME', 3)) == modem.reads.count(('ME', 4)) == 1
    records = list(spool.glob('*.sms'))
    assert len(records) == 2 and all(path.read_bytes().startswith(b'1\t') for path in records)
    assert texts(spool) == ['before \ufffd', '\ufffd after 😀']
    await replay(db, spool).replay_once()
    [sms] = await db.list_sms()
    assert sms['text'] == 'before \ufffd\ufffd after 😀' and not sms['partial']


@pytest.mark.parametrize('reply', [b'ERROR', b'OK'])
def test_failed_retry_ack_does_not_leak_to_next_slot(driver_binary, tmp_path, monkeypatch, reply):
    # Defense in depth: at_enqueue_retrieve_sms already clears the retry on every fresh read.
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            initial = modem.commands.count('AT+CMGR=3')
            modem.put_sms(3, '00FF')
            # If the first slot's retry leaks, the next slot's identical bad
            # first reply could be quarantined instead of retried successfully.
            modem.put_sms(4, deliver_pdu('next slot read fresh'))
            scripted_reads(monkeypatch, modem, 'AT+CMGR=3', [cmgr_reply('00FF'), b'\r\n' + reply + b'\r\n'])
            scripted_reads(monkeypatch, modem, 'AT+CMGR=4', [cmgr_reply('00FF')])
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n\r\n+CMTI: "ME",4\r\n')
            until(lambda: 4 in modem.deleted)
            assert modem.commands.count('AT+CMGR=3') == initial + 2
            assert 3 in modem.sms and 3 not in modem.deleted
            assert texts(spool) == ['next slot read fresh']
            assert len(list(spool.glob('*.sms'))) == 1


def test_disconnect_queued_retry_starts_fresh_after_reconnect(driver_binary, tmp_path, monkeypatch):
    # Defense in depth: disconnect already resets incoming_sms_index; the reset is not the only guard.
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=3000) as driver:
            driver.wait('READY')
            initial = modem.commands.count('AT+CMGR=3')
            modem.put_sms(3, deliver_pdu('fresh retry after reconnect'))
            scripted_reads(monkeypatch, modem, 'AT+CMGR=3', [cmgr_reply('00FF'), b''])
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n')
            until(lambda: modem.commands.count('AT+CMGR=3') == initial + 2)
            assert not modem.deleted and not list(spool.glob('*.sms'))
            modem.disconnect()
            driver.wait('DISCONNECT')
            # The first failed read after reconnect must earn its own retry.
            monkeypatch.undo()
            modem.clear_script('AT+CMGR=3')
            scripted_reads(monkeypatch, modem, 'AT+CMGR=3', [cmgr_reply('00FF')], memory='ME')
            modem.reappear()
            driver.wait('READY')
            assert modem.deleted == [3]
            assert texts(spool) == ['fresh retry after reconnect']
            # SM: the retained inbox bit, then the full read; ME: the failed read and its own retry.
            assert modem.commands.count('AT+CMGR=3') == initial + 6


@pytest.mark.parametrize('binary_fixture', ['driver_binary', 'sanitized_driver'])
@pytest.mark.parametrize('total,part', [(2, 0), (2, 3)], ids=['part-0', 'part-above-total'])
def test_invalid_concatenation_header_is_journaled_unparsed_and_deleted(request, binary_fixture, tmp_path,
                                                                        total, part):
    """A part numbered outside its total never becomes a valid record: no retry holds the slot (C1)."""
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(request.getfixturevalue(binary_fixture), modem, spool) as driver:
            driver.wait('READY')
            bad = deliver_pdu('invalid header', ref=7, total=total, part=part)
            modem.put_sms(3, bad)
            modem.put_sms(4, deliver_pdu('next SMS'))
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n\r\n+CMTI: "ME",4\r\n')
            until(lambda: {3, 4} <= set(modem.deleted))
            assert violations == [] and modem.sms == {}
            records = [parse_fragment(path.read_bytes()) for path in spool.glob('*.sms')]
            [unparsed] = [row for row in records if row['source_id'].startswith('unparsed:')]
            assert unparsed['slot'] == 3 and unparsed['pdu'].upper() == bad
            assert [row['text'] for row in records if 'text' in row] == ['next SMS']
            assert f'[dongle0] SMS slot 3 has an invalid concatenation header (part {part} of {total})' in logs(driver)
            assert not any('journal failed' in line for line in logs(driver))
            assert not any('invalid header' in ' '.join(event) for event in driver.events if event[0] == 'LOG')
            assert not any('Sanitizer' in field or 'runtime error:' in field
                           for event in driver.events for field in event)


@pytest.mark.parametrize('extra', [b'ERROR', b'OK'])
def test_extra_final_reply_before_cmgr_never_deletes_an_unjournaled_slot(driver_binary, tmp_path, monkeypatch,
                                                                        extra):
    """One extra final reply ahead of slot 3's +CMGR is not taken for slot 4's reply (C2).

    Without the AT after it, slot 3's PDU is journaled as slot 4 and slot 4 is
    deleted unjournaled. With it, slot 3 waits in the modem for the next pass.
    """
    spool = tmp_path/'spool'
    spool.mkdir()
    with PtyModem(tmp_path/'modem') as modem:
        violations = journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            modem.put_sms(3, deliver_pdu('slot three'))
            modem.put_sms(4, deliver_pdu('slot four'))
            scripted_reads(monkeypatch, modem, 'AT+CMGR=3', [b'\r\n' + extra + b'\r\n' + cmgr_reply(modem.sms[3])])
            modem.send_data(b'\r\n+CMTI: "ME",3\r\n\r\n+CMTI: "ME",4\r\n')
            until(lambda: 4 in modem.deleted)
            modem.send_data(b'\r\nRING\r\n')
            driver.wait('RING')
            assert violations == []
            assert [(row['slot'], row['text']) for row in fragments(spool)] == [(4, 'slot four')]
            assert 3 in modem.sms and 3 not in modem.deleted
            assert not any(event[0] == 'DISCONNECT' for event in driver.events)
            # The periodic pass takes slot 3 with its own reply.
            driver.adapt('ADVANCE=600')
            until(lambda: 3 in modem.deleted)
            assert violations == [] and modem.sms == {}
            assert sorted((row['slot'], row['text']) for row in fragments(spool)) == [(3, 'slot three'),
                                                                                    (4, 'slot four')]
