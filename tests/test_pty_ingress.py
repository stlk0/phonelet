"""Real serial/parser/journal -> production replay/SQLite integration.

The modem and Asterisk scheduling are isolated adapters. No Asterisk service,
network notification backend or real serial device is involved.
"""
import csv
import functools
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from phonelet.db import Database
from phonelet.cdr_events import CdrHandler
from phonelet.ingress import IngressReplay, parse_fragment
from phonelet.notification import deliver_pending
from tests import pty_modem
from tests.helpers import DeliversByMethod
from tests.pty_modem import DriverSession, PtyModem
from tests.sms_pdu import deliver_pdu

pytestmark = pytest.mark.driver

# Every cross-process wait in this module is bounded at 5 s.
until = functools.partial(pty_modem.until, timeout=5)


def receive(modem, slot, text, **kwargs):
    count = modem.deleted.count(slot)
    modem.put_sms(slot, deliver_pdu(text, **kwargs))
    modem.send_data(f'\r\n+CMTI: "ME",{slot}\r\n'.encode())
    until(lambda: modem.deleted.count(slot) > count)


def consumer(db, spool):
    async def on_call(**fields):
        await db.insert_call(**fields)

    handler = CdrHandler(on_call, AsyncMock())
    return IngressReplay(db, handler, spool, spool.parent / "phonelet.csv")


async def test_interleaved_sms_is_durable_and_delivered_once(driver_binary, tmp_path, db):
    spool = tmp_path / "spool"
    spool.mkdir()
    db.backends = ("test",)
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            # A fragmented URC triggers a command on the same port.
            modem.send_data(b'\r\n+CMT')
            receive_data = deliver_pdu("Hello, мир!\nnext line")
            modem.put_sms(7, receive_data)
            modem.send_data(b'I: "ME",7\r\n')
            until(lambda: 7 in modem.deleted)
            modem.send_data(b"\r\nRING\r\n")
            driver.wait("RING")

            replay = consumer(db, spool)
            await replay.replay_once()
            assert replay.errors == 0
            rows = await db.list_sms()
            assert len(rows) == 1
            assert rows[0]["sender"] == "+15551234567"
            assert rows[0]["text"] == "Hello, мир!\nnext line"
            assert not rows[0]["partial"]
            # Re-read byte-identical PDU in the same SIM slot after a modem retry.
            receive(modem, 7, "Hello, мир!\nnext line")
            await consumer(db, spool).replay_once()
            assert len(await db.list_sms()) == 1
            assert sum(row["count"] for row in await db.outbox_stats()) == 1

    class LocalNotifier(DeliversByMethod):
        backend_id = "test"
        notify_sms = AsyncMock(return_value=True)

    notifier = LocalNotifier()
    await deliver_pending(db, [notifier])
    await consumer(db, spool).replay_once()
    await deliver_pending(db, [notifier])
    notifier.notify_sms.assert_awaited_once_with(
        sender="+15551234567", text="Hello, мир!\nnext line"
    )
    assert [row["status"] for row in await db.outbox_stats()] == ["delivered"]


@pytest.mark.parametrize("wide_ref", [False, True], ids=["8-bit-reference", "16-bit-reference"])
@pytest.mark.parametrize("order", [(1, 2), (2, 1)], ids=["in-order", "reversed"])
async def test_multipart_real_pdu_assembly(driver_binary, tmp_path, db, wide_ref, order):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            for part in order:
                receive(modem, part, {1: "first ", 2: "вторая"}[part],
                        ref=513 if wide_ref else 42, total=2, part=part, wide_ref=wide_ref)
                await consumer(db, spool).replay_once()
            rows = await db.list_sms()
            assert len(rows) == 1
            assert rows[0]["text"] == "first вторая" and not rows[0]["partial"]
            fragments = [parse_fragment(path.read_bytes()) for path in spool.glob("*.sms")]
            assert {fragment["part"] for fragment in fragments} == {1, 2}
            assert {fragment["ref"] for fragment in fragments} == {513 if wide_ref else 42}


async def test_partial_then_late_sibling_after_port_and_python_restart(driver_binary, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    path = str(tmp_path / "history.db")
    db = Database(path, ("test",))
    await db.init()
    try:
        with PtyModem(tmp_path / "modem") as modem:
            with DriverSession(driver_binary, modem, spool) as driver:
                driver.wait("READY")
                receive(modem, 3, "first ", ref=44, total=2, part=1)
                await consumer(db, spool).replay_once()
                assert await db.list_sms() == []
                await db.flush_fragments(60, now=time.time() + 61)
                first = (await db.list_sms())[0]
                assert first["text"] == "first [missing part 2/2]" and first["partial"]
                modem.disconnect()
                driver.wait("DISCONNECT")
                await db.close()
                db = Database(path, ("test",))
                await db.init()
                modem.reappear()
                driver.wait("READY")
                receive(modem, 4, "second", ref=44, total=2, part=2)
                await consumer(db, spool).replay_once()
                rows = await db.list_sms()
                assert len(rows) == 1
                assert rows[0]["id"] == first["id"]
                assert rows[0]["text"] == "first second" and not rows[0]["partial"]
                assert sum(row["count"] for row in await db.outbox_stats()) == 2
    finally:
        await db.close()


async def test_reused_reference_and_ambiguous_generation_do_not_splice(driver_binary, tmp_path, db):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            # Interleave two unambiguous generations sharing a concatenation ref.
            for slot, part, generation in [(1, 1, 0), (2, 1, 1), (3, 2, 0), (4, 2, 1)]:
                receive(modem, slot, f"G{generation}P{part}", ref=42, total=2, part=part,
                        stamp=f"2026-10-17T12:0{generation}:00")
            await consumer(db, spool).replay_once()
            rows = await db.list_sms()
            assert sorted(row["text"] for row in rows) == ["G0P1G0P2", "G1P1G1P2"]
            # Exact SCTS/ref collisions retain the ambiguity rather than guessing.
            for slot, part in [(5, 1), (6, 1), (7, 2), (8, 2)]:
                receive(modem, slot, f"collision-{slot}", ref=43, total=2, part=part)
                await consumer(db, spool).replay_once()
            await db.flush_fragments(60, now=time.time() + 61)
            rows = await db.list_sms()
            assert len(rows) == 6
            assert len([row for row in rows if row["partial"]]) == 4
            assert all("ambiguous multipart" in row["text"] for row in rows if row["partial"])


async def test_16_bit_references_with_same_low_byte_remain_distinct(driver_binary, tmp_path, db):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            for slot, ref, part, body in [
                (1, 0x0101, 1, "A-first "), (2, 0x0201, 1, "B-first "),
                (3, 0x0101, 2, "A-last"), (4, 0x0201, 2, "B-last"),
            ]:
                receive(modem, slot, body, ref=ref, total=2, part=part, wide_ref=True)
                await consumer(db, spool).replay_once()
            rows = await db.list_sms()
            assert sorted(row["text"] for row in rows) == ["A-first A-last", "B-first B-last"]
            assert all(not row["partial"] for row in rows)


async def test_database_commit_failure_replays_real_driver_journal(driver_binary, tmp_path, db, monkeypatch):
    spool = tmp_path / "spool"
    spool.mkdir()
    db.backends = ("test",)
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            receive(modem, 5, "retained during database outage")
    files = list(spool.glob("*.sms"))
    assert files
    commit = db._conn.commit
    failed = False

    async def fail_first_commit():
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("full")
        await commit()

    with monkeypatch.context() as patcher:
        patcher.setattr(db._conn, "commit", fail_first_commit)
        replay = consumer(db, spool)
        await replay.replay_once()
        assert replay.errors == 1
        assert await db.list_sms() == [] and await db.outbox_stats() == []
    assert db._conn.commit == commit
    await db.close()
    await db.init()
    await consumer(db, spool).replay_once()
    await consumer(db, spool).replay_once()
    assert len(await db.list_sms()) == 1
    assert sum(row["count"] for row in await db.outbox_stats()) == 1
    assert all(path.exists() for path in files)


async def test_disconnect_after_spool_before_sim_delete_replays_once(driver_binary, tmp_path, db):
    spool = tmp_path / "spool"
    spool.mkdir()
    db.backends = ("test",)
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool, timeout_ms=1000) as driver:
            driver.wait("READY")
            modem.silence("AT+CMGD=9")
            modem.put_sms(9, deliver_pdu("already durable, still on SIM"))
            modem.send_data(b'\r\n+CMTI: "ME",9\r\n')
            until(lambda: "AT+CMGD=9" in modem.commands)
            assert 9 not in modem.deleted
            assert list(spool.glob("*.sms"))
            await consumer(db, spool).replay_once()
            assert len(await db.list_sms()) == 1
            modem.disconnect()
            driver.wait("DISCONNECT")
            modem.clear_script("AT+CMGD=9")
            modem.reappear()
            driver.wait("READY")  # startup scan recovers SIM without another CMTI
            until(lambda: 9 in modem.deleted)
            await consumer(db, spool).replay_once()
            rows = await db.list_sms()
            assert len(rows) == 1 and rows[0]["text"] == "already durable, still on SIM"
            fragments = [parse_fragment(path.read_bytes()) for path in spool.glob("*.sms")]
            assert len(fragments) >= 2
            assert len({fragment["source_id"] for fragment in fragments}) == 1
            assert sum(row["count"] for row in await db.outbox_stats()) == 1


async def test_spool_failure_retains_sim_until_reopen_and_replay(driver_binary, tmp_path, db):
    spool = tmp_path / "spool"
    spool.mkdir()
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            spool.rmdir()  # only the empty test-owned directory; never real storage
            modem.put_sms(11, deliver_pdu("retained when journal is unavailable"))
            modem.send_data(b'\r\n+CMTI: "ME",11\r\n')
            until(lambda: any("retaining SMS on SIM" in " ".join(event) for event in driver.events))
            assert 11 in modem.sms and 11 not in modem.deleted
            assert await db.list_sms() == []
            spool.mkdir()
            modem.disconnect()
            driver.wait("DISCONNECT")
            modem.reappear()
            driver.wait("READY")
            until(lambda: 11 in modem.deleted)
            await consumer(db, spool).replay_once()
            rows = await db.list_sms()
            assert len(rows) == 1 and rows[0]["text"] == "retained when journal is unavailable"
            assert not rows[0]["partial"]


async def test_sms_left_in_modem_memory_are_taken_as_incoming_at_start(driver_binary, tmp_path, db):
    """Occupied ME slots, like the seven old SMS on the stand, become history at start."""
    spool = tmp_path / "spool"
    spool.mkdir()
    old = {slot: f"old modem sms {slot}" for slot in range(7)}
    with PtyModem(tmp_path / "modem") as modem:
        for slot, text in old.items():
            modem.put_sms(slot, deliver_pdu(text, stamp=f"2026-10-17T12:00:0{slot}"))
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            assert modem.deletions == [("ME", slot) for slot in old]
            assert modem.memories == {"SM": {}, "ME": {}}
    replay = consumer(db, spool)
    await replay.replay_once()
    assert replay.errors == 0
    rows = await db.list_sms()
    assert sorted(row["text"] for row in rows) == sorted(old.values())
    assert not any(row["partial"] for row in rows)


@pytest.mark.parametrize("disposition,userfield,status", [
    ("ANSWERED", "", "answered"), ("NO ANSWER", "", "missed"), ("BUSY", "", "rejected"),
    ("NO ANSWER", ";phonelet-call-rejected", "rejected"),
])
async def test_incoming_ring_then_live_and_retained_cdr(driver_binary, tmp_path, db, disposition, userfield, status):
    spool = tmp_path / "spool"
    spool.mkdir()
    db.backends = ("test",)
    with PtyModem(tmp_path / "modem") as modem:
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait("READY")
            modem.send_data(b"\r\nRI")
            modem.send_data(b"NG\r\n")
            driver.wait("RING")
    # Asterisk owns channel/CDR generation. Model its documented output boundary,
    # then exercise both actual application ingestion paths for the same call.
    event = {"UniqueID": "test-pty-call", "Channel": "Dongle/dongle0-1",
             "LastApplication": "Dial", "Source": "+15551234567", "Disposition": disposition,
             "BillableSeconds": "4", "UserField": userfield, "StartTime": "2026-10-17T12:00:00+00:00"}
    replay = consumer(db, spool)
    await replay.cdr_handler.handle_cdr(event)
    with replay.cdr_path.open("w", newline="") as stream:
        csv.writer(stream).writerow(event[name] for name in (
            "UniqueID", "Channel", "LastApplication", "Source", "Disposition",
            "BillableSeconds", "UserField", "StartTime"))
    await consumer(db, spool).replay_once()
    calls = await db.list_calls()
    assert len(calls) == 1 and calls[0]["status"] == status
    assert calls[0]["caller"] == "+15551234567"
    assert calls[0]["source_id"] == "cdr:test-pty-call"
    assert sum(row["count"] for row in await db.outbox_stats()) == (0 if status == "rejected" else 1)


@pytest.mark.parametrize('read_reply', [b'\r\nERROR\r\n', b'\r\nOK\r\n'], ids=['error', 'ok-without-cmgr'])
def test_sms_sync_error_keeps_unjournaled_slot_and_drains_next(driver_binary, tmp_path, read_reply):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        violations = pty_modem.journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool) as driver:
            driver.wait('READY')
            modem.script('AT+CMGR=4', [read_reply])
            modem.script('AT', [b'\r\nERROR\r\n'])
            modem.put_sms(4, deliver_pdu('unanswered read stays in memory'))
            modem.put_sms(5, deliver_pdu('next slot is durable before deletion'))
            marker = len(modem.commands)
            modem.send_data(b'\r\n+CMTI: "ME",4\r\n\r\n+CMTI: "ME",5\r\n')
            until(lambda: 5 in modem.deleted)
            commands = modem.commands[marker:]
            assert commands.index('AT+CMGR=4') < commands.index('AT') < commands.index('AT+CMGR=5')
            assert 'AT+CMGD=4' not in commands and 4 in modem.sms
            assert pty_modem.texts(spool) == ['next slot is durable before deletion']
            assert not violations


def test_sms_sync_timeout_reopens_and_recovers_once(driver_binary, tmp_path):
    spool = tmp_path / 'spool'
    spool.mkdir()
    with PtyModem(tmp_path / 'modem') as modem:
        violations = pty_modem.journal_before_delete(modem, spool)
        with DriverSession(driver_binary, modem, spool, timeout_ms=100) as driver:
            driver.wait('READY')
            modem.put_sms(4, deliver_pdu('recovered after a silent AT barrier'))
            modem.script('AT+CMGR=4', [b'\r\nERROR\r\n'])
            modem.silence('AT')
            marker = len(modem.commands)
            modem.send_data(b'\r\n+CMTI: "ME",4\r\n')
            until(lambda: 'AT' in modem.commands[marker:])
            # The driver must hit its own deadline; no synthetic unplug triggers it.
            driver.wait('DISCONNECT')
            assert not list(spool.glob('*.sms'))
            assert 'AT+CMGD=4' not in modem.commands[marker:] and 4 in modem.sms
            modem.clear_script('AT+CMGR=4')
            modem.clear_script('AT')
            driver.wait('READY')
            until(lambda: 4 in modem.deleted)
            commands = modem.commands[marker:]
            assert 'ATZ' in commands  # reinitialization includes a fresh memory scan
            assert commands.count('AT+CMGR=4') >= 2
            assert pty_modem.texts(spool) == ['recovered after a silent AT barrier']
            assert modem.deleted.count(4) == 1 and not violations
