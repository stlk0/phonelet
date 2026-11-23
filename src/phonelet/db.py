"""SQLite history, durable fragment assembly, the command SMS queue and the per-destination delivery queue."""
import asyncio
import functools
import json
import time
import uuid
from collections.abc import Collection, Mapping
from datetime import datetime, timezone
from enum import StrEnum

import aiosqlite

from phonelet.constants import CALL_MISSED, CALL_REJECTED, MAX_SMS_PARTS, NotificationMethod, RecordingState
from phonelet.sms_commands import envelope

# Alert methods delivered in order per backend: claim_delivery holds a later
# alert while an earlier one on that backend is undelivered. SMS and call
# jobs are not ordered.
_ORDERED_ALERT_METHODS = (
    NotificationMethod.NETWORK_LOST, NotificationMethod.NETWORK_RESTORED,
    NotificationMethod.MODEM_UNAVAILABLE, NotificationMethod.HEALTH,
)
# Payload keys of notify_sms and notify_call jobs (the notifiers' keyword arguments).
_SMS_PAYLOAD_FIELDS = ("sender", "text")
_CALL_PAYLOAD_FIELDS = ("caller", "status", "duration")
# Durable inventory of the first replay, and its completion marker (independent of clocks).
INGRESS_SNAPSHOT_KEY = "ingress_snapshot"
INGRESS_COMPLETE_KEY = "ingress_complete"
DELIVERY_LEASE_SECONDS = 120
MAX_RETRY_DELAY_SECONDS = 3600  # exponential backoff stops growing at one hour
# Seconds between the receipt of an assembly's first part and of a later part
# that may still join it. SMSCs retry a failed part for tens of minutes.
MULTIPART_JOIN_WINDOW = 24 * 3600
# History text of a command SMS (phonelet.sms_commands): its ciphertext stays out of
# history and notifications.
SMS_COMMAND_TEXT = "[command]"
SMS_COMMAND_INCOMPLETE_TEXT = "[command, incomplete]"
# Metadata row: the stamp of the last command taken for running, whatever its kind;
# older or equal ones are replays.
SMS_COMMAND_LAST_STAMP_KEY = "sms_command_last_stamp"
# Metadata row: the modem state the monitor last announced with an alert (JSON
# [availability, registered]), so a restart neither repeats nor loses a transition.
MODEM_ALERT_STATE_KEY = "modem_alert_state"
# Metadata row: "1" while a low disk space alert is the last one announced (phonelet.disk_space).
DISK_LOW_KEY = "disk_low"


class SmsCommandStatus(StrEnum):
    """sms_commands.status."""
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


def transaction(fn):
    @functools.wraps(fn)
    async def wrapped(self, *args, **kwargs):
        async with self._lock:
            try:
                # Inside the try: a cancellation (a wait_for timeout) while BEGIN is in flight still
                # runs it in the SQLite thread, and the rollback below then ends that transaction.
                await self._conn.execute("BEGIN IMMEDIATE")
                result = await fn(self, *args, **kwargs)
                await self._conn.commit()
                return result
            except BaseException:
                await self._conn.rollback()
                raise
    return wrapped


def read_locked(fn):
    @functools.wraps(fn)
    async def wrapped(self, *args, **kwargs):
        # One aiosqlite connection otherwise exposes another coroutine's
        # uncommitted writes to history readers on that same connection.
        async with self._lock:
            return await fn(self, *args, **kwargs)
    return wrapped


def _history_event_key(table, row_id):
    return f"{table}:{row_id}"


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


class Database:
    # The only schema: a database is created empty by this version or refused (no migrations).
    SCHEMA_VERSION = 6
    _SCHEMA = (
        """CREATE TABLE sms (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        received_at TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL,
        partial BOOLEAN NOT NULL DEFAULT 0, source_id TEXT)""",
        "CREATE UNIQUE INDEX sms_source ON sms(source_id)",
        f"""CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        received_at TEXT NOT NULL, caller TEXT NOT NULL, duration INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL, recording_path TEXT,
        recording_state TEXT NOT NULL DEFAULT '{RecordingState.NONE}', source_id TEXT)""",
        "CREATE UNIQUE INDEX calls_source ON calls(source_id)",
        """CREATE TABLE outbox (id INTEGER PRIMARY KEY, event_key TEXT NOT NULL,
        backend TEXT NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL, owner TEXT,
        UNIQUE(event_key, backend))""",
        "CREATE INDEX outbox_due ON outbox(status,next_attempt,lease_until)",
        # Serves claim_delivery's search for an earlier undelivered alert on the same backend.
        "CREATE INDEX outbox_backend_pending ON outbox(backend,id) WHERE status!='delivered'",
        "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        # Every encountered alert stays deduplicated, including suppressed or unaccepted alerts.
        "CREATE TABLE alert_events (event_key TEXT PRIMARY KEY)",
        """CREATE TABLE assemblies (id TEXT PRIMARY KEY, group_key TEXT NOT NULL,
        sender TEXT NOT NULL, total INTEGER NOT NULL, started REAL NOT NULL,
        timestamp TEXT NOT NULL, sms_id INTEGER, ambiguous INTEGER NOT NULL DEFAULT 0,
        history INTEGER NOT NULL)""",
        """CREATE TABLE fragments (source_id TEXT PRIMARY KEY, assembly_id TEXT NOT NULL,
        part INTEGER NOT NULL, text TEXT NOT NULL, UNIQUE(assembly_id,part))""",
        # Command SMS (phonelet.sms_commands): pending -> running -> done; done drops the
        # ciphertext. `name`: the Command it ran or was rejected as (Command.name), NULL if none.
        f"""CREATE TABLE sms_commands (id INTEGER PRIMARY KEY,
        sms_id INTEGER UNIQUE NOT NULL, ciphertext TEXT, name TEXT,
        status TEXT NOT NULL DEFAULT '{SmsCommandStatus.PENDING}', result TEXT)""",
    )

    def __init__(self, db_path: str, backends=(), *,
                 backend_methods: Mapping[str, Collection[NotificationMethod]] | None = None,
                 sms_commands_enabled: bool = False):
        self.db_path = db_path
        self.backends = tuple(backends)
        # Missing declarations accept every method; an empty set accepts none.
        self.backend_methods = dict(backend_methods or {})
        self.sms_commands_enabled = sms_commands_enabled
        self._conn = None
        self._lock = asyncio.Lock()

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        try:
            await self._conn.execute("PRAGMA busy_timeout=5000")
            # Checked before WAL mode is set: a refused database is left as it was.
            empty = await self._check_schema()
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA synchronous=FULL")
            if empty:
                await self._create_schema()
        except BaseException:
            await self.close()
            raise

    async def _check_schema(self):
        """True for an empty database; raise for one that is not this schema (no migrations)."""
        version = (await self._one("PRAGMA user_version"))["user_version"]
        has_objects = version == 0 and bool((await self._one("SELECT COUNT(*) AS n FROM sqlite_master"))["n"])
        return self.check_schema_version(self.db_path, version, has_objects)

    @classmethod
    def check_schema_version(cls, db_path, version, has_objects):
        """Share the startup schema rule with read-only deployment preflight."""
        if version == cls.SCHEMA_VERSION:
            return False
        if version != 0 or has_objects:
            raise RuntimeError(
                f"Database {db_path} has schema version {version}, this release needs "
                f"{cls.SCHEMA_VERSION}: move it aside and start on an empty database "
                f"(docs/operations.md, Recreating the database)")
        return True

    @transaction
    async def _create_schema(self):
        for sql in self._SCHEMA:
            await self._conn.execute(sql)
        await self._conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    @read_locked
    async def ingress_state(self):
        rows = await self._all("SELECT key,value FROM metadata WHERE key IN (?,?)",
                               (INGRESS_SNAPSHOT_KEY, INGRESS_COMPLETE_KEY))
        state = {r["key"]: r["value"] for r in rows}
        return INGRESS_COMPLETE_KEY in state, json.loads(state.get(INGRESS_SNAPSHOT_KEY, "null"))

    @transaction
    async def save_ingress_snapshot(self, snapshot):
        await self._set_metadata(INGRESS_SNAPSHOT_KEY, json.dumps(snapshot))

    @transaction
    async def complete_ingress(self):
        await self._set_metadata(INGRESS_COMPLETE_KEY, "1")

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _one(self, sql, args=()):
        async with self._conn.execute(sql, args) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _all(self, sql, args=()):
        async with self._conn.execute(sql, args) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def _enqueue(self, event_key, method, payload):
        for backend in self.backends:
            methods = self.backend_methods.get(backend)
            if methods is not None and method not in methods:
                continue
            await self._conn.execute(
                "INSERT OR IGNORE INTO outbox(event_key,backend,method,payload) VALUES (?,?,?,?)",
                (event_key, backend, method, json.dumps(payload)),
            )

    async def _set_metadata(self, key, value):
        await self._conn.execute("""INSERT INTO metadata(key,value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))

    @transaction
    async def enqueue_monitor_alert(self, method, payload, state=None):
        """A modem monitor alert and, for a state change, the announced state, in one transaction."""
        await self._enqueue("health:" + uuid.uuid4().hex, method, payload)
        if state is not None:
            await self._set_metadata(MODEM_ALERT_STATE_KEY, json.dumps(list(state)))

    @read_locked
    async def monitor_alert_state(self):
        """The state stored by enqueue_monitor_alert as a tuple, or None."""
        row = await self._one("SELECT value FROM metadata WHERE key=?", (MODEM_ALERT_STATE_KEY,))
        return tuple(json.loads(row["value"])) if row else None

    @transaction
    async def enqueue_disk_alert(self, message, low):
        """A disk space HEALTH alert and the announced state, in one transaction."""
        await self._enqueue("health:" + uuid.uuid4().hex, NotificationMethod.HEALTH, {"message": message})
        await self._set_metadata(DISK_LOW_KEY, "1" if low else "0")

    @read_locked
    async def disk_low(self):
        """Whether the last announced disk space alert was a low one."""
        row = await self._one("SELECT value FROM metadata WHERE key=?", (DISK_LOW_KEY,))
        return bool(row) and row["value"] == "1"

    @transaction
    async def enqueue_notification(self, method, payload, event_key=None, history=False):
        # True on the first encounter, whether queued, suppressed or accepted by no backend.
        # False for an encountered key, even if the configured backends have changed.
        # Only the first replay inventory is history; wall clocks never decide.
        event_key = event_key or "health:" + uuid.uuid4().hex
        cur = await self._conn.execute("INSERT OR IGNORE INTO alert_events(event_key) VALUES (?)", (event_key,))
        if not cur.rowcount:
            return False
        if not history:
            await self._enqueue(event_key, method, payload)
        return True

    async def _insert_sms(self, sender, text, partial, source_id, timestamp, received, history):
        """`received`: epoch seconds of the driver journal record."""
        old = await self._one("SELECT id FROM sms WHERE source_id=?", (source_id,))
        if old:
            return old["id"]
        received_at = datetime.fromtimestamp(received, timezone.utc).isoformat()
        cur = await self._conn.execute(
            """INSERT INTO sms(timestamp,received_at,sender,text,partial,source_id)
            VALUES (?,?,?,?,?,?)""", (timestamp, received_at, sender, text, partial, source_id))
        if not history:
            await self._enqueue(_history_event_key("sms", cur.lastrowid), NotificationMethod.SMS,
                                dict(zip(_SMS_PAYLOAD_FIELDS, (sender, text))))
        return cur.lastrowid

    @read_locked
    async def list_sms(self, limit=20, offset=0, sender=None, since=None, until=None):
        where, args = [], []
        for expression, value in (("sender=?", sender),
                                  ("julianday(timestamp)>=julianday(?)", since),
                                  ("julianday(timestamp)<=julianday(?)", until)):
            if value is not None:
                where.append(expression)
                args.append(value)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return await self._all("SELECT * FROM sms" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit, offset))

    @transaction
    async def insert_call(self, caller, duration, status, recording_path=None, *, source_id, timestamp,
                          history=False, note=None):
        """`note`: a short remark for the call notification (notify_call's `note`), not stored in history."""
        old = await self._one("SELECT id FROM calls WHERE source_id=?", (source_id,))
        if old:
            return old["id"]
        now = _utc_now_iso()
        cur = await self._conn.execute(
            """INSERT INTO calls(timestamp,received_at,caller,duration,status,recording_path,source_id,
            recording_state) VALUES (?,?,?,?,?,?,?,?)""",
            (timestamp, now, caller, duration, status, recording_path, source_id,
             RecordingState.PENDING if recording_path else RecordingState.NONE))
        if not history and status != CALL_REJECTED:
            payload = dict(zip(_CALL_PAYLOAD_FIELDS, (caller, status, duration)))
            if note:
                payload["note"] = note
            await self._enqueue(_history_event_key("calls", cur.lastrowid), NotificationMethod.CALL, payload)
        return cur.lastrowid

    @read_locked
    async def get_call(self, call_id):
        return await self._one("SELECT * FROM calls WHERE id=?", (call_id,))

    @read_locked
    async def list_calls(self, limit=20, offset=0):
        return await self._all("SELECT * FROM calls ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))

    @transaction
    async def set_recording_state(self, call_id, state):
        await self._conn.execute("UPDATE calls SET recording_state=? WHERE id=?", (state, call_id))

    @read_locked
    async def recording_candidates(self, *, include_completed=False):
        states = ((RecordingState.PENDING, RecordingState.READY, RecordingState.FAILED)
                  if include_completed else (RecordingState.PENDING,))
        placeholders = ','.join('?' for _ in states)
        return await self._all(
            f"SELECT * FROM calls WHERE recording_path IS NOT NULL AND recording_state IN ({placeholders})",
            states)

    @transaction
    async def claim_delivery(self, backends, now=None):
        if not backends:
            return None
        now = time.time() if now is None else now
        placeholders = ','.join('?' for _ in backends)
        alert_placeholders = ','.join('?' for _ in _ORDERED_ALERT_METHODS)
        # A later recovery must not overtake an undelivered outage on the same
        # backend, including during backoff or another worker's active lease.
        # Other backends and ordinary SMS/call jobs remain independently due.
        row = await self._one(f"""SELECT queued.* FROM outbox queued
            WHERE queued.backend IN ({placeholders}) AND
            ((queued.status='pending' AND queued.next_attempt<=?) OR
             (queued.status='sending' AND queued.lease_until<=?)) AND
            (queued.method NOT IN ({alert_placeholders}) OR NOT EXISTS (
                SELECT 1 FROM outbox earlier WHERE earlier.backend=queued.backend
                AND earlier.id<queued.id AND earlier.status!='delivered'
                AND earlier.method IN ({alert_placeholders})))
            ORDER BY queued.id LIMIT 1""",
            (*backends, now, now, *_ORDERED_ALERT_METHODS, *_ORDERED_ALERT_METHODS))
        if not row:
            return None
        owner = uuid.uuid4().hex
        await self._conn.execute("UPDATE outbox SET status='sending',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                                 (owner, now + DELIVERY_LEASE_SECONDS, row["id"]))
        row.update(owner=owner, attempts=row["attempts"] + 1)
        row["payload"] = json.loads(row["payload"])
        return row

    @transaction
    async def checkpoint_delivery(self, job, payload):
        cur = await self._conn.execute("""UPDATE outbox SET payload=? WHERE id=? AND owner=?
            AND status='sending' AND lease_until>?""",
            (json.dumps(payload), job['id'], job['owner'], time.time()))
        if cur.rowcount != 1:
            raise RuntimeError('Delivery ownership expired')
        job['payload'] = payload

    @transaction
    async def finish_delivery(self, job, success, now=None):
        now = time.time() if now is None else now
        delay = min(MAX_RETRY_DELAY_SECONDS, 2 ** min(job["attempts"], 12))  # 2**12 > cap: bounds the exponent
        await self._conn.execute("""UPDATE outbox SET status=?,next_attempt=?,owner=NULL,lease_until=NULL
            WHERE id=? AND owner=?""", ("delivered" if success else "pending", now + delay, job["id"], job["owner"]))

    @read_locked
    async def outbox_stats(self):
        return await self._all("SELECT backend,status,COUNT(*) AS count,MAX(attempts) AS attempts FROM outbox GROUP BY backend,status")

    @staticmethod
    def fragment_group(fragment):
        return json.dumps([fragment["device"], fragment["sim"], fragment["sender"],
                           fragment["ref"], fragment["total"]])

    @transaction
    async def ingest_fragment(self, fragment, history=False):
        """Return whether this source record was newly accepted (rather than replayed)."""
        if await self._one("SELECT source_id FROM fragments WHERE source_id=?", (fragment["source_id"],)):
            return False
        total, part = fragment["total"], fragment["part"]
        if not 1 <= part <= total <= MAX_SMS_PARTS:
            raise ValueError("Invalid fragment numbering")
        # SCTS is not generation evidence: SMSCs stamp each delivery attempt, so
        # parts of one message differ by seconds, and a part whose delivery failed
        # arrives tens of minutes later with a fresh stamp. Candidates are the
        # incomplete assemblies of this device, SIM, sender, UDH reference and
        # total whose first accepted fragment was received within
        # MULTIPART_JOIN_WINDOW. A complete assembly is closed. With reused UDH
        # references, a part number taken in every candidate starts a new
        # generation. A single free candidate is joined. Between several, only an
        # equal, usable SCTS of the first accepted fragment decides; otherwise all
        # are marked ambiguous and nothing is joined. A receipt-time substitute
        # (invalid SCTS) joins like any part but never decides between several.
        group = self.fragment_group(fragment)
        received_epoch = fragment["received_epoch"]
        candidates = []
        assembly, ambiguous = None, False
        if total > 1:  # a single-part SMS never joins an assembly
            candidates = await self._open_assemblies(group, part, received_epoch)
            assembly, ambiguous = self._choose_assembly(candidates, fragment["timestamp"], received_epoch)
        if ambiguous:
            await self._mark_ambiguous(candidates)
        if assembly is None:
            assembly = {"id": fragment["source_id"], "sender": fragment["sender"], "total": total,
                        "timestamp": fragment["timestamp"], "sms_id": None, "ambiguous": int(ambiguous),
                        "started": received_epoch, "history": history}
            await self._conn.execute("INSERT INTO assemblies(id,group_key,sender,total,started,timestamp,ambiguous,history) VALUES (?,?,?,?,?,?,?,?)",
                                     (assembly["id"], group, assembly["sender"], total, received_epoch,
                                      assembly["timestamp"], assembly["ambiguous"], history))
        elif history and not assembly["history"]:
            # A retained part can never turn into a live command by joining a
            # newer assembly. Previously published partial notifications stay;
            # every subsequent publication is silent and no command is queued.
            assembly["history"] = True
            await self._conn.execute("UPDATE assemblies SET history=1 WHERE id=?", (assembly["id"],))
        await self._conn.execute("INSERT INTO fragments(source_id,assembly_id,part,text) VALUES (?,?,?,?)",
                                 (fragment["source_id"], assembly["id"], part, fragment["text"]))
        parts = await self._assembly_parts(assembly["id"])
        if len(parts) == total or assembly["sms_id"] is not None:
            await self._publish_assembly(assembly, parts)
        return True

    async def _open_assemblies(self, group, part, received_epoch):
        """Incomplete assemblies of this group started within the join window; `occupied`: part already taken."""
        return await self._all("""SELECT a.*, EXISTS(SELECT 1 FROM fragments f
            WHERE f.assembly_id=a.id AND f.part=?) AS occupied FROM assemblies a
            WHERE a.group_key=? AND abs(a.started-?)<=?
            AND (SELECT COUNT(*) FROM fragments f WHERE f.assembly_id=a.id)<a.total
            ORDER BY a.started,a.id""", (part, group, received_epoch, MULTIPART_JOIN_WINDOW))

    @staticmethod
    def _choose_assembly(candidates, timestamp, received_epoch):
        """For multipart SMS, return (assembly to join or None, whether the reference is ambiguous)."""
        if any(c["ambiguous"] for c in candidates):
            return None, True
        free = [c for c in candidates if not c["occupied"]]
        if len(free) == 1:
            return free[0], False
        # A receipt-time substitute is durably recognizable by timestamp == receipt
        # time (also a genuine SCTS equal to it, conservatively). It is no SCTS
        # evidence: it never takes part in the tie-break, on either side.
        def substitute(stamp, epoch):
            return stamp == datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        matching = [] if substitute(timestamp, received_epoch) else [
            c for c in free if c["timestamp"] == timestamp and not substitute(c["timestamp"], c["started"])]
        if len(matching) == 1:
            return matching[0], False
        # Several free candidates and no unique SCTS: ambiguous. None free: a new generation.
        return None, bool(free)

    async def _mark_ambiguous(self, candidates):
        # Every candidate becomes ambiguous; one already published is republished as such.
        for previous in candidates:
            if previous['ambiguous']:
                continue
            previous['ambiguous'] = 1
            await self._conn.execute("UPDATE assemblies SET ambiguous=1 WHERE id=?", (previous['id'],))
            if previous['sms_id'] is not None:
                await self._publish_assembly(previous, await self._assembly_parts(previous['id']))

    async def _assembly_parts(self, assembly_id):
        return await self._all("SELECT * FROM fragments WHERE assembly_id=? ORDER BY part", (assembly_id,))

    async def _publish_assembly(self, assembly, parts):
        ambiguous = bool(assembly["ambiguous"])
        partial = len(parts) < assembly["total"] or ambiguous
        # Explicit placeholders prevent silently joining nonadjacent fragments.
        by_part = {r["part"]: r["text"] for r in parts}
        body = ''.join(by_part.get(i, f"[missing part {i}/{assembly['total']}]")
                       for i in range(1, assembly["total"] + 1))
        # The prefix is in part 1. Without part 1, parts that are base64url only may be a
        # command's ciphertext and are hidden too; other parts are shown as they are. A
        # late part 1 republishes the text. Only a complete, unambiguous command runs (below).
        is_command = self.sms_commands_enabled and (envelope.is_command(body) or (
            1 not in by_part and all(envelope.is_continuation(text) for text in by_part.values())))
        if is_command:
            text = SMS_COMMAND_INCOMPLETE_TEXT if partial else SMS_COMMAND_TEXT
        else:
            text = "[ambiguous multipart reference] " + body if ambiguous else body
        # Once history, every later publication and sibling stays silent.
        history = assembly["history"]
        sms_id = assembly["sms_id"]
        if sms_id is None:
            sms_id = await self._insert_sms(
                assembly["sender"], text, partial,
                source_id="assembly:" + assembly["id"], timestamp=assembly["timestamp"],
                received=assembly["started"], history=history)
            await self._conn.execute("UPDATE assemblies SET sms_id=? WHERE id=?", (sms_id, assembly["id"]))
        else:
            await self._conn.execute("UPDATE sms SET text=?,partial=? WHERE id=?", (text, partial, sms_id))
            if not history:
                update_key = f"assembly-update:{assembly['id']}:{len(parts)}:{int(ambiguous)}"
                await self._enqueue(update_key, NotificationMethod.SMS,
                                    dict(zip(_SMS_PAYLOAD_FIELDS, (assembly["sender"], text))))
        if is_command and not partial and not history:
            # Same transaction as the SMS, once per SMS (ingest_fragment skips a replayed journal
            # record); OR IGNORE only keeps a broken invariant from failing the reception.
            await self._conn.execute("""INSERT OR IGNORE INTO sms_commands(sms_id,ciphertext)
                VALUES (?,?)""", (sms_id, body.strip()))

    @transaction
    async def flush_fragments(self, timeout, now=None, exclude_groups=()):
        now = time.time() if now is None else now
        for row in await self._all("SELECT * FROM assemblies WHERE sms_id IS NULL AND started<=?", (now - timeout,)):
            if row['group_key'] in exclude_groups:
                continue
            await self._publish_assembly(row, await self._assembly_parts(row["id"]))

    @read_locked
    async def next_sms_command(self):
        """The oldest pending command, with its SMS's SMSC stamp as sms_timestamp."""
        return await self._one("""SELECT c.*, s.timestamp AS sms_timestamp FROM sms_commands c
            JOIN sms s ON s.id=c.sms_id WHERE c.status=? ORDER BY c.id LIMIT 1""", (SmsCommandStatus.PENDING,))

    @read_locked
    async def sms_command_last_stamp(self):
        row = await self._one("SELECT value FROM metadata WHERE key=?", (SMS_COMMAND_LAST_STAMP_KEY,))
        return int(row["value"]) if row else None

    @transaction
    async def begin_sms_command(self, command_id, stamp_ms, name):
        """pending -> running and the replay stamp, together: a crash cannot run one twice."""
        cur = await self._conn.execute("UPDATE sms_commands SET status=?,name=? WHERE id=? AND status=?",
                                       (SmsCommandStatus.RUNNING, name, command_id, SmsCommandStatus.PENDING))
        if cur.rowcount != 1:
            raise RuntimeError("SMS command is not pending")
        await self._set_metadata(SMS_COMMAND_LAST_STAMP_KEY, str(stamp_ms))

    @transaction
    async def finish_sms_command(self, command_id, result, message, name=None):
        """Done with its result, and its HEALTH notification queued in the same transaction."""
        await self._conn.execute("""UPDATE sms_commands SET status=?,result=?,name=COALESCE(?,name),
            ciphertext=NULL WHERE id=?""", (SmsCommandStatus.DONE, result, name, command_id))
        await self._enqueue(f"command:{command_id}", NotificationMethod.HEALTH, {"message": message})

    @read_locked
    async def running_sms_commands(self):
        """Commands a restart stopped while they ran."""
        return await self._all("SELECT id,sms_id,name FROM sms_commands WHERE status=? ORDER BY id",
                               (SmsCommandStatus.RUNNING,))

    @read_locked
    async def get_stats(self):
        return await self._one("""SELECT (SELECT COUNT(*) FROM sms) AS total_sms,
            (SELECT COUNT(*) FROM calls) AS total_calls,
            (SELECT COUNT(*) FROM calls WHERE status=?) AS missed_calls""", (CALL_MISSED,))
