"""Replay immutable driver fragments and retained Asterisk CDR logs.

The driver, not AMI, establishes the SMS durable boundary. Files are retained
for recovery; only a confirmed backup/retention policy may remove them.
"""
import asyncio
import csv
import hashlib
import logging
import os
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from phonelet.constants import MAX_SMS_PARTS, NotificationMethod
from phonelet.contracts import MalformedCdr, parse_timestamp

logger = logging.getLogger(__name__)
# Column order of the phonelet.csv mapping in asterisk/cdr_custom.conf.
CDR_FIELDS = ('UniqueID', 'Channel', 'LastApplication', 'Source', 'Disposition',
              'BillableSeconds', 'UserField', 'StartTime')
# Seconds one unreadable SMS journal file (or the spool directory) may hold SMS
# ingestion before a durable alert is queued: once per blocker and UTC day.
SMS_BLOCK_ALERT_AFTER = 900
CDR_BATCH_BYTES = 1024 * 1024  # most bytes read per CDR file and pass
# Floor of the incomplete-CDR-line grace and of the ingress freshness window.
_MIN_WINDOW_SECONDS = 10
# Driver journal records (chan_dongle ingress patch): one line, tab-separated.
MAX_FRAGMENT_RECORD_BYTES = 20000
UNPARSED_RECORD_TAG = 'U1'  # a PDU the driver could not parse
FRAGMENT_RECORD_TAG = '1'  # a decoded SMS part
# Source id (and outbox event_key) of a quarantined PDU, which has no assembly identity.
UNPARSED_SOURCE_PREFIX = 'unparsed:'
# Outbox event_key prefixes of the alert for a record that can never be stored, followed
# by the SHA-256 of its bytes: a replay or restart does not alert again.
INVALID_SMS_RECORD_PREFIX = 'sms-journal-invalid:'
INVALID_CDR_RECORD_PREFIX = 'cdr-invalid:'


def parse_fragment(raw):
    """Parse a journal record, leaving an unusable SCTS as None for receipt-time fallback."""
    if len(raw) > MAX_FRAGMENT_RECORD_BYTES or not raw.endswith(b'\n'):
        raise ValueError('Invalid fragment record')
    fields = raw.decode('ascii').rstrip('\n').split('\t')
    if len(fields) == 5 and fields[0] == UNPARSED_RECORD_TAG:
        device, sim = (bytes.fromhex(v).decode('utf-8') for v in fields[1:3])
        slot, pdu = int(fields[3]), fields[4]
        if not 0 <= slot < 256 or not pdu or any(c not in string.hexdigits for c in pdu):
            raise ValueError('Invalid unparsed PDU record')
        return dict(source_id=UNPARSED_SOURCE_PREFIX + hashlib.sha256(raw).hexdigest(),
                    device=device, sim=sim, slot=slot, pdu=pdu)
    if len(fields) != 11 or fields[0] != FRAGMENT_RECORD_TAG:
        raise ValueError('Unknown fragment format')
    device, sim, timestamp, sender = (bytes.fromhex(v).decode('utf-8') for v in fields[1:5])
    total, part = int(fields[6]), int(fields[7])
    if not 1 <= part <= total <= MAX_SMS_PARTS:
        # Never valid for this record: rejected unidentified, it defers no group.
        raise ValueError('Invalid fragment numbering')
    try:
        timestamp = parse_timestamp(timestamp)
    except (ValueError, OverflowError):
        # Other malformed fields must still reject the record. Only a decoded
        # but unusable SCTS gets the file's receipt time in replay_once.
        timestamp = None
    return dict(source_id='fragment:' + hashlib.sha256(raw).hexdigest(), device=device, sim=sim,
                timestamp=timestamp, sender=sender,
                ref=int(fields[5]), total=total, part=part,
                text=bytes.fromhex(fields[8]).decode('utf-8'), slot=int(fields[9]), pdu=fields[10])


def _is_utf8(fields):
    """False for a CDR row read with surrogateescape from bytes that are not UTF-8."""
    try:
        '\t'.join(fields).encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


@dataclass
class _SmsBlock:
    """The journal path that holds SMS ingestion."""
    path: Path
    since: float  # time.monotonic() when this path started to hold it
    alerted_day: str | None = None  # UTC day of the queued alert


class IngressReplay:
    def __init__(self, db, cdr_handler, sms_dir, cdr_path, reassembly_timeout=60, poll_interval=5):
        self.db, self.cdr_handler = db, cdr_handler
        self.sms_dir, self.cdr_path = Path(sms_dir), Path(cdr_path)
        self.reassembly_timeout = reassembly_timeout
        self.poll_interval = poll_interval
        self._cdr_blocked_since = {}
        self.last_success = None
        self._last_success_mono = None
        self.errors = 0
        self._cdr_offsets = {}
        self._cdr_truncated = set()  # observed truncations whose reset may still need a commit
        self._cdr_errors = set()
        self._seen = set()  # restart deliberately replays from durable sources
        self._initial = None
        self._initial_sms = set()
        self._sms_block = None  # _SmsBlock while a journal path holds SMS ingestion
        # Alert keys of records set aside in this process: invalid, so never retried.
        self.quarantined = set()
        # True until the durable first inventory is complete (or found complete).
        self.first_pass = True
        self.records = 0  # journal records consumed by this process: SMS files and CDR lines

    async def _list_sms_journal(self):
        """Return (unseen .sms files as (stat, path) in arrival order, blocker, its error).

        A blocker (the directory, or a file without a stat) holds every file: the list is then empty.
        """
        # Arrival order, not the driver's random file names: whether a part
        # joins an open assembly depends on the parts ingested before it. A
        # file that cannot be read is an unknown part of an unknown group, so
        # it and every later file wait: a later part must not take its place.
        try:
            # Path.glob would silently skip a directory it cannot list.
            names = await asyncio.to_thread(os.listdir, self.sms_dir)
        except OSError as error:
            return [], self.sms_dir, error
        journal, unlisted = [], []
        for path in (self.sms_dir / name for name in names if name.endswith('.sms')):
            if path in self._seen:
                continue
            try:
                journal.append((path.stat(), path))
            except OSError as error:
                unlisted.append((path, error))
        if unlisted:
            # Without its arrival time no file can be placed after it.
            return [], *min(unlisted, key=lambda entry: entry[0])
        # Finish the retained inventory before newer files, even after a clock
        # reset. Keep arrival order within each of those two sets.
        journal.sort(key=lambda entry: (not self._sms_history(entry[1]),
                                        entry[0].st_mtime_ns, entry[1].name))
        return journal, None, None

    def _cdr_paths(self):
        return [p for p in sorted(self.cdr_path.parent.glob(self.cdr_path.name + '.*')) + [self.cdr_path]
                if p.is_file() and p.suffix not in ('.gz', '.zip')]

    async def _first_inventory(self):
        complete, snapshot = await self.db.ingress_state()
        if complete:
            self._initial = None
            self.first_pass = False
            return
        failure = None
        if snapshot is None:
            # Persist the boundary before consuming anything. Failed records keep this
            # inventory open, but cannot turn subsequent arrivals into silent history.
            snapshot = {"sms": None, "cdr": {}}
            for path in self._cdr_paths():
                stat = path.stat()
                snapshot["cdr"][f"{stat.st_dev}:{stat.st_ino}"] = stat.st_size
            await self.db.save_ingress_snapshot(snapshot)
        if snapshot["sms"] is None:
            try:
                names = await asyncio.to_thread(os.listdir, self.sms_dir)
            except OSError as error:
                failure = error  # _hold_sms reports this blocker, even during rebuild
            else:
                snapshot["sms"] = [name for name in names if name.endswith('.sms')]
                await self.db.save_ingress_snapshot(snapshot)
        self._initial = snapshot
        self._initial_sms = set(snapshot["sms"] or [])
        return failure

    def _sms_history(self, path):
        return self._initial is not None and path.name in self._initial_sms

    def _cdr_history(self, identity, offset):
        return (self._initial is not None and
                offset < self._initial["cdr"].get(f"{identity[0]}:{identity[1]}", 0))

    async def _finish_first_pass(self):
        if self._initial is None or self._initial["sms"] is None:
            return
        if not all(self.sms_dir / name in self._seen for name in self._initial["sms"]):
            return
        for identity, size in self._initial["cdr"].items():
            key = tuple(int(v) for v in identity.split(':'))
            if self._cdr_offsets.get(key, 0) < size:
                return
        await self.db.complete_ingress()  # a failed commit leaves the inventory in force
        self._initial = None
        self.first_pass = False

    async def replay_once(self):
        failure = await self._first_inventory()
        if self._initial is not None and self._initial["sms"] is None:
            # A later listing in this pass must not bypass the durable inventory.
            journal, blocker = [], self.sms_dir
        else:
            journal, blocker, failure = await self._list_sms_journal()
        errors = int(blocker is not None)
        blocked_sms_groups = set()
        for stat, path in journal:
            try:
                raw = await asyncio.to_thread(path.read_bytes)
            except Exception as error:
                errors += 1
                blocker, failure = path, error
                break
            try:
                fragment = parse_fragment(raw)
            except ValueError as error:
                # Never valid on a later pass: an alert instead of a retry that would
                # hold readiness (and with it every deploy) forever.
                message = (f'Invalid SMS journal record {path.name} set aside; it stays in the driver '
                           f'journal, its SMS is not in the history')
                if await self._quarantine(INVALID_SMS_RECORD_PREFIX + hashlib.sha256(raw).hexdigest(),
                                          message, path.name, error, self._sms_history(path)):
                    self._consumed(path)
                else:
                    errors += 1
                continue
            try:
                if fragment['source_id'].startswith(UNPARSED_SOURCE_PREFIX):
                    # A quarantine record has no assembly identity. The outbox
                    # key survives restarts; mark seen only after the enqueue.
                    message = (f"Unparsed SMS on device {fragment['device']}, slot {fragment['slot']}; "
                               f"raw PDU retained in driver journal {path.name}")
                    await self.db.enqueue_notification(NotificationMethod.HEALTH, {'message': message},
                                                       event_key=fragment['source_id'], history=self._sms_history(path))
                    self._consumed(path)
                    continue
                fragment["received_epoch"] = stat.st_mtime
                invalid_scts = fragment['timestamp'] is None
                if invalid_scts:
                    fragment['timestamp'] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                if self.db.fragment_group(fragment) in blocked_sms_groups:
                    # An earlier part of this group failed to commit. It goes
                    # first in the next pass, so later parts keep arrival order.
                    continue
                accepted = await self.db.ingest_fragment(fragment, history=self._sms_history(path))
                if accepted and invalid_scts:
                    logger.warning('Invalid SMS date in journal %s; replaced with receipt time', path.name)
                self._consumed(path)  # only after commit
            except Exception:
                errors += 1
                if not fragment['source_id'].startswith(UNPARSED_SOURCE_PREFIX):
                    blocked_sms_groups.add(self.db.fragment_group(fragment))
                logger.exception('SMS journal record retained for replay')
        await self._hold_sms(blocker, failure)
        # Drain known siblings before partial publication. Only an identified
        # sibling whose commit failed defers its group; unrelated or invalid
        # SMS and CDR records must not suppress already durable SMS indefinitely.
        await self.db.flush_fragments(self.reassembly_timeout, exclude_groups=blocked_sms_groups)
        errors += await self._replay_cdr_logs() + len(self._cdr_errors)
        await self._finish_first_pass()
        self.errors = errors
        if not errors:
            self.last_success = datetime.now(timezone.utc).isoformat()
            self._last_success_mono = time.monotonic()

    def _consumed(self, path):
        self._seen.add(path)
        self.records += 1

    async def _replay_cdr_logs(self):
        """Replay every retained CDR file; returns the number of files that failed."""
        failed = 0
        for path in self._cdr_paths():
            try:
                await self._replay_cdr(path)
            except Exception:
                failed += 1
                logger.exception('CDR journal read/commit failed; will replay')
        return failed

    async def _quarantine(self, key, message, name, error, history):
        """Record a rejected record durably; notify the owner unless it belongs to history.

        The record is then skipped for good. Only the record's name and the kind
        of defect are logged, never its contents.
        """
        try:
            await self.db.enqueue_notification(NotificationMethod.HEALTH, {'message': message}, event_key=key,
                                               history=history)
        except Exception:
            logger.exception('Alert for invalid record %s not queued; will retry', name)
            return False
        if key not in self.quarantined:
            self.quarantined.add(key)
            logger.warning('Invalid record %s set aside (%s); alert recorded', name, type(error).__name__)
        return True

    async def _hold_sms(self, blocker, failure):
        # One traceback per blocker, not one per poll. The alert key carries the
        # UTC day: neither another pass nor a restart that day queues it again,
        # a block lasting into the next day alerts again. A failed enqueue
        # retries next pass.
        now = time.monotonic()
        if blocker is None:
            if self._sms_block is not None:
                logger.info('SMS reception resumed: journal %s no longer holds it after %.0f s',
                            self._sms_block.path.name, now - self._sms_block.since)
                self._sms_block = None
            return
        kind = 'directory' if blocker == self.sms_dir else 'file'
        if self._sms_block is None or self._sms_block.path != blocker:
            self._sms_block = _SmsBlock(blocker, now)
            logger.error('SMS journal %s %s unreadable; SMS parts wait for it', kind, blocker.name,
                         exc_info=failure)
        else:
            logger.debug('SMS reception still held by journal %s %s', kind, blocker.name)
        day = datetime.now(timezone.utc).date().isoformat()
        if self._sms_block.alerted_day == day or now - self._sms_block.since < SMS_BLOCK_ALERT_AFTER:
            return
        message = (f'SMS reception blocked: a driver journal {kind} has been unreadable for '
                   f'{SMS_BLOCK_ALERT_AFTER // 60} min')
        try:
            new = await self.db.enqueue_notification(NotificationMethod.HEALTH, {'message': message},
                                                     event_key=f'sms-journal-blocked:{blocker.name}:{day}')
        except Exception:
            logger.exception('SMS reception alert not queued; will retry')
            return
        self._sms_block.alerted_day = day
        # After a restart the same day the alert is already queued: INFO, not another WARNING.
        if new:
            logger.warning('SMS reception held by unreadable journal %s %s for %.0f s; alert enqueued',
                           kind, blocker.name, now - self._sms_block.since)
        else:
            logger.info('SMS reception held by unreadable journal %s %s for %.0f s; alert already queued',
                        kind, blocker.name, now - self._sms_block.since)

    async def _replay_cdr(self, path):
        # In-memory offsets are an optimization only; restart replays retained
        # logs using DB source IDs. Inodes follow rename rotation. Truncation
        # resets replay and retires any initial history boundary durably before
        # ingesting replacement records. At most CDR_BATCH_BYTES are read per file/pass.
        def read_batch():
            with path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                offset = self._cdr_offsets.get(identity, 0)
                key = f"{stat.st_dev}:{stat.st_ino}"
                boundary = self._initial["cdr"].get(key, 0) if self._initial is not None else 0
                truncated = identity in self._cdr_truncated or stat.st_size < max(offset, boundary)
                if truncated:
                    offset = 0
                stream.seek(offset)
                return identity, offset, stream.read(CDR_BATCH_BYTES), truncated
        identity, offset, raw, truncated = await asyncio.to_thread(read_batch)
        if truncated:
            self._cdr_truncated.add(identity)
            key = f"{identity[0]}:{identity[1]}"
            if self._initial is not None and key in self._initial["cdr"]:
                snapshot = {**self._initial, "cdr": dict(self._initial["cdr"])}
                del snapshot["cdr"][key]
                await self.db.save_ingress_snapshot(snapshot)
                self._initial = snapshot
            self._cdr_offsets[identity] = 0
            self._cdr_errors = {k for k in self._cdr_errors if k[0] != identity}
            self._cdr_blocked_since = {k: v for k, v in self._cdr_blocked_since.items() if k[0] != identity}
            self._cdr_truncated.discard(identity)
        if not raw:
            return
        batch_full = len(raw) == CDR_BATCH_BYTES
        # Newline is required for commit: an append may currently be incomplete.
        complete = raw[:raw.rfind(b'\n') + 1]
        consumed = 0
        exhausted = False
        at_start = skipped = False
        def lines():
            nonlocal consumed, exhausted, at_start, skipped
            for line in complete.splitlines(keepends=True):
                consumed += len(line)
                if at_start:
                    # A torn write leaves a zero-filled block: NULs in front of a
                    # record are not part of it, a line of only NULs is skipped.
                    at_start = False
                    record = line.lstrip(b'\0')
                    if len(record) < len(line) and not record.strip(b'\0 \t\r\n'):
                        skipped, record = True, b'\n'
                    line = record
                # Invalid UTF-8 survives as lone surrogates and makes the record invalid below.
                yield line.decode('utf-8', 'surrogateescape')
            exhausted = True
        reader = csv.reader(lines(), strict=True)
        committed = 0
        while True:
            key = (identity, offset + committed)
            at_start, skipped = True, False
            try:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error:
                    if exhausted:
                        # A quoted trailing record spans the current append/batch.
                        self._mark_cdr_blocked(path, identity, offset + committed,
                                               oversized=batch_full and committed == 0)
                        break
                    raise MalformedCdr('CDR line has invalid CSV syntax') from None
                if not skipped:
                    if len(row) != len(CDR_FIELDS) or not _is_utf8(row):
                        raise MalformedCdr('CDR line has another field count or is not UTF-8')
                    if any('\0' in field for field in row):
                        raise MalformedCdr('CDR line contains a NUL byte')
                    await self.cdr_handler.handle_cdr(dict(zip(CDR_FIELDS, row)),
                                                      history=self._cdr_history(identity, offset + committed))
            except MalformedCdr as error:
                # Never valid on a later pass, and later calls of the file are behind it.
                record = complete[committed:consumed]
                message = (f'Invalid CDR line in {path.name} set aside; a call or SMS failure report '
                           f'may be missing from the history')
                if not await self._quarantine(INVALID_CDR_RECORD_PREFIX + hashlib.sha256(record).hexdigest(),
                                              message, path.name, error,
                                              self._cdr_history(identity, offset + committed)):
                    self._cdr_errors.add(key)  # retried from this line on the next pass
                    return
            self._cdr_blocked_since.pop(key, None)
            self._cdr_errors.discard(key)
            committed = consumed
            self._cdr_offsets[identity] = offset + committed
            self.records += not skipped
        if len(raw) > len(complete):
            self._mark_cdr_blocked(path, identity, offset + committed, oversized=batch_full and committed == 0)

    def _mark_cdr_blocked(self, path, identity, offset, oversized):
        key = (identity, offset)
        since = self._cdr_blocked_since.setdefault(key, time.monotonic())
        if oversized or time.monotonic() - since >= self._cdr_partial_grace:
            if key not in self._cdr_errors:
                logger.error('CDR journal %s blocked at byte offset %d; record incomplete or oversized',
                             path.name, offset)
            self._cdr_errors.add(key)

    @property
    def _cdr_partial_grace(self):
        # An append in progress gets two polls (at least the floor) before it counts as an error.
        return max(_MIN_WINDOW_SECONDS, 2 * self.poll_interval)

    @property
    def _fresh_window(self):
        # Ingress is fresh while its last clean pass is within three polls (at least the floor).
        return max(_MIN_WINDOW_SECONDS, 3 * self.poll_interval)

    @property
    def status(self):
        return {'sms_journal_available': self.sms_dir.is_dir(),
                'cdr_journal_available': self.cdr_path.is_file(),
                'ingress_errors': self.errors, 'ingress_quarantined': len(self.quarantined),
                'ingress_updated': self.last_success,
                'ingress_first_pass': self.first_pass, 'ingress_records': self.records,
                'ingress_fresh': self._last_success_mono is not None and
                    time.monotonic() - self._last_success_mono < self._fresh_window}

    async def run(self):
        while True:
            try:
                await self.replay_once()
            except Exception:
                self.errors += 1
                logger.exception('Ingress unavailable; source records retained')
            await asyncio.sleep(self.poll_interval)
