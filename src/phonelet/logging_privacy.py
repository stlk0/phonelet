"""Process-wide logging policy; install before creating AMI/HTTP clients.

Protocol libraries log credentials, headers and message bodies. Keep only their
logger/level and exception type, never their wire data. Application code should
log identifiers/counts, not events or message bodies. An exception record keeps
the exception type and code locations, never exception text; only phonelet's
own loggers also keep their message.
"""
import logging
import os
import re
import traceback
from collections.abc import Iterable

_PROTOCOL_LOGGERS = ("httpx", "httpcore", "aiosqlite")
_MAX_FRAMES = 6
_TOKEN_URL = re.compile(r"(/bot)[^/\s?#]+", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[^\s,'\"}]+", re.IGNORECASE)
_secrets: set[str] = set()


def safe_error(error: BaseException) -> str:
    """Allowlisted diagnostics: no exception message, URL, body or headers."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    result = type(error).__name__
    if isinstance(status, int):
        result += f" (HTTP {status})"
    return result


def _redact(message: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    return _BEARER.sub("Bearer [REDACTED]", _TOKEN_URL.sub(r"\1[REDACTED]", message))


def _describe(error: BaseException) -> str:
    """Type and frames as file:line function, outermost first.

    The outermost frame is where the exception was caught; the deepest ones are
    where it was raised, often inside libraries. No exception text, chained
    exceptions, locals or source lines.
    """
    frames = [f"{os.path.basename(frame.f_code.co_filename)}:{lineno} {frame.f_code.co_name}"
              for frame, lineno in traceback.walk_tb(error.__traceback__)]
    if len(frames) > _MAX_FRAMES:
        frames[1:1 - _MAX_FRAMES] = ["..."]  # keep the outermost and the deepest
    return _redact(safe_error(error) + (" at " + " > ".join(frames) if frames else ""))


def _message(record: logging.LogRecord) -> str:
    """The redacted message; a formatting failure never escapes the logging call."""
    try:
        return _redact(record.getMessage())
    except Exception:
        # Neither the arguments nor the formatting error text: both may be private.
        text = _redact(record.msg) if isinstance(record.msg, str) else "<non-string message>"
        return text + " [unformattable log arguments]"


def _in_family(logger_name: str, *roots: str) -> bool:
    """The logger is one of roots or a descendant of one."""
    return any(logger_name == root or logger_name.startswith(root + ".") for root in roots)


def install_log_privacy(*, secrets: Iterable[str] = ()) -> None:
    """Idempotently protect records before any handler can format them.

    A record factory covers descendant loggers and pre-existing handlers too;
    merely filtering the root logger does not cover propagated child records.
    """
    _secrets.update(value for value in secrets if value)
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_phonelet_privacy", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        protocol = _in_family(record.name, *_PROTOCOL_LOGGERS)
        access = (record.name == "uvicorn.access"
                  and isinstance(record.args, tuple) and len(record.args) == 5)
        if access:
            # AccessFormatter unpacks these five arguments itself. Flattening
            # the record breaks Uvicorn's default handler, even with safe text.
            client, method, path, version, status = record.args
            path = str(path).split("?", 1)[0].split("#", 1)[0]
            record.msg = '%s - "%s %s HTTP/%s" %d'
            record.args = tuple(_redact(str(value)) for value in (
                client, method, path, version,
            )) + (status,)
        elif protocol:
            record.msg = "Protocol details suppressed"
            if record.exc_info and record.exc_info[1]:
                record.msg += ": " + _describe(record.exc_info[1])
        elif record.exc_info and record.exc_info[1]:
            # Exception text and chained exceptions can contain private payloads.
            # Only our own messages are kept: asyncio and other libraries put
            # exception reprs and callback arguments into theirs.
            own = _in_family(record.name, "phonelet")
            record.msg = ((_message(record).rstrip() if own else "Operation failed")
                          + ": " + _describe(record.exc_info[1]))
        else:
            record.msg = _message(record)
        if not access:
            record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return record

    factory._phonelet_privacy = True
    logging.setLogRecordFactory(factory)


def configure_logging(level: str | int = "INFO", *, secrets: Iterable[str] = ()) -> None:
    """Main's logging entry point; safe at both INFO and DEBUG."""
    install_log_privacy(secrets=secrets)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
