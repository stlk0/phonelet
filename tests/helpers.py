"""Small builders and helpers shared by several test modules."""
import asyncio
import csv
import importlib.util
import io
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from phonelet.constants import NotificationMethod

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if name == 'deploy-release':
        # Fixtures keep recordings under the test temp dir (often /tmp); test_asterisk_config checks the list.
        temp = os.path.realpath(tempfile.gettempdir())
        module.ASTERISK_UNWRITABLE = tuple(top for top in module.ASTERISK_UNWRITABLE
                                           if not (temp + '/').startswith(os.path.realpath(top) + '/'))
    return module


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\nset -eu\n' + body)
    path.chmod(0o755)


class DeliversByMethod:
    """deliver() of a fake backend: its notify_<method>(**payload), as BaseNotifier.deliver dispatches."""

    async def deliver(self, method, payload, checkpoint):
        return await getattr(self, method)(**payload)


async def deliver_sms(notifier, sender, text):
    """deliver() of one SMS job, as the outbox calls it, with a checkpoint that stores nothing."""
    async def checkpoint(payload):
        pass
    return await notifier.deliver(NotificationMethod.SMS, {'sender': sender, 'text': text}, checkpoint)


def cdr_line(uid, stamp='2026-09-25T12:00:00+00:00', source='+7', recording=''):
    """One answered incoming call in the phonelet.csv column layout of asterisk/cdr_custom.conf."""
    output = io.StringIO()
    csv.writer(output).writerow([uid, 'Dongle/dongle0-1', 'Dial', source, 'ANSWERED', '4', recording, stamp])
    return output.getvalue()


def local_paths(root):
    """All path settings under a temp directory; the defaults are the real host paths."""
    for name in ('recordings', 'sms', 'cdr', 'netplan'):
        (root / name).mkdir(parents=True)
    return dict(db_path=str(root / 'phonelet.db'), recordings_dir=str(root / 'recordings'),
                sms_ingress_dir=str(root / 'sms'), cdr_log_path=str(root / 'cdr' / 'phonelet.csv'),
                wifi_netplan_file=str(root / 'netplan' / '30-wifis-dhcp.yaml'), wifi_state_dir=str(root / 'wifi'))


def journal_record(text, part=1, total=1, ref=7, slot=1, stamp='2026-09-25 12:00:00 +00:00', sender='+15550000001'):
    """One driver journal line (the format ingress.parse_fragment reads)."""
    fields = ['1', *[s.encode().hex() for s in ('dongle0', 'sim1', stamp, sender)],
              str(ref), str(total), str(part), text.encode().hex(), str(slot), 'AABB%d%d' % (part, slot)]
    return ('\t'.join(fields) + '\n').encode()


async def store_sms(db, sender, text, source_id=None, timestamp=None):
    """Store one single-part incoming SMS through Database.ingest_fragment, as reception does; its sms id.

    A repeated source_id is a replayed journal record: nothing new is stored.
    """
    source_id = source_id or 'fragment:' + uuid.uuid4().hex
    await db.ingest_fragment(dict(source_id=source_id, device='dongle0', sim='sim1', sender=sender, ref=0,
                                  total=1, part=1, text=text, received_epoch=time.time(),
                                  timestamp=timestamp or datetime.now(timezone.utc).isoformat()))
    return (await db._one('SELECT sms_id FROM assemblies WHERE id=?', (source_id,)))['sms_id']


async def store_call(db, caller, duration, status, recording_path=None, history=False, note=None):
    """Store one call as the CDR replay does, with a fresh source id and the current time; its id."""
    return await db.insert_call(caller, duration, status, recording_path, source_id='cdr:' + uuid.uuid4().hex,
                                timestamp=datetime.now(timezone.utc).isoformat(), history=history, note=note)


async def command_rows(db):
    return await db._all('SELECT * FROM sms_commands ORDER BY id')


async def _health_jobs(db):
    return await db._all("SELECT event_key,payload FROM outbox WHERE method='notify_health' ORDER BY id")


async def health_by_event(db):
    """{event_key: message} of the queued HEALTH notifications."""
    return {job['event_key']: json.loads(job['payload'])['message'] for job in await _health_jobs(db)}


async def health_messages(db):
    """The queued HEALTH messages, oldest first, one per outbox job."""
    return [json.loads(job['payload'])['message'] for job in await _health_jobs(db)]


class StopLoop(Exception):
    """Raised by the patched sleep to end command_loop."""


def stop_after(monkeypatch, passes, between=None):
    """End command_loop at the sleep after pass `passes`; `between(n)` runs after each earlier pass n.

    Patches asyncio.sleep itself, for the whole test: the worker has no sleep of its own.
    """
    count = 0

    async def sleep(seconds):
        nonlocal count
        count += 1
        if count >= passes:
            raise StopLoop
        if between:
            between(count)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
