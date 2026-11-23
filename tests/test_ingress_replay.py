import csv
import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from phonelet.db import Database
from phonelet.cdr_events import CdrHandler
from phonelet.ingress import IngressReplay, parse_fragment
from phonelet.notifiers.sip import SipDelivery
from phonelet.sms_commands import envelope
from tests.helpers import cdr_line


def record(text='hello', part=1, total=1, stamp='2026-09-25 12:00:00 +00:00', slot=1, ref=42, sender='+7'):
    fields = ['1', *[s.encode().hex() for s in ('dongle0', 'sim1', stamp, sender)],
              str(ref), str(total), str(part), text.encode().hex(), str(slot), ('AABB' + str(part) + str(slot))]
    return ('\t'.join(fields) + '\n').encode()


T0 = 1_790_000_000.0  # synthetic receipt clock for direct ingest tests
PARTS = {1: 'first ', 2: 'second ', 3: 'third'}
FULL = 'first second third'


def scts(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%d %H:%M:%S +00:00')


def received_now(raw):
    """A parsed driver record received now, as replay_once stamps it."""
    item = parse_fragment(raw)
    item['received_epoch'] = time.time()
    return item


def fragment(text, part, total, received, sent=None, slot=None):
    """A driver record received at `received`; the SMSC stamps each delivery attempt."""
    item = parse_fragment(record(text, part, total, scts(received - 6 if sent is None else sent), slot or part))
    item['received_epoch'] = received
    return item


async def outbox(db):
    return await db._all('SELECT event_key,backend,payload FROM outbox ORDER BY id')


def replay(db, tmp_path):
    async def call(**kwargs):
        await db.insert_call(**kwargs)
    return IngressReplay(db, CdrHandler(call, AsyncMock()), tmp_path, tmp_path/'phonelet.csv',
                         reassembly_timeout=60)


async def test_db_outage_and_python_restart_replay(db, tmp_path, monkeypatch):
    (tmp_path/'one.sms').write_bytes(record())
    consumer = replay(db, tmp_path)
    original = db.ingest_fragment
    monkeypatch.setattr(db, 'ingest_fragment', AsyncMock(side_effect=sqlite3.OperationalError('full')))
    await consumer.replay_once()
    assert consumer.errors == 1
    assert (tmp_path/'one.sms').exists()
    assert not await db.list_sms()
    monkeypatch.setattr(db, 'ingest_fragment', original)
    await consumer.replay_once()
    assert consumer.errors == 0
    restarted = replay(db, tmp_path)
    await restarted.replay_once()
    assert restarted.errors == 0  # the stored fragment is recognised, not a unique-index failure
    assert len(await db.list_sms()) == 1


async def test_partial_restart_late_part_and_no_old_sender_timeout(db, tmp_path):
    consumer = replay(db, tmp_path)
    (tmp_path/'one.sms').write_bytes(record('A', 1, 2, stamp='2020-01-01 12:00:00 +00:00'))
    await consumer.replay_once()
    assert await db.list_sms() == []  # timeout measures receipt, not SCTS
    await db.flush_fragments(60, now=time.time()+61)
    assert (await db.list_sms())[0]['partial']
    (tmp_path/'two.sms').write_bytes(record('B', 2, 2, stamp='2020-01-01 12:00:00 +00:00'))
    await replay(db, tmp_path).replay_once()
    rows = await db.list_sms()
    assert len(rows) == 1 and rows[0]['text'] == 'AB' and not rows[0]['partial']


async def test_replay_drains_old_siblings_before_partial(db, tmp_path):
    for name, part in [('a', 1), ('b', 2)]:
        path = tmp_path/f'{name}.sms'
        path.write_bytes(record(name, part, 2))
        os.utime(path, (100, 100))
    await replay(db, tmp_path).replay_once()
    assert len(await db.list_sms()) == 1
    assert not (await db.list_sms())[0]['partial']


async def test_interleaved_reference_reuse_never_splices(db):
    for body, part, stamp, slot in [('A-one ',1,'10:00:00',1), ('B-one ',1,'10:01:00',2),
                                   ('A-two',2,'10:00:00',3), ('B-two',2,'10:01:00',4)]:
        await db.ingest_fragment(received_now(record(body, part, 2, '2026-09-25 '+stamp+' +00:00', slot)))
    assert sorted(r['text'] for r in await db.list_sms()) == ['A-one A-two', 'B-one B-two']


# Journal names per record in arrival order; 'acdb' sorts as G0P1, G1P2, G1P1, G0P2.
@pytest.mark.parametrize('names', ['acdb', 'dcba'], ids=['shuffled', 'reversed'])
async def test_replay_ingests_journal_in_arrival_order_not_name_order(db, tmp_path, names):
    # The driver names records randomly (mkstemp); only mtime keeps arrival order.
    now = time.time()
    arrivals = [('G0P1', 1, 0), ('G1P1', 1, 1), ('G0P2', 2, 0), ('G1P2', 2, 1)]
    for index, ((body, part, generation), name) in enumerate(zip(arrivals, names)):
        path = tmp_path/f'.fragment-{name}.sms'
        path.write_bytes(record(body, part, 2, scts(now - 600 + 60 * generation), slot=index + 1))
        os.utime(path, (now - 30 + index, now - 30 + index))
    await replay(db, tmp_path).replay_once()
    rows = await db.list_sms()
    assert sorted(r['text'] for r in rows) == ['G0P1G0P2', 'G1P1G1P2']
    assert not any(r['partial'] for r in rows)
    assert not await db._all('SELECT id FROM assemblies WHERE ambiguous=1')


async def test_same_scts_collision_is_partial_and_slot_distinguishes_messages(db):
    for body, part, slot in [('A',1,1), ('B',1,2), ('C',2,3), ('D',2,4)]:
        await db.ingest_fragment(received_now(record(body, part, 2, slot=slot)))
    await db.flush_fragments(60, now=time.time()+61)
    assert len(await db.list_sms()) == 4
    assert all(r['partial'] for r in await db.list_sms())
    await db.ingest_fragment(received_now(record('same', slot=10)))
    await db.ingest_fragment(received_now(record('same', slot=11)))
    assert len(await db.list_sms()) == 6


@pytest.mark.parametrize('delays', [(0, 0), (2, 21), (30, 29)], ids=lambda d: 'delays-%d-%d' % d)
@pytest.mark.parametrize('order', list(permutations((1, 2, 3))), ids=lambda o: 'parts-%d%d%d' % o)
async def test_parts_with_different_scts_join_in_any_order(db, order, delays):
    db.backends = ('telegram', 'sip')
    db.backend_methods = {'sip': SipDelivery.accepted_methods}
    received = T0
    for index, part in enumerate(order):
        received += delays[index - 1] if index else 0
        # Every delivery attempt carries its own SCTS, even with zero delay.
        await db.ingest_fragment(fragment(PARTS[part], part, 3, received, sent=received - 6 + index))
        await db.flush_fragments(60, now=received)  # the replay loop flushes after every pass
    [row] = await db.list_sms()
    assert row['text'] == FULL and not row['partial']
    assert sorted((j['event_key'], j['backend']) for j in await outbox(db)) == [
        (f"sms:{row['id']}", 'sip'), (f"sms:{row['id']}", 'telegram')]
    await db.flush_fragments(60, now=received + 3600)
    assert await db.list_sms() == [row]


@pytest.mark.parametrize('early, late', [((1, 2), (3,)), ((3, 1), (2,)), ((3,), (1, 2))],
                         ids=['part-3-late', 'part-3-first', 'only-part-3-on-time'])
async def test_late_part_with_new_scts_completes_the_published_partial(db, early, late):
    db.backends = ('telegram', 'sip')
    db.backend_methods = {'sip': SipDelivery.accepted_methods}
    for index, part in enumerate(early):
        await db.ingest_fragment(fragment(PARTS[part], part, 3, T0 + 6 * index))
    await db.flush_fragments(60, now=T0 + 59)
    assert await db.list_sms() == []
    await db.flush_fragments(60, now=T0 + 61)
    [first] = await db.list_sms()
    assert first['partial'] and '[missing part' in first['text']
    for index, part in enumerate(late):
        received = T0 + 1200 + 6 * index  # the SMSC retried for twenty minutes
        await db.ingest_fragment(fragment(PARTS[part], part, 3, received))
        await db.flush_fragments(60, now=received)
    [row] = await db.list_sms()
    assert row['id'] == first['id'] and row['text'] == FULL and not row['partial']
    jobs = await outbox(db)
    assert sorted(j['backend'] for j in jobs if j['event_key'] == f"sms:{row['id']}") == ['sip', 'telegram']
    # Each late part republishes once per channel; the last update is complete.
    updates = [j for j in jobs if j['event_key'].startswith('assembly-update:')]
    assert len(updates) == 2 * len(late)
    final = [j for j in updates if j['event_key'].endswith(':3:0')]
    assert sorted(j['backend'] for j in final) == ['sip', 'telegram']
    assert all(json.loads(j['payload'])['text'] == FULL for j in final)


@pytest.mark.parametrize('early, late', [((1, 2), 3), ((3, 1), 2)], ids=['part-3-late', 'part-3-first'])
async def test_late_part_after_restart_replays_spool_without_duplicates(tmp_path, early, late):
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    now = time.time()

    def receive(part, received):
        item = spool/f'{int(received)}-{part}.sms'
        item.write_bytes(record(PARTS[part], part, 3, scts(received - 6), slot=part))
        os.utime(item, (received, received))

    async def restart_and_replay():
        db = Database(path, ('telegram', 'sip'), backend_methods={'sip': SipDelivery.accepted_methods})
        await db.init()
        try:
            for _ in range(2):  # a new consumer rereads the whole spool
                await replay(db, spool).replay_once()
            return await db.list_sms(), await outbox(db)
        finally:
            await db.close()

    for index, part in enumerate(early):
        receive(part, now - 1200 + 6 * index)
    [first], _ = await restart_and_replay()
    assert first['partial'] and '[missing part' in first['text']
    receive(late, now)
    [row], jobs = await restart_and_replay()
    assert row['id'] == first['id'] and row['text'] == FULL and not row['partial']
    assert sorted((j['event_key'].split(':')[0], j['backend']) for j in jobs) == [
        ('assembly-update', 'sip'), ('assembly-update', 'telegram'), ('sms', 'sip'), ('sms', 'telegram')]
    assert all(json.loads(j['payload'])['text'] == FULL and j['event_key'].endswith(':3:0')
               for j in jobs if j['event_key'].startswith('assembly-update:'))
    assert await restart_and_replay() == ([row], jobs)


@pytest.mark.parametrize('hours', [23, 25])
async def test_late_part_joins_within_a_day_only(db, hours):
    db.backends = ('telegram',)
    await db.ingest_fragment(fragment('first ', 1, 2, T0))
    await db.flush_fragments(60, now=T0 + 61)
    [first] = await db.list_sms()
    late = T0 + hours * 3600
    await db.ingest_fragment(fragment('second', 2, 2, late))
    await db.flush_fragments(60, now=late + 61)
    rows = await db.list_sms()
    if hours == 23:
        assert len(rows) == 1 and rows[0]['id'] == first['id']
        assert rows[0]['text'] == 'first second' and not rows[0]['partial']
    else:
        assert sorted(r['text'] for r in rows) == ['[missing part 1/2]second', 'first [missing part 2/2]']
        assert await db._one('SELECT * FROM sms WHERE id=?', (first['id'],)) == first
        assert not [j for j in await outbox(db) if j['event_key'].startswith('assembly-update:')]


async def test_complete_assembly_is_closed_to_a_reused_reference(db):
    db.backends = ('telegram',)
    await db.ingest_fragment(fragment('old-one ', 1, 2, T0))
    await db.ingest_fragment(fragment('old-two', 2, 2, T0 + 5))
    [old] = await db.list_sms()
    # Ten minutes later the same sender reuses the reference and total.
    await db.ingest_fragment(fragment('new-two', 2, 2, T0 + 600))
    await db.flush_fragments(60, now=T0 + 600)
    await db.ingest_fragment(fragment('new-one ', 1, 2, T0 + 605))
    await db.flush_fragments(60, now=T0 + 7200)
    rows = await db.list_sms()
    assert sorted(r['text'] for r in rows) == ['new-one new-two', 'old-one old-two']
    assert not any(r['partial'] for r in rows)
    assert await db._one('SELECT * FROM sms WHERE id=?', (old['id'],)) == old
    assert sorted(j['event_key'] for j in await outbox(db)) == sorted(f"sms:{r['id']}" for r in rows)


async def test_late_part_fitting_two_open_assemblies_is_marked_not_joined(db):
    db.backends = ('telegram',)
    stale = fragment('S-one ', 1, 2, T0 - 30 * 3600)
    closed = [fragment('C-one ', 1, 2, T0 - 3600), fragment('C-two', 2, 2, T0 - 3595)]
    await db.ingest_fragment(stale)
    await db.flush_fragments(60, now=T0 - 30 * 3600 + 61)
    for item in closed:
        await db.ingest_fragment(item)
    untouched = await db._all('SELECT * FROM sms ORDER BY id')
    x = fragment('X-one ', 1, 2, T0)
    await db.ingest_fragment(x)
    await db.flush_fragments(60, now=T0 + 61)  # X is published as a partial
    y = fragment('Y-one ', 1, 2, T0 + 300)  # part 1 is taken in X: a new generation
    await db.ingest_fragment(y)
    late = fragment('late', 2, 2, T0 + 330)  # fits X and Y, its SCTS matches neither
    await db.ingest_fragment(late)
    await db.flush_fragments(60, now=T0 + 400)
    rows = await db._all('SELECT * FROM sms ORDER BY id')
    assert rows[:2] == untouched
    marker = '[ambiguous multipart reference] '
    assert rows[2]['text'] == marker + 'X-one [missing part 2/2]'
    assert sorted(r['text'] for r in rows[3:]) == [marker + 'Y-one [missing part 2/2]',
                                                   marker + '[missing part 1/2]late']
    assert all(r['partial'] for r in rows[2:])
    flags = {r['id']: r['ambiguous'] for r in await db._all('SELECT id,ambiguous FROM assemblies')}
    assert [flags[f['source_id']] for f in (stale, closed[0], x, y, late)] == [0, 0, 1, 1, 1]
    updates = [j for j in await outbox(db) if j['event_key'].startswith('assembly-update:')]
    assert [j['event_key'] for j in updates] == [f"assembly-update:{x['source_id']}:1:1"]
    assert json.loads(updates[0]['payload'])['text'] == rows[2]['text']


async def test_failed_late_part_defers_its_group_whatever_its_scts(db, tmp_path, monkeypatch):
    db.backends = ('telegram',)
    now = time.time()
    first = tmp_path/'a1.sms'
    first.write_bytes(record('A-one ', 1, 2, stamp=scts(now - 126)))
    os.utime(first, (now - 120, now - 120))
    (tmp_path/'a2.sms').write_bytes(record('A-two', 2, 2, stamp=scts(now - 6), slot=2))
    original = db.ingest_fragment

    async def fail_late(item, **kwargs):
        if item['text'] == 'A-two':
            raise sqlite3.OperationalError('temporary commit failure')
        await original(item, **kwargs)
    monkeypatch.setattr(db, 'ingest_fragment', fail_late)
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 1 and await db.list_sms() == []
    assert await db._all('SELECT part,text FROM fragments') == [{'part': 1, 'text': 'A-one '}]
    monkeypatch.setattr(db, 'ingest_fragment', original)
    await consumer.replay_once()
    [row] = await db.list_sms()
    assert row['text'] == 'A-one A-two' and not row['partial']
    assert [j['event_key'] for j in await outbox(db)] == [f"sms:{row['id']}"]


def fail_fragment_insert_once(db, monkeypatch, text):
    """Fail inside the ingest transaction, after its assembly row was written."""
    execute, pending = db._conn.execute, [text]

    def failing(sql, *args):
        if pending and sql.startswith('INSERT INTO fragments') and args[0][3] == pending[0]:
            pending.clear()
            raise sqlite3.OperationalError('disk I/O error')
        return execute(sql, *args)
    monkeypatch.setattr(db._conn, 'execute', failing)


@pytest.mark.parametrize('restart', [False, True], ids=['next-pass', 'after-restart'])
async def test_failed_commit_defers_later_parts_of_its_group(tmp_path, monkeypatch, restart):
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    now = time.time()
    # A is complete before B starts: same sender, reference and total.
    for index, (body, part) in enumerate([('A-one ', 1), ('A-two', 2), ('B-one ', 1), ('B-two', 2)]):
        item = spool/f'.fragment-{"dcba"[index]}.sms'
        item.write_bytes(record(body, part, 2, scts(now - 40 + index), slot=index + 1))
        os.utime(item, (now - 30 + index, now - 30 + index))
    db = Database(path, ('telegram', 'sip'), backend_methods={'sip': SipDelivery.accepted_methods})
    await db.init()
    try:
        fail_fragment_insert_once(db, monkeypatch, 'A-one ')
        consumer = replay(db, spool)
        await consumer.replay_once()
        assert consumer.errors == 1
        # A1 rolled back; its later siblings wait for it instead of taking its place.
        assert await db._all('SELECT id FROM assemblies') == [] and await db.list_sms() == []
        if restart:
            await db.close()
            db = Database(path, ('telegram', 'sip'), backend_methods={'sip': SipDelivery.accepted_methods})
            await db.init()
            consumer = replay(db, spool)
        await consumer.replay_once()
        assert consumer.errors == 0
        rows = await db.list_sms()
        assert sorted(r['text'] for r in rows) == ['A-one A-two', 'B-one B-two']
        assert not any(r['partial'] for r in rows)
        assert (await db._one('SELECT COUNT(*) AS n FROM fragments'))['n'] == 4
        assert not await db._all('SELECT id FROM assemblies WHERE ambiguous=1')
        assert sorted((j['event_key'], j['backend']) for j in await outbox(db)) == sorted(
            (f"sms:{r['id']}", backend) for r in rows for backend in ('sip', 'telegram'))
    finally:
        await db.close()


async def test_record_with_invalid_numbering_blocks_nothing_in_its_group(db, tmp_path):
    db.backends = ('telegram',)
    now = time.time()
    for name, raw, received in [('a', record('A-one ', 1, 2, scts(now - 126)), now - 120),
                                ('b', record('bad', 3, 2, scts(now - 125), slot=9), now - 119)]:
        (tmp_path/f'{name}.sms').write_bytes(raw)
        os.utime(tmp_path/f'{name}.sms', (received, received))
    with pytest.raises(ValueError, match='numbering'):
        parse_fragment(record('bad', 3, 2))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 0  # set aside with an alert instead of a retry that holds /ready
    assert await invalid_record_alerts(db) == [sms_invalid_key(tmp_path/'b.sms')]
    [first] = await db.list_sms()
    assert first['text'] == 'A-one [missing part 2/2]'  # its group's partial is not deferred
    (tmp_path/'c.sms').write_bytes(record('A-two', 2, 2, scts(now - 6), slot=2))
    await consumer.replay_once()
    assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
    [row] = await db.list_sms()
    assert row['id'] == first['id'] and row['text'] == 'A-one A-two' and not row['partial']
    assert (tmp_path/'b.sms').exists()


def sms_invalid_key(path):
    return 'sms-journal-invalid:' + hashlib.sha256(path.read_bytes()).hexdigest()


async def invalid_record_alerts(db):
    return [job['event_key'] for job in await outbox(db) if job['event_key'].startswith(('sms-journal-invalid:',
                                                                                       'cdr-invalid:'))]


def journal_ab(spool, now):
    """Arrival order A1, A2, B1, B2: A is complete before B starts, same group key, distinct SCTS."""
    paths = {}
    for index, (body, part) in enumerate([('A-one ', 1), ('A-two', 2), ('B-one ', 1), ('B-two', 2)]):
        path = spool/f'.fragment-{"dcba"[index]}.sms'
        path.write_bytes(record(body, part, 2, scts(now - 40 + index), slot=index + 1))
        os.utime(path, (now - 30 + index, now - 30 + index))
        paths[body.strip()] = path
    return paths


def make_unreadable(monkeypatch, path, method):
    """A transient OS error on one journal file; clear the returned set to repair it."""
    broken, original = {path}, getattr(Path, method)

    def failing(self, *args, **kwargs):
        if self in broken:
            raise PermissionError(13, 'Permission denied', str(self))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, method, failing)
    return broken


def fake_clocks(monkeypatch, day='2026-10-23'):
    """Monotonic seconds and the UTC day used by the ingress alert, both settable."""
    import phonelet.ingress as module
    clock, today = [1000.], [day]

    class Today(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(today[0] + 'T12:00:00+00:00').astimezone(tz)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module, 'datetime', Today)
    return clock, today


async def stored_parts(db):
    return sorted(r['text'] for r in await db._all('SELECT text FROM fragments'))


async def block_alerts(db):
    return [j for j in await db._all('SELECT * FROM outbox ORDER BY id')
            if j['event_key'].startswith('sms-journal-blocked:')]


# A read error holds that file and every later one; a stat error while listing holds the whole pass.
@pytest.mark.parametrize('method,broken,accepted', [
    ('read_bytes', 'A-one', []), ('read_bytes', 'B-one', ['A-one ', 'A-two']),
    ('read_bytes', 'B-two', ['A-one ', 'A-two', 'B-one ']),
    ('stat', 'A-one', []), ('stat', 'B-two', []),
], ids=['read-first', 'read-middle', 'read-last', 'stat-first', 'stat-last'])
@pytest.mark.parametrize('restart', [False, True], ids=['same-process', 'restart'])
async def test_unreadable_journal_file_holds_later_parts(tmp_path, monkeypatch, method, broken, accepted,
                                                         restart):
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    paths = journal_ab(spool, time.time())
    (spool/'phonelet.csv').write_text(cdr_line('held-sms'))
    db = Database(path, ('telegram', 'sip'), backend_methods={'sip': SipDelivery.accepted_methods})
    await db.init()
    try:
        repair = make_unreadable(monkeypatch, paths[broken], method)
        consumer = replay(db, spool)
        for _ in range(2):
            await consumer.replay_once()
            assert consumer.errors == 1
            # A later part never takes the place of the unknown one.
            assert await stored_parts(db) == accepted
            assert [r['text'] for r in await db.list_sms()] == (['A-one A-two'] if accepted else [])
            assert len(await db.list_calls()) == 1  # CDR is not held
            if restart:
                await db.close()
                db = Database(path, ('telegram', 'sip'), backend_methods={'sip': SipDelivery.accepted_methods})
                await db.init()
                consumer = replay(db, spool)
        repair.clear()
        await consumer.replay_once()
        assert consumer.errors == 0
        rows = await db.list_sms()
        assert sorted(r['text'] for r in rows) == ['A-one A-two', 'B-one B-two']
        assert not any(r['partial'] for r in rows)
        assert len(await stored_parts(db)) == 4
        assert not await db._all('SELECT id FROM assemblies WHERE ambiguous=1')
        assert sorted((j['event_key'], j['backend']) for j in await outbox(db)) == sorted(
            [(f"sms:{r['id']}", backend) for r in rows for backend in ('sip', 'telegram')]
            + [(f"calls:{c['id']}", 'telegram') for c in await db.list_calls()])
        assert len(await db.list_calls()) == 1
    finally:
        await db.close()


@pytest.mark.parametrize('method', ['read_bytes', 'stat'])
async def test_unreadable_journal_file_does_not_hold_timeout_publication(db, tmp_path, monkeypatch, method):
    db.backends = ('telegram',)
    now = time.time()
    first, late = tmp_path/'a.sms', tmp_path/'b.sms'
    first.write_bytes(record('A-one ', 1, 2, scts(now - 36)))
    os.utime(first, (now - 30, now - 30))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert await db.list_sms() == []  # accepted, not yet due
    late.write_bytes(record('A-two', 2, 2, scts(now - 6), slot=2))
    repair = make_unreadable(monkeypatch, late, method)
    consumer.reassembly_timeout = 10  # the accepted part is now due
    await consumer.replay_once()
    assert consumer.errors == 1
    [partial] = await db.list_sms()  # published on timeout while the late file holds reception
    assert partial['text'] == 'A-one [missing part 2/2]' and partial['partial']
    repair.clear()
    await consumer.replay_once()
    assert consumer.errors == 0
    [row] = await db.list_sms()
    assert row['id'] == partial['id'] and row['text'] == 'A-one A-two' and not row['partial']
    jobs = await outbox(db)
    assembly = parse_fragment(first.read_bytes())['source_id']
    assert [j['event_key'] for j in jobs] == [f"sms:{row['id']}", f"assembly-update:{assembly}:2:0"]
    assert json.loads(jobs[1]['payload'])['text'] == 'A-one A-two'


async def test_corrupt_journal_file_does_not_hold_later_parts(db, tmp_path):
    db.backends = ('telegram',)
    now = time.time()
    poison = tmp_path/'.fragment-z.sms'
    poison.write_bytes(b'1\tcorrupt\n')
    os.utime(poison, (now - 60, now - 60))  # first in arrival order
    journal_ab(tmp_path, now)
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
    rows = await db.list_sms()
    assert sorted(r['text'] for r in rows) == ['A-one A-two', 'B-one B-two']
    assert consumer._sms_block is None
    assert await invalid_record_alerts(db) == [sms_invalid_key(poison)]


BLOCK_ALERT = 'SMS reception blocked: a driver journal file has been unreadable for 15 min'


async def test_unreadable_journal_file_alerts_once_after_900_s(tmp_path, monkeypatch, caplog):
    clock, _ = fake_clocks(monkeypatch)
    caplog.set_level(logging.INFO, logger='phonelet.ingress')
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    backends = ('telegram', 'homeassistant', 'sip')
    paths = journal_ab(spool, time.time())
    repair = make_unreadable(monkeypatch, paths['A-one'], 'read_bytes')
    db = Database(path, backends, backend_methods={'sip': SipDelivery.accepted_methods})
    await db.init()
    try:
        consumer = replay(db, spool)
        await consumer.replay_once()
        clock[0] += 899
        await consumer.replay_once()
        assert await block_alerts(db) == []  # not before 900 s
        clock[0] += 1
        await consumer.replay_once()
        queued = await block_alerts(db)
        # The durable alert path: Telegram and Home Assistant, never SIP.
        assert sorted(j['backend'] for j in queued) == ['homeassistant', 'telegram']
        assert {j['event_key'] for j in queued} == {f"sms-journal-blocked:{paths['A-one'].name}:2026-10-23"}
        assert {j['method'] for j in queued} == {'notify_health'}
        assert {j['payload'] for j in queued} == {json.dumps({'message': BLOCK_ALERT})}
        for _ in range(3):
            clock[0] += 600
            await consumer.replay_once()
        assert await block_alerts(db) == queued

        def held(level):
            return [r.getMessage() for r in caplog.records
                    if r.levelno == level and 'held by unreadable journal' in r.getMessage()]
        assert len(held(logging.WARNING)) == 1 and held(logging.INFO) == []
        # A restart the same day restarts the 900 s count and queues nothing new.
        await db.close()
        db = Database(path, backends, backend_methods={'sip': SipDelivery.accepted_methods})
        await db.init()
        consumer = replay(db, spool)
        for _ in range(3):
            await consumer.replay_once()
            clock[0] += 600
        assert consumer._sms_block.alerted_day == '2026-10-23'
        assert await block_alerts(db) == queued
        # The alert was already queued: no second WARNING after the restart, only INFO.
        assert len(held(logging.WARNING)) == 1
        assert [m.endswith('alert already queued') for m in held(logging.INFO)] == [True]
        repair.clear()
        await consumer.replay_once()
        assert consumer.errors == 0 and consumer._sms_block is None
        assert sorted(r['text'] for r in await db.list_sms()) == ['A-one A-two', 'B-one B-two']
        # Recovery is a log line, not a notification.
        assert await block_alerts(db) == queued
        assert any(r.levelno == logging.INFO and 'resumed' in r.getMessage() for r in caplog.records)
        for record_ in caplog.records:
            assert '+7' not in record_.getMessage() and 'A-one' not in record_.getMessage()
        assert '+7' not in BLOCK_ALERT
    finally:
        await db.close()


async def test_block_alert_once_per_blocker_and_utc_day(tmp_path, monkeypatch):
    clock, today = fake_clocks(monkeypatch, '2026-10-23')
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    now = time.time()
    paths = journal_ab(spool, now)
    oldest = paths['A-one'].name
    broken = make_unreadable(monkeypatch, paths['A-one'], 'read_bytes')
    broken.update(paths.values())  # e.g. the whole spool lost its group

    async def incident(db, consumer):
        for _ in range(3):
            await consumer.replay_once()
            clock[0] += 600
        assert consumer.errors == 1
        broken.clear()
        await consumer.replay_once()
        assert consumer.errors == 0

    def keys(alerts):
        return sorted({j['event_key'] for j in alerts})
    db = Database(path, ('telegram',))
    await db.init()
    await incident(db, replay(db, spool))
    assert keys(await block_alerts(db)) == [f'sms-journal-blocked:{oldest}:2026-10-23']
    await db.close()
    # Same day, a new SMS, a restart and the same failure: the oldest file holds again, no new alert.
    new = spool/'.fragment-new.sms'
    new.write_bytes(record('C', 1, 1, scts(now + 5), slot=9))
    os.utime(new, (now + 10, now + 10))
    broken.update([*paths.values(), new])
    db = Database(path, ('telegram',))
    await db.init()
    try:
        await incident(db, replay(db, spool))
        assert keys(await block_alerts(db)) == [f'sms-journal-blocked:{oldest}:2026-10-23']
        # Another UTC day, another restart: the second incident alerts.
        await db.close()
        today[0] = '2026-10-24'
        broken.update([*paths.values(), new])
        db = Database(path, ('telegram',))
        await db.init()
        await incident(db, replay(db, spool))
        assert keys(await block_alerts(db)) == [f'sms-journal-blocked:{oldest}:2026-10-23',
                                                f'sms-journal-blocked:{oldest}:2026-10-24']
        assert sorted(r['text'] for r in await db.list_sms()) == ['A-one A-two', 'B-one B-two', 'C']
    finally:
        await db.close()


async def test_block_lasting_into_another_utc_day_alerts_again(db, tmp_path, monkeypatch):
    clock, today = fake_clocks(monkeypatch, '2026-10-23')
    db.backends = ('telegram',)
    paths = journal_ab(tmp_path, time.time())
    make_unreadable(monkeypatch, paths['A-one'], 'read_bytes')
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    clock[0] += 900
    await consumer.replay_once()
    clock[0] += 3600
    await consumer.replay_once()
    assert len(await block_alerts(db)) == 1
    today[0] = '2026-10-24'
    clock[0] += 5
    await consumer.replay_once()
    await consumer.replay_once()
    assert [j['event_key'] for j in await block_alerts(db)] == [
        f"sms-journal-blocked:{paths['A-one'].name}:{day}" for day in ('2026-10-23', '2026-10-24')]


async def test_blocker_change_restarts_the_900_s_count(db, tmp_path, monkeypatch):
    clock, _ = fake_clocks(monkeypatch)
    db.backends = ('telegram',)
    paths = journal_ab(tmp_path, time.time())
    broken = make_unreadable(monkeypatch, paths['A-two'], 'read_bytes')
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    clock[0] += 899
    broken.clear()
    broken.add(paths['B-two'])  # A recovers, B now holds reception
    await consumer.replay_once()
    assert consumer._sms_block.path == paths['B-two']
    clock[0] += 1
    await consumer.replay_once()
    assert await block_alerts(db) == []  # 900 s since A started, 1 s of B's own
    clock[0] += 898
    await consumer.replay_once()
    assert await block_alerts(db) == []
    clock[0] += 1
    await consumer.replay_once()
    [alert] = await block_alerts(db)
    assert alert['event_key'] == f"sms-journal-blocked:{paths['B-two'].name}:2026-10-23"


async def test_unreadable_spool_directory_holds_sms_and_alerts(db, tmp_path, monkeypatch):
    import errno
    import phonelet.ingress as module
    clock, _ = fake_clocks(monkeypatch)
    db.backends = ('telegram', 'sip')
    db.backend_methods = {'sip': SipDelivery.accepted_methods}
    spool = tmp_path/'spool'
    spool.mkdir()
    (spool/'one.sms').write_bytes(record())
    (spool/'phonelet.csv').write_text(cdr_line('listing-failed'))
    listdir, failing = os.listdir, [True]

    def unlistable(target='.'):
        if failing and Path(target) == spool:
            raise OSError(errno.EIO, 'Input/output error')
        return listdir(target)
    monkeypatch.setattr(module.os, 'listdir', unlistable)
    consumer = replay(db, spool)
    await consumer.replay_once()
    assert consumer.errors == 1 and consumer.status['ingress_errors'] == 1  # /ready degraded
    assert await stored_parts(db) == [] and await db.list_sms() == []
    assert len(await db.list_calls()) == 1  # CDR is not held
    clock[0] += 899
    await consumer.replay_once()
    assert await block_alerts(db) == []
    clock[0] += 1
    await consumer.replay_once()
    [alert] = await block_alerts(db)
    assert alert['backend'] == 'telegram'
    assert alert['event_key'] == 'sms-journal-blocked:spool:2026-10-23'
    assert json.loads(alert['payload']) == {
        'message': 'SMS reception blocked: a driver journal directory has been unreadable for 15 min'}
    failing.clear()
    await consumer.replay_once()
    assert consumer.errors == 0 and [r['text'] for r in await db.list_sms()] == ['hello']


async def test_one_traceback_per_blocker_not_per_pass(db, tmp_path, monkeypatch, caplog):
    fake_clocks(monkeypatch)
    caplog.set_level(logging.DEBUG, logger='phonelet.ingress')
    paths = journal_ab(tmp_path, time.time())
    broken = make_unreadable(monkeypatch, paths['A-two'], 'read_bytes')
    consumer = replay(db, tmp_path)

    def tracebacks():
        # With the privacy factory installed the exception is folded into the message.
        return [r for r in caplog.records if r.name == 'phonelet.ingress' and r.levelno == logging.ERROR]
    for _ in range(5):
        await consumer.replay_once()
        assert consumer.errors == 1
    [first] = tracebacks()
    assert first.getMessage().startswith(f"SMS journal file {paths['A-two'].name} unreadable; ")
    assert (first.exc_info or (None,))[0] is PermissionError or 'PermissionError at' in first.getMessage()
    broken.clear()
    broken.add(paths['B-two'])
    for _ in range(5):
        await consumer.replay_once()
    assert [r.getMessage().startswith(f"SMS journal file {paths['B-two'].name} unreadable; SMS parts wait")
            for r in tracebacks()] == [False, True]
    held = [r for r in caplog.records if 'still held' in r.getMessage()]
    assert len(held) == 8 and {r.levelno for r in held} == {logging.DEBUG}


async def test_failed_alert_enqueue_does_not_break_the_pass_and_retries(db, tmp_path, monkeypatch):
    clock, _ = fake_clocks(monkeypatch)
    db.backends = ('telegram',)
    paths = journal_ab(tmp_path, time.time())
    (tmp_path/'phonelet.csv').write_text(cdr_line('during-alert-failure'))
    make_unreadable(monkeypatch, paths['A-two'], 'read_bytes')
    original = db.enqueue_notification
    monkeypatch.setattr(db, 'enqueue_notification', AsyncMock(side_effect=sqlite3.OperationalError('locked')))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    clock[0] += 900
    await consumer.replay_once()
    assert consumer.errors == 1 and len(await db.list_calls()) == 1  # the pass went on
    assert consumer._sms_block.alerted_day is None
    monkeypatch.setattr(db, 'enqueue_notification', original)
    await consumer.replay_once()
    assert [(j['backend'], json.loads(j['payload'])) for j in await block_alerts(db)] == [
        ('telegram', {'message': BLOCK_ALERT})]


@pytest.mark.parametrize('userfield', [';phonelet-call-rejected',
                                     '/var/lib/phonelet/recordings/test.mp3;phonelet-call-rejected'])
async def test_rejected_cdr_replay_and_restart(db, tmp_path, userfield):
    db.backends = ('telegram', 'homeassistant', 'other')
    (tmp_path/'phonelet.csv').write_text(
        cdr_line('rejected', source='+15551234567', recording=userfield).replace(',ANSWERED,4,', ',NO ANSWER,0,'))
    for _ in range(2):
        consumer = replay(db, tmp_path)
        await consumer.replay_once()
        assert consumer.errors == 0
        calls = await db.list_calls()
        assert len(calls) == 1
        assert calls[0]['source_id'] == 'cdr:rejected'
        assert calls[0]['status'] == 'rejected'
        assert calls[0]['recording_path'] is None
        assert await outbox(db) == []


@pytest.mark.parametrize('disposition', ['NO ANSWER', 'FAILED'])
async def test_call_to_offline_phone_is_one_missed_call_with_notification(db, tmp_path, disposition):
    # The phone has no reachable contact: Dial gets no callee, Hangup() overwrites lastapp,
    # and the call's only row has lastapp Hangup.
    db.backends = ('telegram',)
    (tmp_path/'phonelet.csv').write_text(
        cdr_line('offline', source='+15551234567', recording='/var/lib/phonelet/recordings/x.mp3')
        .replace(',Dial,', ',Hangup,').replace(',ANSWERED,4,', f',{disposition},0,'))
    for _ in range(2):
        consumer = replay(db, tmp_path)
        await consumer.replay_once()
        assert consumer.errors == 0
        calls = await db.list_calls()
        assert [(c['source_id'], c['status'], c['recording_path']) for c in calls] == [
            ('cdr:offline', 'missed', None)]
        assert [(j['backend'], json.loads(j['payload'])['status']) for j in await outbox(db)] == [
            ('telegram', 'missed')]


@pytest.mark.parametrize('disposition,userfield,status,jobs', [
    ('ANSWERED,4', '/var/lib/phonelet/recordings/x.mp3', 'answered', 1),
    ('NO ANSWER,0', '/var/lib/phonelet/recordings/x.mp3', 'missed', 1),
    ('NO ANSWER,0', '/var/lib/phonelet/recordings/x.mp3;phonelet-call-rejected', 'rejected', 0),
])
async def test_dial_row_then_hangup_row_is_one_call_with_the_dial_status(db, tmp_path, disposition, userfield,
                                                                         status, jobs):
    db.backends = ('telegram',)
    dial = cdr_line('both', recording=userfield).replace(',ANSWERED,4,', f',{disposition},')
    hangup = (cdr_line('both', recording='/var/lib/phonelet/recordings/x.mp3')
              .replace(',Dial,', ',Hangup,').replace(',ANSWERED,4,', ',NO ANSWER,0,'))
    (tmp_path/'phonelet.csv').write_text(dial + hangup)
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 0
    calls = await db.list_calls()
    assert [(c['source_id'], c['status']) for c in calls] == [('cdr:both', status)]
    assert calls[0]['recording_path'] == ('/var/lib/phonelet/recordings/x.mp3' if status == 'answered' else None)
    assert [json.loads(j['payload'])['status'] for j in await outbox(db)] == [status] * jobs


async def test_cdr_replay_incremental_rotation_partial_and_restart(db, tmp_path):
    path = tmp_path/'phonelet.csv'
    path.write_text(cdr_line('1'))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    offsets = dict(consumer._cdr_offsets)
    await consumer.replay_once()
    assert consumer._cdr_offsets == offsets
    with path.open('a') as f:
        f.write(cdr_line('2', source='quoted,\ncaller')[:-3])
    await consumer.replay_once()
    assert len(await db.list_calls()) == 1
    with path.open('a') as f:
        f.write(cdr_line('2', source='quoted,\ncaller')[-3:])
    await consumer.replay_once()
    path.rename(tmp_path/'phonelet.csv.1')
    path.write_text(cdr_line('3'))
    await consumer.replay_once()
    assert consumer.errors == 0
    restarted = replay(db, tmp_path)
    await restarted.replay_once()
    assert restarted.errors == 0  # replayed calls are recognised by source id, not retried
    assert len(await db.list_calls()) == 3
    assert (await db.get_call(1))['timestamp'] == '2026-09-25T12:00:00+00:00'
    assert (await db.get_call(1))['received_at'] != (await db.get_call(1))['timestamp']


@pytest.mark.parametrize('schema', [
    # Previous schemas are refused; the board recreates its database explicitly.
    'PRAGMA user_version=4; CREATE TABLE history_alerts(event_key TEXT PRIMARY KEY);',
    'PRAGMA user_version=3; CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);',
    'PRAGMA user_version=2; CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);',
    # Schema 1 had global notified flags.
    "CREATE TABLE sms(id INTEGER PRIMARY KEY, notified INTEGER); PRAGMA user_version=1",
    # the old service's unversioned database
    "CREATE TABLE calls(id INTEGER PRIMARY KEY, caller TEXT, notified INTEGER)",
    "PRAGMA user_version=99",
])
async def test_database_of_another_schema_stops_start_up_untouched(tmp_path, schema):
    path = tmp_path/'other.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(schema)
    before = path.read_bytes()
    db = Database(str(path))
    with pytest.raises(RuntimeError, match='move it aside and start on an empty database'):
        await db.init()
    assert db._conn is None
    assert path.read_bytes() == before  # no silent migration


@pytest.mark.history_rebuild
async def test_new_database_rebuilds_old_journal_and_cdr_without_notifications_or_commands(tmp_path):
    # The first inventory is history even with timestamps later than the database.
    day_ago = time.time() + 3600
    old_stamp = datetime.fromtimestamp(day_ago, timezone.utc).isoformat()
    command = envelope.encode(bytes(32), 1, b'', int(day_ago * 1000))
    for name, raw in (('old.sms', record('old text')), ('command.sms', record(command, slot=2)),
                      ('unparsed.sms', unparsed_record()), ('invalid.sms', b'garbage\n'),
                      ('first-part.sms', record('first ', 1, 2, ref=77, slot=5))):
        (tmp_path/name).write_bytes(raw)
        os.utime(tmp_path/name, (day_ago, day_ago))
    cdr = tmp_path/'phonelet.csv'
    cdr.write_text(cdr_line('old-call', old_stamp) + 'one,invalid,line\n')
    os.utime(cdr, (day_ago, day_ago))
    db = Database(str(tmp_path/'new.db'), backends=('telegram', 'sip'),
                  backend_methods={'sip': SipDelivery.accepted_methods}, sms_commands_enabled=True)
    await db.init()
    try:
        replay = IngressReplay(db, CdrHandler(db.insert_call, AsyncMock()), tmp_path, cdr)
        await replay.replay_once()
        assert replay.errors == 0
        await db.flush_fragments(60, now=day_ago + 61)  # history survives publication after completion
        assert sorted(row['text'] for row in await db.list_sms()) == [
            '[command]', 'first [missing part 2/2]', 'old text']
        assert [row['source_id'] for row in await db.list_calls()] == ['cdr:old-call']
        assert await db.outbox_stats() == []
        assert await db._all('SELECT * FROM sms_commands') == []
        # New records after the first inventory notify with any clock. A late part of an old
        # message completes it in history without an update notification.
        (tmp_path/'new.sms').write_bytes(record('new text', slot=4))
        (tmp_path/'second-part.sms').write_bytes(record('second', 2, 2, ref=77, slot=6))
        for name in ('new.sms', 'second-part.sms'):
            os.utime(tmp_path/name, (day_ago - 7200, day_ago - 7200))
        with cdr.open('a') as stream:
            stream.write(cdr_line('new-call', '2000-01-01T00:00:00+00:00'))
        await replay.replay_once()
        assert replay.errors == 0
        jobs = await db._all('SELECT event_key, backend FROM outbox ORDER BY id')
        assert [(j['event_key'].split(':')[0], j['backend']) for j in jobs] == [
            ('sms', 'telegram'), ('sms', 'sip'), ('calls', 'telegram')]
    finally:
        await db.close()
    # The completion marker and suppressed alerts survive replay after a restart.
    db = Database(str(tmp_path/'new.db'), backends=('telegram',))
    await db.init()
    try:
        await IngressReplay(db, CdrHandler(db.insert_call, AsyncMock()), tmp_path, cdr).replay_once()
        assert await db._all('SELECT event_key, backend FROM outbox ORDER BY id') == jobs
    finally:
        await db.close()


@pytest.mark.history_rebuild
async def test_only_replay_explicitly_suppresses_history_alerts(tmp_path):
    db = Database(str(tmp_path/'new.db'), backends=('telegram',))
    await db.init()
    try:
        assert await db.enqueue_notification('notify_sms_failed', {'recipient': '+7', 'reason': 'i'},
                                             event_key='sms-report:old', history=True)
        assert await db.outbox_stats() == []
        await db.enqueue_notification('notify_sms_failed', {'recipient': '+7', 'reason': 'i'},
                                      event_key='sms-report:new')
        await db.enqueue_notification('notify_sms_failed', {'recipient': '+7', 'reason': 'i'},
                                      event_key='sms-report:unknown-age')
        assert sum(row['count'] for row in await db.outbox_stats()) == 2
    finally:
        await db.close()


async def test_crashed_csv_append_becomes_visible_and_recovers(db,tmp_path,monkeypatch):
    import phonelet.ingress as module
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    path = tmp_path/'phonelet.csv'
    path.write_text(cdr_line('1') + '"unfinished\n')
    consumer = replay(db,tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 0
    assert len(await db.list_calls()) == 1
    clock[0] += 11
    await consumer.replay_once()
    assert consumer.errors > 0
    # Operator repairs retained tail in place (same inode/committed prefix).
    path.write_text(cdr_line('1') + cdr_line('2'))
    await consumer.replay_once()
    assert consumer.errors == 0
    assert len(await db.list_calls()) == 2


@pytest.mark.parametrize('corrupt_source',['cdr','sms'])
async def test_unrelated_corrupt_journal_does_not_block_expired_partial(db,tmp_path,corrupt_source):
    first = tmp_path/'first.sms'
    first.write_bytes(record('first ',1,2))
    expired = time.time() - 120  # past the partial timeout, inside the join window
    os.utime(first,(expired,expired))
    poison = tmp_path/('phonelet.csv' if corrupt_source == 'cdr' else 'poison.sms')
    poison.write_text('corrupt record\n')
    consumer = replay(db,tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1  # set aside, alerted
    rows = await db.list_sms()
    assert len(rows) == 1 and rows[0]['partial']
    assert rows[0]['text'] == 'first [missing part 2/2]'
    await consumer.replay_once()
    assert len(await db.list_sms()) == 1

    # Repair the unrelated journal; a later sibling corrects the original row.
    if corrupt_source == 'cdr':
        poison.write_text(cdr_line('recovered'))
    else:
        poison.write_bytes(record('unrelated',slot=8))
    (tmp_path/'second.sms').write_bytes(record('second',2,2,slot=2))
    recovered = replay(db,tmp_path)
    await recovered.replay_once()
    assert recovered.errors == 0
    row = await db._one('SELECT * FROM sms WHERE id=?',(rows[0]['id'],))
    assert row['text'] == 'first second' and not row['partial']


async def test_failed_sibling_commit_defers_only_its_own_partial_group(db,tmp_path,monkeypatch):
    for name,raw in [('a1',record('A-one ',1,2)),('a2',record('A-two',2,2,slot=2)),
                     ('b1',record('B-one ',1,2,stamp='2026-09-25 13:00:00 +00:00',slot=3,ref=43))]:
        path = tmp_path/f'{name}.sms'
        path.write_bytes(raw)
        os.utime(path,(100,100))
    original = db.ingest_fragment
    async def fail_sibling(fragment, **kwargs):
        if fragment['text'] == 'A-two':
            raise sqlite3.OperationalError('temporary commit failure')
        await original(fragment, **kwargs)
    monkeypatch.setattr(db,'ingest_fragment',fail_sibling)
    consumer = replay(db,tmp_path)
    await consumer.replay_once()
    rows = await db.list_sms()
    assert consumer.errors == 1
    assert len(rows) == 1 and rows[0]['text'].startswith('B-one ')
    assert rows[0]['partial']
    monkeypatch.setattr(db,'ingest_fragment',original)
    await consumer.replay_once()
    rows = await db.list_sms()
    assert len(rows) == 2
    assert next(row for row in rows if row['text'].startswith('A-one'))['partial'] == 0


def unparsed_record(pdu='AABB', slot=3):
    return f'U1\t646f6e676c6530\t73696d31\t{slot}\t{pdu}\n'.encode()


@pytest.mark.parametrize('name,raw,quarantined', [
    ('invalid.sms', b'garbage\n', 1),
    ('phonelet.csv', b'one,invalid,line\n', 1),
    ('unparsed.sms', unparsed_record(), 0),
])
@pytest.mark.parametrize('first_backends', [('telegram', 'sip'), ('sip',)])
async def test_journal_alert_replay_does_not_notify_new_channels(tmp_path, name, raw, quarantined, first_backends):
    journal = tmp_path/name
    journal.write_bytes(raw)
    jobs = None
    for backends in (first_backends, ('telegram', 'homeassistant', 'sip')):
        db = Database(str(tmp_path/'history.db'), backends,
                      backend_methods={'sip': SipDelivery.accepted_methods})
        await db.init()
        try:
            consumer = replay(db, tmp_path)
            for _ in range(2):
                await consumer.replay_once()
                assert consumer.errors == 0
                assert consumer.status['ingress_quarantined'] == quarantined
                assert consumer.status['ingress_fresh']
            current_jobs = await outbox(db)
            if jobs is None:
                jobs = current_jobs
                assert [job['backend'] for job in jobs] == (['telegram'] if 'telegram' in first_backends else [])
            assert current_jobs == jobs
        finally:
            await db.close()
    assert journal.read_bytes() == raw


async def test_unparsed_record_alert_is_durable_and_idempotent(tmp_path):
    spool = tmp_path/'spool'
    spool.mkdir()
    raw = unparsed_record()
    journal = spool/'unparsed.sms'
    journal.write_bytes(raw)
    database = tmp_path/'history.db'
    for _ in range(2):
        db = Database(str(database), ('telegram', 'homeassistant', 'sip'),
                      backend_methods={'sip': SipDelivery.accepted_methods})
        await db.init()
        try:
            consumer = replay(db, spool)
            await consumer.replay_once()
            await consumer.replay_once()
            assert consumer.errors == 0 and journal in consumer._seen
            jobs = await db._all('SELECT * FROM outbox ORDER BY backend')
            assert [job['backend'] for job in jobs] == ['homeassistant', 'telegram']
            assert {job['event_key'] for job in jobs} == {'unparsed:' + hashlib.sha256(raw).hexdigest()}
            assert {job['method'] for job in jobs} == {'notify_health'}
            assert {json.loads(job['payload'])['message'] for job in jobs} == {
                'Unparsed SMS on device dongle0, slot 3; raw PDU retained in driver journal unparsed.sms'}
            assert await db.list_sms() == [] and await stored_parts(db) == []
        finally:
            await db.close()


async def test_unparsed_enqueue_failure_retries_without_holding_sms(db, tmp_path, monkeypatch):
    db.backends = ('telegram',)
    bad = tmp_path/'bad.sms'
    bad.write_bytes(unparsed_record())
    for i, text in [(1, 'first '), (2, 'second')]:
        path = tmp_path/f'part{i}.sms'
        path.write_bytes(record(text, i, 2, slot=i))
        os.utime(path, (100+i, 100+i))
    os.utime(bad, (100, 100))
    enqueue = db.enqueue_notification
    monkeypatch.setattr(db, 'enqueue_notification', AsyncMock(side_effect=sqlite3.OperationalError('full')))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 1 and bad not in consumer._seen
    assert [row['text'] for row in await db.list_sms()] == ['first second']
    monkeypatch.setattr(db, 'enqueue_notification', enqueue)
    await consumer.replay_once()
    await replay(db, tmp_path).replay_once()
    assert consumer.errors == 0 and bad in consumer._seen
    jobs = await outbox(db)
    assert len([job for job in jobs if job['event_key'].startswith('unparsed:')]) == 1
    assert len(await db.list_sms()) == 1


async def test_nul_text_survives_fragment_parsing(db):
    fragment = received_now(record('before\x00after'))
    assert fragment['text'] == 'before\x00after'
    await db.ingest_fragment(fragment)
    assert [row['text'] for row in await db.list_sms()] == ['before\x00after']


async def test_failed_unparsed_alert_does_not_hold_partial_publication(db, tmp_path, monkeypatch):
    db.backends = ('telegram',)
    (tmp_path/'bad.sms').write_bytes(unparsed_record())
    part = tmp_path/'part.sms'
    part.write_bytes(record('first ', 1, 2))
    expired = time.time() - 120
    os.utime(part, (expired, expired))
    monkeypatch.setattr(db, 'enqueue_notification', AsyncMock(side_effect=sqlite3.OperationalError('full')))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 1
    [row] = await db.list_sms()
    assert row['partial'] and row['text'] == 'first [missing part 2/2]'


@pytest.mark.history_rebuild
@pytest.mark.parametrize('history', [False, True], ids=['live', 'history'])
@pytest.mark.parametrize('stamp', ['2026-00-25 12:00:00 +00:00', '2026-13-40 25:61:00 +00:00', '',
                                 '9999-12-31 23:59:59 -01:00'],
                         ids=['month-00', 'invalid-calendar', 'empty', 'utc-overflow'])
async def test_invalid_scts_uses_receipt_time_without_duplicates(tmp_path, caplog, stamp, history):
    spool = tmp_path/'spool'
    spool.mkdir()
    item = spool/'invalid-date.sms'
    sender, text = '+15550000001', 'synthetic message with an invalid date'
    raw = record(text, stamp=stamp, sender=sender)
    item.write_bytes(raw)
    received = T0 + 0.125
    os.utime(item, (received, received))
    expected_stamp = datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat()
    source_id = 'fragment:' + hashlib.sha256(raw).hexdigest()
    path = str(tmp_path/'history.db')
    first = None
    with caplog.at_level(logging.WARNING, logger='phonelet.ingress'):
        for restart in range(2):
            db = Database(path, ('telegram', 'sip'), backend_methods={'sip': SipDelivery.accepted_methods})
            await db.init()
            try:
                if not history and not restart:
                    await db.complete_ingress()
                consumer = replay(db, spool)
                for _ in range(2):
                    await consumer.replay_once()
                    assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 0
                    [row] = await db.list_sms()
                    assert row['text'] == text and row['sender'] == sender and not row['partial']
                    assert row['timestamp'] == row['received_at'] == expected_stamp
                    assert row['source_id'] == 'assembly:' + source_id
                    jobs = await outbox(db)
                    assert sorted((j['event_key'], j['backend']) for j in jobs) == (
                        [] if history else [(f"sms:{row['id']}", 'sip'), (f"sms:{row['id']}", 'telegram')])
                    assert all(json.loads(j['payload']) == {'sender': sender, 'text': text} for j in jobs)
                    assert await db._all('SELECT * FROM alert_events') == []
                    assert await db._all('SELECT source_id FROM fragments') == [{'source_id': source_id}]
                    if first is None:
                        first = row, jobs
                    assert (row, jobs) == first
            finally:
                await db.close()
    warnings = [r for r in caplog.records if r.name == 'phonelet.ingress' and r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == [
        'Invalid SMS date in journal invalid-date.sms; replaced with receipt time']
    assert sender not in caplog.text and text not in caplog.text
    if stamp:
        assert stamp not in caplog.text
    assert item.read_bytes() == raw and item.stat().st_mtime == received


@pytest.mark.parametrize('invalid_parts', [(1, 2), (1,), (2,), ()],
                         ids=['both', 'first', 'second', 'valid-equals-receipt'])
@pytest.mark.parametrize('order', [(1, 2), (2, 1)], ids=['in-order', 'reversed'])
async def test_invalid_scts_multipart_joins_its_only_candidate_after_restart(tmp_path, invalid_parts, order):
    """With one open candidate a receipt-time substitute joins it: one complete SMS, also after replay."""
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    received = float(int(time.time()))
    before = None
    for index, part in enumerate(order):
        item = spool/f'part-{part}.sms'
        stamp = '2026-00-25 12:00:00 +00:00' if part in invalid_parts else scts(received - 600)
        if not invalid_parts:  # a genuine SCTS equal to receipt time is treated the same way
            stamp = scts(received + index)
        item.write_bytes(record({1: 'first ', 2: 'second'}[part], part, 2, stamp=stamp,
                                slot=part, sender='+15550000001'))
        os.utime(item, (received + index, received + index))
        db = Database(path, ('telegram',))  # a restart between the parts
        await db.init()
        try:
            await replay(db, spool).replay_once()
            if not index:
                assert await db.list_sms() == []
            else:
                [row] = await db.list_sms()
                assert row['text'] == 'first second' and not row['partial']
                # The SMS time is the first accepted part's (its substitute or SCTS).
                first_stamp = (datetime.fromtimestamp(received, timezone.utc).isoformat()
                               if order[0] in invalid_parts or not invalid_parts
                               else datetime.fromtimestamp(received - 600, timezone.utc).isoformat())
                assert row['timestamp'] == first_stamp
                [job] = await outbox(db)
                assert json.loads(job['payload'])['text'] == 'first second'
                assert (await db._one('SELECT COUNT(*) AS n FROM fragments'))['n'] == 2
                assert await db._all('SELECT ambiguous FROM assemblies') == [{'ambiguous': 0}]
                before = [row], [job]
        finally:
            await db.close()
    db = Database(path, ('telegram',))
    await db.init()
    try:
        for _ in range(2):
            await replay(db, spool).replay_once()
            await db.flush_fragments(timeout=60, now=received + 120)
            assert (await db.list_sms(), await outbox(db)) == before
    finally:
        await db.close()


@pytest.mark.parametrize('restart', [False, True], ids=['same-process', 'restart-between-messages'])
@pytest.mark.parametrize('stamp', ['', 'valid'], ids=['invalid-scts', 'valid-scts'])
async def test_interleaved_reference_joins_the_only_candidate_like_valid_scts(tmp_path, restart, stamp):
    """A substitute follows the valid-SCTS rule: a single free candidate is joined, whatever its date.

    Interleaved arrivals of two messages sharing sender, reference and total are therefore paired by
    arrival order (SMS has no unique message ID); the result is the same in one pass and after a restart."""
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    received = time.time()
    arrivals = [('A-one ', 1), ('B-two', 2), ('B-one ', 1), ('A-two', 2)]
    db = Database(path, ('telegram',))
    await db.init()
    try:
        consumer = replay(db, spool)
        for index, (text, part) in enumerate(arrivals):
            item = spool/f'{index}.sms'
            item.write_bytes(record(text, part, 2, stamp=stamp and scts(received - 600 + 60 * index),
                                    slot=index + 1, sender='+15550000001'))
            os.utime(item, (received + index, received + index))
            await consumer.replay_once()
            assert consumer.errors == 0 and not consumer.quarantined
            if restart and index == 1:
                await db.close()
                db = Database(path, ('telegram',))
                await db.init()
                consumer = replay(db, spool)
        await db.flush_fragments(timeout=60, now=received + 64)
        rows = await db.list_sms()
        assert sorted(r['text'] for r in rows) == ['A-one B-two', 'B-one A-two']
        assert not any(r['partial'] for r in rows)
        jobs = await outbox(db)
        assert len(jobs) == 2
        await db.close()
        db = Database(path, ('telegram',))
        await db.init()
        await replay(db, spool).replay_once()
        assert (await db.list_sms(), await outbox(db)) == (rows, jobs)
    finally:
        await db.close()


@pytest.mark.parametrize('invalid', ['incoming', 'candidate', 'all', 'valid-equals-receipt'])
async def test_invalid_scts_receipt_time_cannot_resolve_reference_collision(tmp_path, invalid):
    spool = tmp_path/'spool'
    spool.mkdir()
    path = str(tmp_path/'history.db')
    # Deliberately equal: a receipt-time fallback must never act as SCTS evidence.
    received = float(int(time.time())) - 120
    stamps = [scts(received), scts(received - 600), scts(received)]
    if invalid == 'all':
        stamps = ['', '', '']
    elif invalid == 'incoming':
        stamps[0], stamps[2] = scts(received + 2), ''
    elif invalid == 'candidate':
        stamps[0] = ''
    for index, (text, part, stamp) in enumerate(zip(['A-one ', 'B-one ', 'unknown-two'], [1, 1, 2], stamps)):
        item = spool/f'{index}.sms'
        item.write_bytes(record(text, part, 2, stamp=stamp, slot=index + 1, sender='+15550000001'))
        os.utime(item, (received + index, received + index))
        # Persist candidate timestamps, then reopen before the colliding part arrives.
        db = Database(path, ('telegram',))
        await db.init()
        try:
            await replay(db, spool).replay_once()
            if index == 2:
                rows = await db.list_sms()
                marker = '[ambiguous multipart reference] '
                assert sorted(r['text'] for r in rows) == sorted([
                    marker + 'A-one [missing part 2/2]', marker + 'B-one [missing part 2/2]',
                    marker + '[missing part 1/2]unknown-two'])
                assert all(r['partial'] for r in rows)
                assert (await db._one('SELECT COUNT(*) AS n FROM fragments'))['n'] == 3
                assert not await invalid_record_alerts(db)
        finally:
            await db.close()


@pytest.mark.parametrize('raw', [b'garbage\n', record('bad', 0, 2)], ids=['garbage', 'part-0'])
async def test_invalid_sms_record_is_set_aside_with_one_durable_alert(tmp_path, raw):
    """A record that can never be stored alerts once, also across a restart, and leaves /ready alone."""
    path = str(tmp_path/'history.db')
    spool = tmp_path/'spool'
    spool.mkdir()
    (spool/'bad.sms').write_bytes(raw)
    (spool/'good.sms').write_bytes(record('fine', slot=2))
    for _ in range(2):  # the second round is a restart: new process state, same database
        db = Database(path, ('telegram', 'homeassistant', 'sip'),
                      backend_methods={'sip': SipDelivery.accepted_methods})
        await db.init()
        try:
            consumer = replay(db, spool)
            for _ in range(2):
                await consumer.replay_once()
                assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
                assert consumer.status['ingress_fresh']
            assert [r['text'] for r in await db.list_sms()] == ['fine']
            jobs = [job for job in await outbox(db) if job['event_key'] == sms_invalid_key(spool/'bad.sms')]
            assert sorted(job['backend'] for job in jobs) == ['homeassistant', 'telegram']  # SIP takes SMS only
            assert json.loads(jobs[0]['payload']) == {'message': 'Invalid SMS journal record bad.sms set aside; '
                                                      'it stays in the driver journal, its SMS is not in the history'}
        finally:
            await db.close()
    assert (spool/'bad.sms').read_bytes() == raw  # the journal is never changed


async def test_invalid_sms_record_whose_alert_fails_is_retried(db, tmp_path, monkeypatch):
    db.backends = ('telegram',)
    (tmp_path/'bad.sms').write_bytes(b'garbage\n')
    enqueue = db.enqueue_notification
    monkeypatch.setattr(db, 'enqueue_notification', AsyncMock(side_effect=sqlite3.OperationalError('locked')))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 1 and not consumer.quarantined  # not skipped before its alert is queued
    monkeypatch.setattr(db, 'enqueue_notification', enqueue)
    await consumer.replay_once()
    assert consumer.errors == 0 and await invalid_record_alerts(db) == [sms_invalid_key(tmp_path/'bad.sms')]


@pytest.mark.parametrize('bad', [
    cdr_line('short').replace(',ANSWERED,4,', ',ANSWERED,,'),
    cdr_line('stamp', stamp='2026-13-40 99:00:00'),
    'two,fields\n',
    cdr_line('bytes').encode().replace(b'+7', b'+\xff7').decode('latin-1'),
], ids=['empty-duration', 'invalid-start', 'field-count', 'not-utf-8'])
async def test_invalid_cdr_line_is_set_aside_and_later_calls_are_stored(tmp_path, bad):
    path = str(tmp_path/'history.db')
    (tmp_path/'phonelet.csv').write_bytes(cdr_line('before').encode() + bad.encode('latin-1')
                                          + cdr_line('after').encode())
    for _ in range(2):  # the second round is a restart: the file is read again from its start
        db = Database(path, ('telegram',))
        await db.init()
        try:
            consumer = replay(db, tmp_path)
            await consumer.replay_once()
            assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
            assert sorted(c['source_id'] for c in await db.list_calls()) == ['cdr:after', 'cdr:before']
            [key] = await invalid_record_alerts(db)
            assert key == 'cdr-invalid:' + hashlib.sha256(bad.encode('latin-1')).hexdigest()
        finally:
            await db.close()


def quoted_cdr_line(uid):
    """Match cdr_custom.conf: CSV_QUOTE on every field, including empty fields."""
    fields = [uid, 'Dongle/dongle0-1', 'Dial', '+15550000001', 'ANSWERED', '4', '',
              '2026-09-25T12:00:00+0000']
    return (','.join('"' + field.replace('"', '""') + '"' for field in fields) + '\n').encode()


@pytest.mark.parametrize('torn', [b'\0' * 512 + b'\n', b'\0' * 4096 + b' \r\n', b'\0' * 512],
                         ids=['nul-line', 'nul-line-whitespace', 'nul-before-record'])
async def test_nul_run_from_a_torn_cdr_write_is_not_part_of_a_record(tmp_path, torn):
    """A zero-filled block alone on a line is skipped; in front of a record it is dropped from it."""
    path = str(tmp_path/'history.db')
    data = cdr_line('before').encode() + torn + cdr_line('after').encode()
    (tmp_path/'phonelet.csv').write_bytes(data)
    for _ in range(2):  # the second round is a restart: the file is read again from its start
        db = Database(path, ('telegram',))
        await db.init()
        try:
            consumer = replay(db, tmp_path)
            for _ in range(2):
                await consumer.replay_once()
                assert consumer.errors == 0 and not consumer.quarantined
                assert sorted(c['source_id'] for c in await db.list_calls()) == ['cdr:after', 'cdr:before']
                assert await invalid_record_alerts(db) == []
                assert consumer.records == 2
                assert consumer._cdr_offsets == {(os.stat(tmp_path/'phonelet.csv').st_dev,
                                                  os.stat(tmp_path/'phonelet.csv').st_ino): len(data)}
        finally:
            await db.close()


@pytest.mark.parametrize('bad', [
    cdr_line('mid\0dle').encode(),
    cdr_line('tail').encode().replace(b'\r\n', b'\0\0\r\n'),
    quoted_cdr_line('quoted').replace(b'Dial', b'Di\0al'),
], ids=['in-uniqueid', 'before-newline', 'in-quoted-field'])
async def test_nul_inside_a_cdr_record_is_set_aside_and_later_calls_are_stored(tmp_path, caplog, bad):
    path = str(tmp_path/'history.db')
    (tmp_path/'phonelet.csv').write_bytes(cdr_line('before').encode() + bad + cdr_line('after').encode())
    for _ in range(2):
        db = Database(path, ('telegram',))
        await db.init()
        try:
            consumer = replay(db, tmp_path)
            await consumer.replay_once()
            assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
            assert sorted(c['source_id'] for c in await db.list_calls()) == ['cdr:after', 'cdr:before']
            [key] = await invalid_record_alerts(db)
            assert key == 'cdr-invalid:' + hashlib.sha256(bad).hexdigest()
        finally:
            await db.close()
    assert '\0' not in caplog.text and 'Dongle/dongle0-1' not in caplog.text


@pytest.mark.parametrize('following_count', [1, 2])
async def test_torn_cdr_is_quarantined_and_later_appends_are_stored(db, tmp_path, monkeypatch, caplog,
                                                                 following_count):
    import phonelet.ingress as module
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    db.backends = ('telegram',)
    path = tmp_path/'phonelet.csv'
    before = quoted_cdr_line('before')
    torn = quoted_cdr_line('torn').partition(b'dongle0')[0]
    glued = quoted_cdr_line('glued')
    raw = before + torn + glued + (quoted_cdr_line('after') if following_count == 2 else b'')
    path.write_bytes(raw)
    consumer = replay(db, tmp_path)
    expected = ['cdr:before'] + (['cdr:after'] if following_count == 2 else [])
    key = 'cdr-invalid:' + hashlib.sha256(torn + glued).hexdigest()
    for _ in range(2):
        await consumer.replay_once()
        assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
        assert sorted(row['source_id'] for row in await db.list_calls()) == sorted(expected)
        assert await invalid_record_alerts(db) == [key]
        assert list(consumer._cdr_offsets.values()) == [len(raw)]
        clock[0] += consumer._cdr_partial_grace + 1
    with path.open('ab') as stream:
        stream.write(quoted_cdr_line('next'))
    await consumer.replay_once()
    assert consumer.errors == 0
    assert sorted(row['source_id'] for row in await db.list_calls()) == sorted(expected + ['cdr:next'])
    [job] = [job for job in await outbox(db) if job['event_key'] == key]
    assert json.loads(job['payload']) == {
        'message': 'Invalid CDR line in phonelet.csv set aside; a call or SMS failure report '
                   'may be missing from the history'}
    assert '+15550000001' not in caplog.text and 'glued' not in caplog.text and 'torn' not in caplog.text


@pytest.mark.parametrize('newline', [b'', b'\n'], ids=['no-newline', 'quoted-newline'])
async def test_torn_cdr_tail_logs_once_after_grace_and_recovers(db, tmp_path, monkeypatch, caplog, newline):
    import phonelet.ingress as module
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    db.backends = ('telegram',)
    path = tmp_path/'phonelet.csv'
    before = quoted_cdr_line('before')
    torn = quoted_cdr_line('tail-private').partition(b'2026-')[0] + b'2026-' + newline
    path.write_bytes(before + torn)
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.errors == 0 and not consumer.quarantined
    assert not caplog.records
    clock[0] += consumer._cdr_partial_grace + 1
    for _ in range(2):
        await consumer.replay_once()
        assert consumer.errors == 1 and not consumer.quarantined
        assert list(consumer._cdr_offsets.values()) == [len(before)]
    errors = [r for r in caplog.records if r.name == 'phonelet.ingress' and r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert path.name in errors[0].getMessage() and str(len(before)) in errors[0].getMessage()
    assert errors[0].exc_info is None
    glued = quoted_cdr_line('glued')
    with path.open('ab') as stream:
        stream.write(glued + quoted_cdr_line('after'))
    await consumer.replay_once()
    assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
    assert sorted(row['source_id'] for row in await db.list_calls()) == ['cdr:after', 'cdr:before']
    assert await invalid_record_alerts(db) == ['cdr-invalid:' + hashlib.sha256(torn + glued).hexdigest()]
    assert '+15550000001' not in caplog.text and 'tail-private' not in caplog.text
    [alert] = [job for job in await outbox(db) if job['event_key'].startswith('cdr-invalid:')]
    assert '+15550000001' not in alert['payload'] and 'tail-private' not in alert['payload']


@pytest.mark.parametrize('newline', [b'', b'\n'], ids=['no-newline', 'quoted-newline'])
async def test_oversized_cdr_tail_logs_once_without_waiting(db, tmp_path, monkeypatch, caplog, newline):
    import phonelet.ingress as module
    path = tmp_path/'phonelet.csv'
    torn = quoted_cdr_line('oversized-private').partition(b'2026-')[0] + b'2026-' + newline
    path.write_bytes(torn)
    monkeypatch.setattr(module, 'CDR_BATCH_BYTES', len(torn))
    consumer = replay(db, tmp_path)
    for _ in range(2):
        await consumer.replay_once()
        assert consumer.errors == 1 and not consumer.quarantined
    assert not await db.list_calls()
    errors = [r for r in caplog.records if r.name == 'phonelet.ingress' and r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert path.name in errors[0].getMessage() and 'offset 0' in errors[0].getMessage()
    assert errors[0].exc_info is None
    assert '+15550000001' not in caplog.text and 'oversized-private' not in caplog.text


async def test_every_torn_cdr_prefix_reaches_the_last_complete_call(tmp_path, monkeypatch):
    import phonelet.ingress as module
    clock = [100.]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    line = quoted_cdr_line('torn')
    glued, last = quoted_cdr_line('glued'), quoted_cdr_line('last')
    valid_splices = []
    for cut in range(1, len(line)):
        spool = tmp_path/str(cut)
        spool.mkdir()
        (spool/'phonelet.csv').write_bytes(line[:cut] + glued + last)
        database = Database(str(spool/'history.db'))
        await database.init()
        try:
            consumer = replay(database, spool)
            await consumer.replay_once()
            clock[0] += consumer._cdr_partial_grace + 1
            await consumer.replay_once()
            assert consumer.errors == 0, cut
            calls = await database.list_calls()
            expected = ['cdr:last']
            # A splice that is itself valid CSV cannot be recognized as torn.
            try:
                row = next(csv.reader([(line[:cut] + glued).decode()], strict=True))
            except csv.Error:
                row = []
            if len(row) == 8:
                valid_splices.append(cut)
                expected.append('cdr:' + row[0])
            assert sorted(call['source_id'] for call in calls) == sorted(expected), cut
            assert all(call['caller'] == '+15550000001' and call['duration'] == 4 and
                       call['timestamp'] == '2026-09-25T12:00:00+00:00' for call in calls), cut
            assert list(consumer._cdr_offsets.values()) == [len(line[:cut] + glued + last)], cut
        finally:
            await database.close()
    assert valid_splices == [len(b'"torn"')]


async def test_torn_cdr_quarantine_failure_retries_from_the_same_offset(db, tmp_path, monkeypatch):
    db.backends = ('telegram',)
    path = tmp_path/'phonelet.csv'
    before = quoted_cdr_line('before')
    torn = quoted_cdr_line('torn').partition(b'dongle0')[0]
    glued = quoted_cdr_line('glued')
    raw = before + torn + glued + quoted_cdr_line('after')
    path.write_bytes(raw)
    enqueue = db.enqueue_notification
    failing = AsyncMock(side_effect=sqlite3.OperationalError('locked'))
    monkeypatch.setattr(db, 'enqueue_notification', failing)
    consumer = replay(db, tmp_path)
    for _ in range(2):
        await consumer.replay_once()
        assert consumer.errors == 1 and not consumer.quarantined
        assert list(consumer._cdr_offsets.values()) == [len(before)]
        assert [call['source_id'] for call in await db.list_calls()] == ['cdr:before']
    key = 'cdr-invalid:' + hashlib.sha256(torn + glued).hexdigest()
    assert [call.kwargs['event_key'] for call in failing.await_args_list] == [key, key]
    monkeypatch.setattr(db, 'enqueue_notification', enqueue)
    await consumer.replay_once()
    assert consumer.errors == 0 and consumer.status['ingress_quarantined'] == 1
    assert sorted(call['source_id'] for call in await db.list_calls()) == ['cdr:after', 'cdr:before']
    assert list(consumer._cdr_offsets.values()) == [len(raw)]
    assert await invalid_record_alerts(db) == [key]


@pytest.mark.parametrize('failure', [sqlite3.OperationalError('disk I/O error'), ValueError('no active connection')],
                         ids=['sqlite', 'closed-connection'])
async def test_cdr_storage_failure_is_retried_not_set_aside(db, tmp_path, failure):
    """Only parsing marks a CDR line invalid: any failure to store it, even a ValueError, is retried."""
    (tmp_path/'phonelet.csv').write_text(cdr_line('kept'))
    failing = [True]

    async def call(**kwargs):
        if failing:
            raise failure
        await db.insert_call(**kwargs)
    consumer = IngressReplay(db, CdrHandler(call, AsyncMock()), tmp_path, tmp_path/'phonelet.csv')
    await consumer.replay_once()
    assert consumer.errors == 1 and not consumer.quarantined
    failing.clear()
    await consumer.replay_once()
    assert consumer.errors == 0 and [c['source_id'] for c in await db.list_calls()] == ['cdr:kept']


@pytest.mark.history_rebuild
@pytest.mark.parametrize('stop', ['commit', 'restart', 'completion-commit'])
async def test_first_inventory_survives_interruption_and_bounds_batched_cdr(tmp_path, monkeypatch, stop):
    import phonelet.ingress as module
    path = str(tmp_path / 'rebuild.db')
    cdr = tmp_path / 'phonelet.csv'
    lines = [cdr_line(str(i), '2099-01-01T00:00:00+00:00') for i in range(3)]
    cdr.write_text(''.join(lines))
    (tmp_path / 'old.sms').write_bytes(record('old'))
    # Exactly one line per pass, including each rotated file.
    monkeypatch.setattr(module, 'CDR_BATCH_BYTES', len(lines[0].encode()) + 8)
    rotated = tmp_path / 'phonelet.csv.1'
    rotated.write_text(cdr_line('r', '2099-01-01T00:00:00+00:00'))
    db = Database(path, ('test',))
    await db.init()
    try:
        consumer = replay(db, tmp_path)
        await consumer._first_inventory()
        if stop == 'commit':
            commit = db._conn.commit
            with monkeypatch.context() as patcher:
                async def failed_commit():
                    raise sqlite3.OperationalError('full')
                patcher.setattr(db._conn, 'commit', failed_commit)
                # All record commits fail, the durable boundary remains unchanged.
                with pytest.raises(sqlite3.OperationalError):
                    await consumer.replay_once()
            assert db._conn.commit == commit
            assert await db.list_sms() == []
        else:
            await consumer.replay_once()
        assert not (await db.ingress_state())[0]
        if stop == 'completion-commit':
            complete = db.complete_ingress
            monkeypatch.setattr(db, 'complete_ingress', AsyncMock(side_effect=sqlite3.OperationalError('full')))
            with pytest.raises(sqlite3.OperationalError):
                for _ in range(5):
                    await consumer.replay_once()
            assert not (await db.ingress_state())[0]
            monkeypatch.setattr(db, 'complete_ingress', complete)
    finally:
        await db.close()
    # A new Python process on the same DB sees the original boundary, not a new inventory.
    db = Database(path, ('test',))
    await db.init()
    try:
        consumer = replay(db, tmp_path)
        for _ in range(6):
            await consumer.replay_once()
        assert (await db.ingress_state())[0]
        assert len(await db.list_calls()) == 4 and len(await db.list_sms()) == 1
        assert await outbox(db) == []
        (tmp_path / 'new.sms').write_bytes(record('new', slot=9))
        with cdr.open('a') as stream:
            stream.write(cdr_line('new', '2000-01-01T00:00:00+00:00'))
        for _ in range(3):
            await consumer.replay_once()
        assert len(await outbox(db)) == 2
    finally:
        await db.close()


@pytest.mark.history_rebuild
@pytest.mark.parametrize('defect', ['sms-commit', 'sms-read', 'cdr-quarantine'])
async def test_persistent_initial_failure_does_not_make_new_arrivals_history(tmp_path, monkeypatch, defect):
    import phonelet.ingress as module
    db = Database(str(tmp_path / 'rebuild.db'), ('test',))
    await db.init()
    try:
        cdr = tmp_path / 'phonelet.csv'
        cdr.write_text('broken,line\n' if defect == 'cdr-quarantine' else cdr_line('old'))
        old = tmp_path / 'old.sms'
        old.write_bytes(record('old'))
        consumer = replay(db, tmp_path)
        clock = [100.]
        monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
        if defect == 'sms-read':
            read_bytes = Path.read_bytes
            def read(path):
                if path == old:
                    raise PermissionError('unreadable')
                return read_bytes(path)
            monkeypatch.setattr(Path, 'read_bytes', read)
        elif defect == 'sms-commit':
            ingest = db.ingest_fragment
            async def fail_old(fragment, **kwargs):
                if fragment['text'] == 'old':
                    raise sqlite3.OperationalError('full')
                return await ingest(fragment, **kwargs)
            monkeypatch.setattr(db, 'ingest_fragment', fail_old)
        else:
            enqueue = db.enqueue_notification
            async def fail_quarantine(*args, **kwargs):
                if kwargs.get('event_key', '').startswith('cdr-invalid:'):
                    raise sqlite3.OperationalError('full')
                return await enqueue(*args, **kwargs)
            monkeypatch.setattr(db, 'enqueue_notification', fail_quarantine)
        await consumer.replay_once()
        assert consumer.errors > 0 and not (await db.ingress_state())[0]
        (tmp_path / 'new.sms').write_bytes(record('new', slot=9, ref=99))
        # Put the new CDR in another retained file so an old failed line cannot hold its read.
        (tmp_path / 'phonelet.csv.2').write_text(cdr_line('new', '2000-01-01T00:00:00+00:00'))
        clock[0] += module.SMS_BLOCK_ALERT_AFTER + 1
        await consumer.replay_once()
        jobs = await db._all('SELECT event_key,method,payload FROM outbox')
        assert not (await db.ingress_state())[0]
        assert any(j['method'] == 'notify_call' for j in jobs)
        if defect == 'sms-read':
            assert any(j['event_key'].startswith('sms-journal-blocked:') for j in jobs)
        else:
            assert any(json.loads(j['payload']).get('text') == 'new' for j in jobs)
    finally:
        await db.close()


@pytest.mark.history_rebuild
async def test_new_u1_and_quarantine_alerts_notify_with_old_clock_after_first_pass(tmp_path):
    db = Database(str(tmp_path / 'rebuild.db'), ('test',))
    await db.init()
    try:
        consumer = replay(db, tmp_path)
        await consumer.replay_once()
        (tmp_path / 'unparsed.sms').write_bytes(unparsed_record())
        (tmp_path / 'bad.sms').write_bytes(b'broken\n')
        for path in tmp_path.glob('*.sms'):
            os.utime(path, (100, 100))
        cdr = tmp_path / 'phonelet.csv'
        cdr.write_text('invalid,line\n')
        os.utime(cdr, (100, 100))
        await consumer.replay_once()
        keys = [job['event_key'].split(':')[0] for job in await outbox(db)]
        assert sorted(keys) == ['cdr-invalid', 'sms-journal-invalid', 'unparsed']
        await replay(db, tmp_path).replay_once()
        assert len(await outbox(db)) == 3
    finally:
        await db.close()


@pytest.mark.history_rebuild
@pytest.mark.parametrize('restart', [False, True], ids=['same-process', 'restarted-before-truncation'])
@pytest.mark.parametrize('empty', [False, True], ids=['replacement-row', 'empty-file'])
async def test_truncated_initial_cdr_retires_boundary_durably(tmp_path, monkeypatch, restart, empty):
    import phonelet.ingress as module
    db_path = str(tmp_path / 'rebuild.db')
    cdr = tmp_path / 'phonelet.csv'
    old_line = cdr_line('old', source='x' * 200)
    cdr.write_text(old_line * 3)
    monkeypatch.setattr(module, 'CDR_BATCH_BYTES', len(old_line.encode()) + 8)
    # Keep completion pending independently of CDR to check persistence across restart.
    sms = tmp_path / 'pending.sms'
    sms.write_bytes(record('pending'))
    async def blocked(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic unavailable SMS commit')
    db = Database(db_path, ('test',))
    await db.init()
    try:
        monkeypatch.setattr(db, 'ingest_fragment', blocked)
        consumer = replay(db, tmp_path)
        await consumer.replay_once()
        assert not (await db.ingress_state())[0] and await outbox(db) == []
        identity = next(iter(consumer._cdr_offsets))
        if restart:
            await db.close()
            db = Database(db_path, ('test',))
            await db.init()
            monkeypatch.setattr(db, 'ingest_fragment', blocked)
            consumer = replay(db, tmp_path)
        cdr.write_text('' if empty else cdr_line('new'))  # preserve the inode
        await consumer.replay_once()
        complete, snapshot = await db.ingress_state()
        assert not complete and f'{identity[0]}:{identity[1]}' not in snapshot['cdr']
        assert len(await outbox(db)) == (0 if empty else 1)
        if empty:
            assert consumer._cdr_offsets[identity] == 0
    finally:
        await db.close()
    db = Database(db_path, ('test',))
    await db.init()
    try:
        # Regrow past the former boundary: only the durable removal prevents
        # these replacement records from silently becoming history on restart.
        with cdr.open('a') as stream:
            for i in range(12):
                stream.write(cdr_line(f'replacement-{i}', source='y' * 100))
        consumer = replay(db, tmp_path)
        for _ in range(15):
            await consumer.replay_once()
        assert (await db.ingress_state())[0]  # no wait for the original CDR byte length
        # The previously blocked SMS is still history; every replacement call notifies.
        jobs = await db._all('SELECT method FROM outbox')
        assert jobs == [{'method': 'notify_call'}] * (12 if empty else 13)
    finally:
        await db.close()


@pytest.mark.history_rebuild
@pytest.mark.parametrize('regrow', [False, True], ids=['still-short', 'regrown-before-retry'])
async def test_cdr_truncation_boundary_commit_failure_retries_before_notifying(db, tmp_path, monkeypatch, regrow):
    cdr = tmp_path / 'phonelet.csv'
    cdr.write_text(cdr_line('old', source='x' * 200) * 3)
    consumer = replay(db, tmp_path)
    await consumer._first_inventory()
    cdr.write_text(cdr_line('new'))
    db.backends = ('test',)
    with monkeypatch.context() as patcher:
        patcher.setattr(db, 'save_ingress_snapshot', AsyncMock(side_effect=sqlite3.OperationalError('full')))
        await consumer.replay_once()
    assert consumer.errors == 1 and await db.list_calls() == [] and await outbox(db) == []
    assert not (await db.ingress_state())[0]
    if regrow:
        # Do not lose the observed truncation when its first metadata commit
        # fails and the file grows beyond the old boundary before the retry.
        with cdr.open('a') as stream:
            stream.write(cdr_line('regrown', source='x' * 1000))
    await consumer.replay_once()
    assert (await db.ingress_state())[0]
    assert sorted(row['source_id'] for row in await db.list_calls()) == (
        ['cdr:new', 'cdr:regrown'] if regrow else ['cdr:new'])
    assert len(await outbox(db)) == (2 if regrow else 1)


@pytest.mark.history_rebuild
async def test_failed_initial_listing_does_not_ingest_sms_on_a_successful_retry(db, tmp_path, monkeypatch):
    import phonelet.ingress as module
    from phonelet.sms_commands.commands.wifi import CODE, pack_args
    db.sms_commands_enabled = True
    spool = tmp_path / 'spool'
    spool.mkdir()
    now = time.time()
    command = envelope.encode(bytes(32), CODE, pack_args('Example-Net', 'example-password'), int(now * 1000))
    (spool / 'old.sms').write_bytes(record('retained text'))
    (spool / 'command.sms').write_bytes(record(command, slot=2, stamp=scts(now)))
    (spool / 'phonelet.csv').write_text(cdr_line('old'))
    db.backends = ('test',)
    listdir = os.listdir
    listing_calls = 0
    failure = PermissionError('synthetic initial listing failure')

    def fail_first_listing(target='.'):
        nonlocal listing_calls
        if Path(target) == spool:
            listing_calls += 1
            if listing_calls == 1:
                raise failure
        return listdir(target)

    monkeypatch.setattr(module.os, 'listdir', fail_first_listing)
    consumer = replay(db, spool)
    hold_sms = AsyncMock(wraps=consumer._hold_sms)
    monkeypatch.setattr(consumer, '_hold_sms', hold_sms)
    await consumer.replay_once()
    assert consumer.errors == 1 and consumer._sms_block.path == spool
    hold_sms.assert_awaited_once_with(spool, failure)
    assert await db.list_sms() == [] and await stored_parts(db) == []
    assert await db._all('SELECT * FROM sms_commands') == [] and await outbox(db) == []
    assert [row['source_id'] for row in await db.list_calls()] == ['cdr:old']  # CDR still drains
    complete, snapshot = await db.ingress_state()
    assert not complete and snapshot['sms'] is None
    await consumer.replay_once()  # every listing after the first failure succeeds
    assert consumer.errors == 0 and consumer._sms_block is None
    assert sorted(row['text'] for row in await db.list_sms()) == ['[command]', 'retained text']
    assert await db._all('SELECT * FROM sms_commands') == [] and await outbox(db) == []
    assert (await db.ingress_state())[0]


@pytest.mark.history_rebuild
async def test_first_pass_status_during_and_after_a_long_inventory(tmp_path):
    # Synthetic journals: 200 SMS fragment files and 200 CDR lines in one first pass.
    spool, count = tmp_path / 'spool', 200
    spool.mkdir()
    for i in range(count):
        path = spool / f'{i:05d}.sms'
        path.write_bytes(record(f'text {i}', slot=i % 256, ref=i % 256))
        os.utime(path, (T0 + i, T0 + i))
    cdr = spool / 'phonelet.csv'
    cdr.write_text(''.join(cdr_line(f'call-{i}', '2026-09-25T12:00:00+00:00') for i in range(count)))
    db = Database(str(tmp_path / 'new.db'), ('test',))
    await db.init()
    try:
        consumer = IngressReplay(db, CdrHandler(db.insert_call, AsyncMock()), spool, cdr)
        assert consumer.status['ingress_first_pass'] is True and consumer.status['ingress_records'] == 0
        seen = []
        ingest = db.ingest_fragment
        async def observed(fragment, **kwargs):
            seen.append((consumer.status['ingress_first_pass'], consumer.status['ingress_records']))
            return await ingest(fragment, **kwargs)
        db.ingest_fragment = observed
        await consumer.replay_once()
        assert consumer.errors == 0
        assert all(first for first, _ in seen) and [n for _, n in seen] == list(range(count))
        assert consumer.status['ingress_first_pass'] is False
        assert consumer.status['ingress_records'] == 2 * count
        assert (await db.ingress_state())[0]
    finally:
        await db.close()


@pytest.mark.history_rebuild
async def test_first_pass_stays_reported_while_a_record_fails(db, tmp_path, monkeypatch):
    (tmp_path / 'one.sms').write_bytes(record('one'))
    (tmp_path / 'bad.sms').write_bytes(b'garbage\n')  # quarantined, still consumed
    consumer = replay(db, tmp_path)
    original = db.ingest_fragment
    monkeypatch.setattr(db, 'ingest_fragment', AsyncMock(side_effect=sqlite3.OperationalError('full')))
    await consumer.replay_once()
    assert consumer.status['ingress_first_pass'] is True
    assert consumer.status['ingress_records'] == 1
    monkeypatch.setattr(db, 'ingest_fragment', original)
    await consumer.replay_once()
    assert consumer.status['ingress_first_pass'] is False
    assert consumer.status['ingress_records'] == 2


async def test_already_complete_database_reports_no_first_pass(db, tmp_path):
    (tmp_path / 'one.sms').write_bytes(record('one'))
    (tmp_path / 'phonelet.csv').write_text(cdr_line('call'))
    consumer = replay(db, tmp_path)
    await consumer.replay_once()
    assert consumer.status['ingress_first_pass'] is False
    assert consumer.status['ingress_records'] == 2
    await consumer.replay_once()  # seen records are not counted again
    assert consumer.status['ingress_records'] == 2
