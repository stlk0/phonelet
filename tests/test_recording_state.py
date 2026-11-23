import asyncio
from datetime import datetime, timezone
import json
import os
from unittest.mock import AsyncMock, call

import pytest

from phonelet import recording
from phonelet.recording import recording_state, reconcile_recordings
from tests.helpers import store_call


async def test_pending_failed_ready_recovery(db,tmp_path):
    path = tmp_path/'test.mp3'
    call_id = await store_call(db, '+7',10,'answered',str(path))
    await reconcile_recordings(db,tmp_path)
    assert (await db.get_call(call_id))['recording_state'] == 'pending'
    sidecar = path.with_name(path.name+'.status.json')
    sidecar.write_text(json.dumps({'status':'failed'}))
    await reconcile_recordings(db,tmp_path)
    assert (await db.get_call(call_id))['recording_state'] == 'failed'
    path.write_bytes(b'ID3valid audio placeholder')
    sidecar.write_text(json.dumps({'status':'ready'}))
    await reconcile_recordings(db,tmp_path)
    assert (await db.get_call(call_id))['recording_state'] == 'failed'
    await reconcile_recordings(db,tmp_path,include_completed=True)
    assert (await db.get_call(call_id))['recording_state'] == 'ready'
    path.unlink()
    await reconcile_recordings(db,tmp_path)
    assert (await db.get_call(call_id))['recording_state'] == 'ready'
    await reconcile_recordings(db,tmp_path,include_completed=True)
    assert (await db.get_call(call_id))['recording_state'] == 'failed'


@pytest.mark.parametrize('marker,expected', [('ready','ready'), ('failed','failed')])
async def test_frequent_pass_only_checks_pending(db,tmp_path,monkeypatch,marker,expected):
    completed_states = {}
    for state in ('ready','failed'):
        path = tmp_path/f'{state}.mp3'
        call_id = await store_call(db, '+7',10,'answered',str(path))
        await db.set_recording_state(call_id,state)
        completed_states[call_id] = state
    path = tmp_path/'pending.mp3'
    path.write_bytes(b'ID3valid audio placeholder')
    path.with_name(path.name+'.status.json').write_text(json.dumps({'status':marker}))
    pending_id = await store_call(db, '+7',10,'answered',str(path))
    await store_call(db, '+7',0,'missed')

    assert {row['id'] for row in await db.recording_candidates()} == {pending_id}
    assert {row['id'] for row in await db.recording_candidates(include_completed=True)} == set(completed_states) | {pending_id}

    checked = []

    def check_pending(call, base):
        checked.append(call['id'])
        return recording_state(call, base)

    monkeypatch.setattr(recording,'recording_state',check_pending)
    await reconcile_recordings(db,tmp_path)
    assert checked == [pending_id]
    assert (await db.get_call(pending_id))['recording_state'] == expected
    for call_id,state in completed_states.items():
        assert (await db.get_call(call_id))['recording_state'] == state


async def test_recording_loop_full_pass_at_startup_and_hourly(monkeypatch,tmp_path):
    db = object()
    ticks = iter((0,5,3595,3600,3605))
    reconcile = AsyncMock()
    sleep = AsyncMock(side_effect=[None,None,None,None,asyncio.CancelledError])
    monkeypatch.setattr(recording,'monotonic',lambda: next(ticks))
    monkeypatch.setattr(recording,'reconcile_recordings',reconcile)
    monkeypatch.setattr(recording.asyncio,'sleep',sleep)

    with pytest.raises(asyncio.CancelledError):
        await recording.recording_loop(db,tmp_path)

    assert reconcile.await_args_list == [
        call(db,tmp_path,include_completed=full) for full in (True,False,False,True,False)
    ]
    assert sleep.await_args_list == [call(5)] * 5


def test_empty_and_escaped_recording_never_ready(tmp_path):
    path = tmp_path/'empty.mp3'
    path.touch()
    call = {'recording_path':str(path),'received_at':datetime.now(timezone.utc).isoformat()}
    assert recording_state(call,tmp_path) == 'pending'
    call['recording_path'] = '/etc/passwd'
    assert recording_state(call,tmp_path) == 'failed'


def test_ready_recording_outside_directory_is_failed(tmp_path):
    base = tmp_path / 'recordings'
    base.mkdir()
    path = tmp_path / 'outside.mp3'
    path.write_bytes(b'ID3synthetic audio')
    path.with_name(path.name + '.status.json').write_text('{"status":"ready"}')
    call = {'recording_path': str(path), 'received_at': datetime.now(timezone.utc).isoformat()}

    assert recording_state(call, base) == 'failed'


@pytest.mark.parametrize('marker,expected', [
    (None,'pending'),
    ('{"status":"pending"}','pending'),
    ('{"status":"unknown"}','failed'),
    ('{broken','failed'),
    ('[]','failed'),
    ('{}','failed'),
])
async def test_nonempty_recording_requires_explicit_ready_sidecar(db,tmp_path,marker,expected):
    path = tmp_path/'unfinished.mp3'
    path.write_bytes(b'ID3partially written audio')
    call_id = await store_call(db, '+7',10,'answered',str(path))
    sidecar = path.with_name(path.name+'.status.json')
    if marker is not None:
        sidecar.write_text(marker)
    await reconcile_recordings(db,tmp_path)
    assert (await db.get_call(call_id))['recording_state'] == expected
    sidecar.write_text('{"status":"ready"}')
    await reconcile_recordings(db,tmp_path,include_completed=True)
    assert (await db.get_call(call_id))['recording_state'] == 'ready'


def test_ready_sidecar_with_empty_file_is_failed(tmp_path):
    path = tmp_path/'empty.mp3'
    path.touch()
    path.with_name(path.name+'.status.json').write_text('{"status":"ready"}')
    call = {'recording_path':str(path),'received_at':datetime.now(timezone.utc).isoformat()}
    assert recording_state(call,tmp_path) == 'failed'


# --- Retention (PHONELET_RECORDING_RETENTION_DAYS) ---

OLD = '2026-01-01T00:00:00+00:00'
NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


async def recorded_call(db, path, received=OLD, state='ready'):
    call_id = await store_call(db, '+15550000001', 10, 'answered', str(path))
    await db._conn.execute("UPDATE calls SET received_at=?,recording_state=? WHERE id=?", (received, state, call_id))
    await db._conn.commit()
    return call_id


def recording_set(directory, name='20260101-000000-1.1-+15550000001', mtime=OLD):
    directory.mkdir(parents=True, exist_ok=True)
    mp3 = directory / (name + '.mp3')
    files = [mp3, mp3.with_name(mp3.name + '.status.json'), directory / (name + '.wav')]
    stamp = datetime.fromisoformat(mtime).timestamp()
    for f in files:
        f.write_bytes(b'synthetic')
        os.utime(f, (stamp, stamp))
    return mp3, files


async def test_retention_deletes_old_recordings_and_keeps_new(db, tmp_path):
    base = tmp_path / 'recordings'
    old_mp3, old_files = recording_set(base / '2026/01/01')
    new_mp3, new_files = recording_set(base / '2026/02/28', name='new', mtime='2026-02-28T00:00:00+00:00')
    lock = old_mp3.with_name(old_mp3.name + '.lock')  # the converter's lock stays
    lock.touch()
    old_id = await recorded_call(db, old_mp3)
    failed_id = await recorded_call(db, base / '2026/01/01/gone.mp3', state='failed')  # files already gone
    new_id = await recorded_call(db, new_mp3, received='2026-02-28T00:00:00+00:00')
    await recording.apply_retention(db, base, 30, now=NOW)
    assert not any(f.exists() for f in old_files) and lock.exists()
    assert all(f.exists() for f in new_files)
    assert [(await db.get_call(i))['recording_state'] for i in (old_id, failed_id, new_id)] == [
        'deleted', 'deleted', 'ready']
    # A deleted recording is never a candidate again: reconciliation leaves it as it is.
    assert {row['id'] for row in await db.recording_candidates(include_completed=True)} == {new_id}
    await reconcile_recordings(db, base, include_completed=True)
    assert (await db.get_call(old_id))['recording_state'] == 'deleted'


async def test_retention_off_by_default(db, tmp_path):
    from phonelet.config import Settings
    assert Settings(api_key='x', ami_secret='x').recording_retention_days == 0
    base = tmp_path / 'recordings'
    mp3, files = recording_set(base)
    call_id = await recorded_call(db, mp3)
    await recording.apply_retention(db, base, 0, now=NOW)
    assert all(f.exists() for f in files) and (await db.get_call(call_id))['recording_state'] == 'ready'


async def test_retention_refuses_a_recording_outside_the_directory(db, tmp_path):
    base = tmp_path / 'recordings'
    base.mkdir()
    outside, files = recording_set(tmp_path / 'other')
    escaped = base / '..' / 'other' / outside.name
    ids = [await recorded_call(db, outside), await recorded_call(db, escaped)]
    await recording.apply_retention(db, base, 30, now=NOW)
    assert all(f.exists() for f in files)
    assert [(await db.get_call(i))['recording_state'] for i in ids] == ['ready', 'ready']


@pytest.mark.parametrize('link', ['directory', 'file'])
async def test_retention_follows_no_symlink(db, tmp_path, link):
    base = tmp_path / 'recordings'
    target, files = recording_set(tmp_path / 'elsewhere')
    if link == 'directory':
        base.mkdir()
        (base / 'day').symlink_to(tmp_path / 'elsewhere')
        mp3 = base / 'day' / target.name
    else:
        (base / 'day').mkdir(parents=True)
        mp3 = base / 'day' / target.name
        mp3.symlink_to(target)
    call_id = await recorded_call(db, mp3)
    await recording.apply_retention(db, base, 30, now=NOW)
    assert all(f.exists() for f in files)
    assert (await db.get_call(call_id))['recording_state'] == 'ready'


async def test_retention_keeps_a_recent_file_of_an_old_call(db, tmp_path):
    base = tmp_path / 'recordings'
    mp3, files = recording_set(base, mtime='2026-02-28T00:00:00+00:00')
    call_id = await recorded_call(db, mp3)
    await recording.apply_retention(db, base, 30, now=NOW)
    assert all(f.exists() for f in files)
    assert (await db.get_call(call_id))['recording_state'] == 'ready'


async def test_recording_loop_applies_retention_on_full_passes(monkeypatch, tmp_path):
    db = object()
    ticks = iter((0, 5))
    retention = AsyncMock()
    disk = AsyncMock()
    monkeypatch.setattr(recording, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(recording, 'reconcile_recordings', AsyncMock())
    monkeypatch.setattr(recording, 'apply_retention', retention)
    monkeypatch.setattr(recording.asyncio, 'sleep', AsyncMock(side_effect=[None, asyncio.CancelledError]))
    with pytest.raises(asyncio.CancelledError):
        await recording.recording_loop(db, tmp_path, disk=disk, retention_days=7)
    assert retention.await_args_list == [call(db, tmp_path, 7, refusals_logged=set())]
    assert disk.check.await_count == 2


@pytest.mark.parametrize('missing', ['base', 'date directory'])
async def test_retention_keeps_the_state_when_a_directory_is_missing(db, tmp_path, missing):
    base = tmp_path / 'recordings'
    if missing == 'date directory':
        base.mkdir()
    call_id = await recorded_call(db, base / '2026/01/01/x.mp3')
    await recording.apply_retention(db, base, 30, now=NOW)
    assert (await db.get_call(call_id))['recording_state'] == 'ready'


async def test_retention_logs_a_refusal_once_per_call(db, tmp_path, caplog):
    refusals_logged = {-1}  # A previous candidate must not occupy the set forever.
    call_id = await recorded_call(db, tmp_path / 'outside' / 'x.mp3')
    base = tmp_path / 'recordings'
    base.mkdir()
    for _ in range(2):
        await recording.apply_retention(db, base, 30, now=NOW, refusals_logged=refusals_logged)
    warnings = [r for r in caplog.records if 'Recording retention: kept' in r.getMessage()]
    assert len(warnings) == 1 and 'kept 1 ' in warnings[0].getMessage()
    assert refusals_logged == {call_id}
    assert (await db.get_call(call_id))['recording_state'] == 'ready'
    await db.set_recording_state(call_id, 'deleted')
    await recording.apply_retention(db, base, 30, now=NOW, refusals_logged=refusals_logged)
    assert refusals_logged == set()


async def test_recording_loop_keeps_refusal_state_between_full_passes(db, tmp_path, monkeypatch, caplog):
    await recorded_call(db, tmp_path / 'outside' / 'x.mp3')
    ticks = iter((0, recording.FULL_RECONCILE_INTERVAL_SECONDS))
    monkeypatch.setattr(recording, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(recording, 'reconcile_recordings', AsyncMock())
    monkeypatch.setattr(recording.asyncio, 'sleep', AsyncMock(side_effect=[None, asyncio.CancelledError]))
    with pytest.raises(asyncio.CancelledError):
        await recording.recording_loop(db, tmp_path, retention_days=30)
    warnings = [r for r in caplog.records if 'Recording retention: kept' in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.parametrize('days,valid', [(36500, True), (36501, False), (-1, False)])
def test_retention_days_are_bounded(days, valid):
    from pydantic import ValidationError
    from phonelet.config import Settings
    if valid:
        assert Settings(api_key='x', ami_secret='x', recording_retention_days=days).recording_retention_days == days
    else:
        with pytest.raises(ValidationError):
            Settings(api_key='x', ami_secret='x', recording_retention_days=days)
