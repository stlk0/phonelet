"""Check and run command SMS (db.sms_commands) one at a time, oldest first.

An accepted command records its stamp and becomes `running` in one
transaction, so a crash cannot run it twice: at the next start it ends as
interrupted. A rejected one moves neither the replay stamp nor anything the
command controls. Of the arguments, the journal shows only what
Command.describe gives and a notification only what the command's own message
says; neither ever carries the key or the ciphertext.
"""
import asyncio
import logging
import time
from datetime import datetime
from enum import StrEnum

from phonelet.sms_commands.envelope import MAX_CODE, DecodeError, RejectionReason, decode

logger = logging.getLogger(__name__)

# The phone stamp against the SMSC stamp (SCTS): an SMS can wait at the SMSC for a
# day, but the phone cannot have made it long after the SMSC received it.
MAX_STAMP_AGE_MS = 24 * 3600 * 1000
MAX_STAMP_AHEAD_MS = 3600 * 1000
# The SMSC stamp against the board's clock: the journal, replayed into an older or empty
# database, brings old command SMS back as new. 48 h leaves room for a late part (it
# completes a command up to db.MULTIPART_JOIN_WINDOW, 24 h, after the first) and a late
# delivery. A board clock that is behind only passes.
MAX_COMMAND_AGE_MS = 48 * 3600 * 1000


class RunResult(StrEnum):
    """The result the worker stores for a command that did not end by itself."""
    INTERRUPTED = "interrupted"  # a restart cut its run short
    ERROR = "failed:error"  # its run raised


def by_code(commands):
    """The worker's command table; a duplicate or out-of-range code is a programming error."""
    table = {}
    for command in commands:
        if not 0 <= command.code <= MAX_CODE or command.code in table:
            raise ValueError(f"Bad or duplicate SMS command code {command.code}")
        table[command.code] = command
    return table


class _Rejected(Exception):
    def __init__(self, reason, code=None):  # code: None until the envelope decodes
        super().__init__(reason)
        self.reason, self.code = reason, code


def _smsc_ms(timestamp):
    """The SMSC stamp (SCTS) in ms; ingress stores it with its UTC offset."""
    return int(datetime.fromisoformat(timestamp).timestamp() * 1000)


def _check(row, key, last_stamp, commands, now_ms):
    """Return (stamp, command, arguments), or raise _Rejected; docs/sms-commands.md lists these checks in this order.

    now_ms: the board's clock; stale also covers an SMSC stamp older than MAX_COMMAND_AGE_MS by it.
    """
    if key is None:
        raise _Rejected(RejectionReason.DISABLED)
    try:
        envelope = decode(key, row["ciphertext"])
    except DecodeError as rejection:
        raise _Rejected(rejection.reason) from None
    if last_stamp is not None and envelope.stamp_ms <= last_stamp:
        raise _Rejected(RejectionReason.REPLAY, envelope.code)
    smsc_ms = _smsc_ms(row["sms_timestamp"])
    if envelope.stamp_ms - smsc_ms > MAX_STAMP_AHEAD_MS:
        raise _Rejected(RejectionReason.FUTURE, envelope.code)
    if smsc_ms - envelope.stamp_ms > MAX_STAMP_AGE_MS or now_ms - smsc_ms > MAX_COMMAND_AGE_MS:
        raise _Rejected(RejectionReason.STALE, envelope.code)
    command = commands.get(envelope.code)
    if command is None:
        raise _Rejected(RejectionReason.UNKNOWN_COMMAND, envelope.code)
    try:
        args = command.parse(envelope.packed_args)
    except ValueError:  # UnicodeDecodeError too; `from None`: it holds the argument bytes
        raise _Rejected(RejectionReason.BAD_FORMAT, envelope.code) from None
    return envelope.stamp_ms, command, args


async def _reject(db, row, commands, reason, code=None):
    command = commands.get(code)
    if command is not None:
        shown, label, name = command.name, command.label, command.name
    elif code is None:
        shown, label, name = "-", "SMS", None
    else:
        shown, label, name = f"code {code}", "SMS", None
    logger.warning("SMS command rejected: sms=%s command=%s reason=%s", row["sms_id"], shown, reason)
    await db.finish_sms_command(row["id"], "rejected:" + reason, f"{label} command rejected: {reason}", name=name)


async def process_commands(db, key, commands, clock=time.time):
    """Check and run every pending command, oldest first; `commands` is a by_code table, clock the board's.

    Returns None, or the (command id, result, message) the database refused to
    save: command_loop saves it on a later pass, and the command never runs again.
    """
    while row := await db.next_sms_command():
        # Outside the catch-all: a database error leaves the command pending for the next pass.
        last_stamp = await db.sms_command_last_stamp()
        try:
            stamp, command, args = _check(row, key, last_stamp, commands, int(clock() * 1000))
            details = command.describe(args)
        except _Rejected as rejection:
            await _reject(db, row, commands, rejection.reason, rejection.code)
            continue
        except Exception:
            # Anything else would hold this command, and every later one, forever.
            logger.exception("SMS command check failed: sms=%s", row["sms_id"])
            await _reject(db, row, commands, RejectionReason.CHECK_ERROR)
            continue
        await db.begin_sms_command(row["id"], stamp, command.name)
        logger.info("SMS command accepted: sms=%s command=%s%s", row["sms_id"], command.name,
                    " " + details if details else "")
        started = time.monotonic()
        try:
            result, message = await command.run(args)
        except Exception:
            logger.exception("SMS command failed: sms=%s command=%s", row["sms_id"], command.name)
            result, message = RunResult.ERROR, f"{command.label}: command failed, check the board"
        logger.info("SMS command finished: sms=%s command=%s result=%s in %.0f s", row["sms_id"], command.name,
                    result, time.monotonic() - started)
        finished = (row["id"], result, message)
        try:
            await db.finish_sms_command(*finished)
        except Exception:
            logger.exception("SMS command result not saved: sms=%s; will retry", row["sms_id"])
            return finished
    return None


async def _end_interrupted(db, commands):
    """Finish the commands a restart left `running`, without running them again."""
    for row in await db.running_sms_commands():
        command = next((command for command in commands.values() if command.name == row["name"]), None)
        if command is None:  # its kind is gone from this release
            message = f"SMS command {row['name']} interrupted by a restart; not repeated"
        else:
            message = command.interrupted_message()
        await db.finish_sms_command(row["id"], RunResult.INTERRUPTED, message)
        logger.warning("SMS command interrupted, not repeated: sms=%s command=%s", row["sms_id"], row["name"])


async def command_loop(db, key, commands, poll_interval, clock=time.time):
    """Every poll_interval: each command's tick, then every pending command.

    `key` None rejects already queued commands as disabled; reception with commands
    off stores new PC1 texts as ordinary SMS. `commands` is a by_code table.
    The first pass ends commands left `running` by a restart as
    interrupted. A failing tick does not stop the other commands, and the ticks
    go on while a result waits to be saved; no other command runs meanwhile.
    """
    restart_handled = False
    unsaved = None  # (command id, result, message): run, its result not saved yet
    while True:
        for command in commands.values():
            try:
                await command.tick(db)
            except Exception:
                logger.exception("SMS command %s tick failed; will retry", command.name)
        try:
            if not restart_handled:
                await _end_interrupted(db, commands)
                restart_handled = True
            if unsaved:
                await db.finish_sms_command(*unsaved)
                unsaved = None
            unsaved = await process_commands(db, key, commands, clock)
        except Exception:
            logger.exception("SMS command processing unavailable; will retry")
        await asyncio.sleep(poll_interval)
