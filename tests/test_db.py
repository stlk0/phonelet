import json
import sqlite3

import pytest

from phonelet.db import Database
from phonelet.constants import NotificationMethod
from tests.helpers import store_call, store_sms

STAMP = "2026-09-25T12:00:00+00:00"


def _query(db, sql):
    # Read the temporary database file directly, not through Database helpers.
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def test_init_creates_tables(db):
    tables = {name for (name,) in _query(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sms" in tables
    assert "calls" in tables


async def test_backend_subscriptions_default_to_all_and_allow_an_empty_set(tmp_db_path):
    db = Database(tmp_db_path, backends=('all', 'sms-only', 'none'),
                  backend_methods={'sms-only': {NotificationMethod.SMS}, 'none': set()})
    await db.init()
    try:
        for method in NotificationMethod:
            await db.enqueue_notification(method, {})
        jobs = await db._all('SELECT backend, method FROM outbox ORDER BY id')
        assert {j['method'] for j in jobs if j['backend'] == 'all'} == set(NotificationMethod)
        assert [j['method'] for j in jobs if j['backend'] == 'sms-only'] == [NotificationMethod.SMS]
        assert not any(j['backend'] == 'none' for j in jobs)
    finally:
        await db.close()


async def test_insert_call(db):
    call_id = await store_call(db,
        caller="+15551234567",
        duration=150,
        status="answered",
        recording_path="/var/lib/phonelet/recordings/2026/03/21/test.mp3",
    )
    assert call_id == 1

    call = await db.get_call(call_id)
    assert call["caller"] == "+15551234567"
    assert call["duration"] == 150
    assert call["status"] == "answered"
    assert call["recording_path"].endswith("test.mp3")


async def test_insert_missed_call(db):
    call_id = await store_call(db,
        caller="anonymous",
        duration=0,
        status="missed",
        recording_path=None,
    )
    call = await db.get_call(call_id)
    assert call["caller"] == "anonymous"
    assert call["status"] == "missed"
    assert call["recording_path"] is None


@pytest.mark.parametrize("status,duration", [("rejected", 0), ("missed", 0), ("answered", 150), ("unknown", 0)])
@pytest.mark.parametrize("history", [False, True])
async def test_call_history_and_outbox_by_status(db, status, duration, history):
    db.backends = ("telegram", "homeassistant", "other")
    db.backend_methods = {backend: {NotificationMethod.CALL} for backend in db.backends}
    call_id = await db.insert_call("+15551234567", duration, status, source_id="cdr:test-call", timestamp=STAMP,
                                  history=history)
    assert await db.insert_call("+15551234567", duration, status, source_id="cdr:test-call", timestamp=STAMP) == call_id

    call = await db.get_call(call_id)
    assert call["status"] == status
    assert call["source_id"] == "cdr:test-call"
    assert await db.list_calls() == [call]
    jobs = await db._all("SELECT event_key, backend, method, payload FROM outbox ORDER BY id")
    if history or status == "rejected":
        assert jobs == []
    else:
        assert [(job["event_key"], job["backend"], job["method"]) for job in jobs] == [
            (f"calls:{call_id}", backend, NotificationMethod.CALL) for backend in db.backends
        ]
        assert all(json.loads(job["payload"]) == {
            "caller": "+15551234567", "status": status, "duration": duration,
        } for job in jobs)


async def test_list_sms_pagination(db):
    for i in range(15):
        await store_sms(db, sender=f"+1555{i:07d}", text=f"msg {i}")

    page1 = await db.list_sms(limit=10, offset=0)
    assert len(page1) == 10

    page2 = await db.list_sms(limit=10, offset=10)
    assert len(page2) == 5


async def test_list_calls_pagination(db):
    for i in range(5):
        await store_call(db, caller=f"+1555{i:07d}", duration=i * 10, status="answered")

    calls = await db.list_calls(limit=10, offset=0)
    assert len(calls) == 5


async def test_get_stats(db):
    await store_sms(db, sender="+15551234567", text="test")
    await store_call(db, caller="+15551234567", duration=60, status="answered")
    await store_call(db, caller="+15552222222", duration=0, status="missed")

    stats = await db.get_stats()
    assert stats["total_sms"] == 1
    assert stats["total_calls"] == 2
    assert stats["missed_calls"] == 1


async def test_wal_mode_enabled(db):
    assert _query(db, "PRAGMA journal_mode") == [("wal",)]
