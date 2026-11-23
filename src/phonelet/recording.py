import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from phonelet.constants import RecordingState

logger = logging.getLogger(__name__)

PENDING_TIMEOUT_SECONDS = 3600  # not ready or failed after this long: the conversion is considered lost
RECONCILE_INTERVAL_SECONDS = 5
FULL_RECONCILE_INTERVAL_SECONDS = 3600
_MARKER_SUFFIX = '.status.json'  # written next to the MP3 by scripts/convert-recording.py
_MARKER_STATES = (RecordingState.PENDING, RecordingState.READY, RecordingState.FAILED)


def _marker_state(marker):
    """The converter's status; None without a marker, FAILED for an unreadable one."""
    if not marker.is_file():
        return None
    try:
        metadata = json.loads(marker.read_text())
    except (ValueError, OSError):
        return RecordingState.FAILED
    state = metadata.get('status') if isinstance(metadata, dict) else None
    return RecordingState(state) if state in _MARKER_STATES else RecordingState.FAILED


def _received(call):
    return datetime.fromisoformat(call['received_at'])


def recording_state(call, base):
    path = Path(call['recording_path']).resolve()
    if not path.is_relative_to(Path(base).resolve()) or path.suffix != '.mp3':
        return RecordingState.FAILED
    state = _marker_state(path.with_name(path.name + _MARKER_SUFFIX))
    if state == RecordingState.READY:
        # A nonempty MP3 is not completion evidence without the ready marker.
        return RecordingState.READY if path.is_file() and path.stat().st_size > 0 else RecordingState.FAILED
    if state == RecordingState.FAILED:
        return RecordingState.FAILED
    age = (datetime.now(timezone.utc) - _received(call)).total_seconds()
    return RecordingState.FAILED if age > PENDING_TIMEOUT_SECONDS else RecordingState.PENDING


async def reconcile_recordings(db, base, *, include_completed=False):
    for call in await db.recording_candidates(include_completed=include_completed):
        try:
            state = await asyncio.to_thread(recording_state, call, base)
            if state != call['recording_state']:
                await db.set_recording_state(call['id'], state)
        except OSError:
            logger.warning('Recording reconciliation deferred: call=%s', call['id'], exc_info=True)


def recording_files(recording_path, base):
    """The files of one recording to delete: the MP3, its marker and a leftover WAV.

    None refuses the recording: not an MP3 beneath ``base``, a symlink on the way or among the files,
    or a missing directory (an unmounted disk is not proof that the files are gone). Only files that
    exist are returned. The converter's ``.lock`` stays: a converter may still hold it.
    """
    base = Path(os.path.abspath(base))
    path = Path(os.path.abspath(recording_path))  # lexical: a ".." cannot climb out unnoticed
    if path.suffix != '.mp3' or path == base or not path.is_relative_to(base):
        return None
    directory = base.resolve()
    if not directory.is_dir():
        return None
    for part in path.parent.relative_to(base).parts:
        directory = directory / part
        if directory.is_symlink():
            return None
        if not directory.is_dir():
            return None
    names = (path.name, path.name + _MARKER_SUFFIX, path.stem + '.wav')
    files = [directory / name for name in names]
    if any(f.is_symlink() or (f.exists() and not f.is_file()) for f in files):
        return None
    return [f for f in files if f.exists()]


def _delete_recording(call, base, cutoff):
    """True once the files are gone; False when refused (outside, symlink, or a file newer than cutoff)."""
    files = recording_files(call['recording_path'], base)
    if files is None or any(f.stat().st_mtime > cutoff.timestamp() for f in files):
        return False
    for f in files:
        f.unlink()
    return True


async def apply_retention(db, base, days, now=None, *, refusals_logged=None):
    """Delete recordings older than ``days``; 0 keeps them all.

    Repeated passes share ``refusals_logged``, pruned to the current candidates.
    """
    if refusals_logged is None:
        refusals_logged = set()
    if days <= 0:
        return
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    deleted = refused = 0
    candidates = await db.recording_candidates(include_completed=True)
    refusals_logged.intersection_update(call['id'] for call in candidates)
    for call in candidates:
        if _received(call) > cutoff:
            continue
        try:
            done = await asyncio.to_thread(_delete_recording, call, base, cutoff)
        except OSError:
            logger.warning('Recording retention deferred: call=%s', call['id'], exc_info=True)
            continue
        if done:
            refusals_logged.discard(call['id'])
            await db.set_recording_state(call['id'], RecordingState.DELETED)
            deleted += 1
        elif call['id'] not in refusals_logged:
            refusals_logged.add(call['id'])
            refused += 1
    if deleted:
        logger.info('Recording retention: deleted %d recordings older than %d days', deleted, days)
    if refused:
        logger.warning('Recording retention: kept %d expired recordings outside the recordings dir, '
                       'behind a symlink, in a missing directory or with a recent file', refused)


async def recording_loop(db, base, *, disk=None, retention_days=0):
    """Reconcile recordings; also check free space (``disk``: DiskSpaceMonitor) and apply retention."""
    next_full_reconcile = None
    refusals_logged = set()
    while True:
        try:
            now = monotonic()
            include_completed = next_full_reconcile is None or now >= next_full_reconcile
            await reconcile_recordings(db, base, include_completed=include_completed)
            if include_completed:
                next_full_reconcile = now + FULL_RECONCILE_INTERVAL_SECONDS
                await apply_retention(db, base, retention_days, refusals_logged=refusals_logged)
        except Exception:
            logger.exception('Recording reconciliation unavailable; will retry')
        if disk is not None:
            try:
                await disk.check()
            except Exception:
                logger.exception('Disk space check unavailable; will retry')
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
