import json
from collections import namedtuple

import pytest

from phonelet import main
from phonelet.config import Settings
from phonelet.db import Database
from phonelet.disk_space import MIB, DiskSpaceMonitor
from tests.helpers import local_paths
from tests.test_lifespan import isolated  # noqa: F401 (fixture)

Usage = namedtuple('Usage', 'total used free')
MINIMUM = 100 * MIB


class FakeDisk:
    """free bytes per directory: a synthetic shutil.disk_usage."""
    def __init__(self, free):
        self.free = free
        self.calls = []

    def __call__(self, directory):
        self.calls.append(directory)
        return Usage(0, 0, self.free)


async def open_db(path):
    database = Database(str(path), backends=['t'])
    await database.init()
    return database


async def alerts(database):
    rows = await database._all("SELECT method,payload FROM outbox ORDER BY id")
    return [(row['method'], json.loads(row['payload'])['message']) for row in rows]


async def test_low_then_restored_once_each_with_hysteresis(tmp_path):
    database = await open_db(tmp_path / 'a.db')
    disk = FakeDisk(500 * MIB)
    monitor = DiskSpaceMonitor(database, [tmp_path], MINIMUM, disk_usage=disk)
    try:
        await monitor.check()
        assert await alerts(database) == []  # space was never announced as low: no "restored"
        disk.free = 40 * MIB
        await monitor.check()
        await monitor.check()
        assert await alerts(database) == [
            ('notify_health', f'Low disk space: 40 MiB free on {tmp_path}, minimum 100 MiB')]
        assert monitor.low is True
        disk.free = 105 * MIB  # above the minimum, below the 10 % margin: still low
        await monitor.check()
        assert len(await alerts(database)) == 1 and monitor.low is True
        disk.free = 111 * MIB
        await monitor.check()
        await monitor.check()
        assert (await alerts(database))[1:] == [('notify_health', f'Disk space restored: 111 MiB free on {tmp_path}')]
        assert monitor.low is False
    finally:
        await database.close()


async def test_restart_neither_repeats_low_nor_loses_restored(tmp_path):
    path = tmp_path / 'a.db'
    database = await open_db(path)
    disk = FakeDisk(10 * MIB)
    await DiskSpaceMonitor(database, [tmp_path], MINIMUM, disk_usage=disk).check()
    await database.close()

    database = await open_db(path)
    try:
        await DiskSpaceMonitor(database, [tmp_path], MINIMUM, disk_usage=disk).check()
        assert len(await alerts(database)) == 1  # still low after restart: no second alert
    finally:
        await database.close()

    disk.free = 200 * MIB  # space came back while the service was down
    database = await open_db(path)
    try:
        monitor = DiskSpaceMonitor(database, [tmp_path], MINIMUM, disk_usage=disk)
        await monitor.check()
        assert [message for _, message in await alerts(database)][1].startswith('Disk space restored')
        assert await database.disk_low() is False
    finally:
        await database.close()


async def test_failed_enqueue_keeps_the_state_for_a_retry(tmp_path):
    database = await open_db(tmp_path / 'a.db')
    monitor = DiskSpaceMonitor(database, [tmp_path], MINIMUM, disk_usage=FakeDisk(MIB))
    enqueue = database.enqueue_disk_alert

    async def failing(*args):
        raise OSError('disk full')
    database.enqueue_disk_alert = failing
    try:
        with pytest.raises(OSError):
            await monitor.check()
        assert monitor.low is False
        database.enqueue_disk_alert = enqueue
        await monitor.check()
        assert len(await alerts(database)) == 1 and monitor.low is True
    finally:
        await database.close()


async def test_one_check_per_filesystem_and_missing_directories_skipped(tmp_path):
    database = await open_db(tmp_path / 'a.db')
    (tmp_path / 'x').mkdir()
    disk = FakeDisk(500 * MIB)
    monitor = DiskSpaceMonitor(database, [tmp_path, tmp_path / 'x', tmp_path / 'missing'], MINIMUM, disk_usage=disk)
    try:
        await monitor.check()
        assert disk.calls == [str(tmp_path)]  # same st_dev
    finally:
        await database.close()


@pytest.mark.parametrize('record_calls,low,status,recording,noted', [
    (True, True, 'answered', None, True),
    (True, False, 'answered', None, False),
    (False, True, 'answered', None, False),
    (True, True, 'missed', None, False),
    (True, True, 'answered', '/synthetic/rec.mp3', False),
])
async def test_call_notification_notes_a_recording_skipped_for_low_space(tmp_path, isolated, record_calls, low,
                                                                         status, recording, noted):
    config = Settings(**local_paths(tmp_path), api_key='fake', ami_secret='fake', telegram_token='1:fake',
                      telegram_chat_id='1', record_calls=record_calls)
    runtime = main.Runtime(config)
    await runtime.db.init()
    try:
        runtime.disk.low = low
        await runtime.on_call_end(source_id='cdr:1', timestamp='2026-11-01T00:00:00+00:00', caller='+15550000001',
                                  status=status, duration=12, recording_path=recording)
        payloads = [json.loads(row['payload']) for row in await runtime.db._all("SELECT payload FROM outbox")]
        assert payloads == [{'caller': '+15550000001', 'status': status, 'duration': 12,
                             **({'note': 'not recorded: low disk space'} if noted else {})}]
    finally:
        await runtime.db.close()


async def test_telegram_and_home_assistant_show_the_note():
    from unittest.mock import AsyncMock
    from phonelet.notifiers.homeassistant import HomeAssistantNotifier
    from phonelet.notifiers.telegram import TelegramNotifier

    telegram = TelegramNotifier('1:fake', '1')
    telegram._send = AsyncMock(return_value=True)
    await telegram.notify_call('+15550000001', 'answered', 65, note='not recorded: low disk space')
    assert telegram._send.await_args.args[0].endswith(' (not recorded: low disk space)')
    hass = HomeAssistantNotifier('http://192.0.2.10', 'fake')
    hass._fire_event = AsyncMock(return_value=True)
    await hass.notify_call('+15550000001', 'answered', 65, note='not recorded: low disk space')
    assert hass._fire_event.await_args.args[1]['note'] == 'not recorded: low disk space'
    await hass.notify_call('+15550000001', 'answered', 65)
    assert 'note' not in hass._fire_event.await_args.args[1]
