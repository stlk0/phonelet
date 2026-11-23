"""Only synthetic credentials/content and mock HTTP; never sends on the network."""
import asyncio
import gc
import logging
import re
import sqlite3
import traceback
from unittest.mock import AsyncMock

import httpx
import pytest

from phonelet.contracts import MalformedCdr
from phonelet.cdr_events import CdrHandler
from phonelet.ingress import IngressReplay
from phonelet.logging_privacy import configure_logging, install_log_privacy
from phonelet.notification import delivery_loop
from phonelet.notifiers.telegram import TelegramNotifier
from phonelet.notifiers.homeassistant import HomeAssistantNotifier
from tests.helpers import deliver_sms

TOKEN = "123456:FAKE_PRIVATE_TOKEN"
PRIVATE = "FAKE SMS verification code 543210 +15550001122"
PRIVATE_PARTS = (TOKEN, PRIVATE, "543210", "+15550001122", "FAKE SMS")
FRAME = re.compile(r"\S+\.py:\d+ \S+")


@pytest.fixture(autouse=True)
def preserve_log_factory(monkeypatch):
    monkeypatch.setattr("phonelet.logging_privacy._secrets", set())
    original = logging.getLogRecordFactory()
    yield
    logging.setLogRecordFactory(original)


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("outcome", ["ok", "http_error", "transport_error", "invalid_json", "api_error"])
async def test_telegram_logs_private_data_neither_on_send_nor_retry(caplog, level, outcome):
    caplog.set_level(level)
    configure_logging(level, secrets=(TOKEN,))
    notifier = TelegramNotifier(TOKEN, "FAKE_CHAT_ID")

    def respond(request):
        if outcome == "transport_error":
            raise httpx.ConnectError(f"{request.url} {PRIVATE}", request=request)
        if outcome == "http_error":
            return httpx.Response(401, text=f"{TOKEN} {PRIVATE}")
        if outcome == "invalid_json":
            return httpx.Response(200, text=f"{TOKEN} {PRIVATE}")
        return httpx.Response(200, json={"ok": outcome == "ok", "description": PRIVATE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        notifier._client = client
        for _ in range(2):
            assert await deliver_sms(notifier, "+15550001122", PRIVATE) is (outcome == "ok")
    assert TOKEN not in caplog.text
    assert PRIVATE not in caplog.text
    assert "+15550001122" not in caplog.text
    assert "FAKE_CHAT_ID" not in caplog.text
    if outcome == "http_error":
        assert "HTTPStatusError (HTTP 401)" in caplog.text
    if outcome == "transport_error":
        assert "ConnectError" in caplog.text


@pytest.mark.parametrize("name", ["httpx", "httpcore.http11", "aiosqlite"])
def test_descendant_debug_logs_suppressed_with_existing_handlers(caplog, name):
    caplog.set_level(logging.DEBUG)
    install_log_privacy(secrets=(TOKEN,))
    logger = logging.getLogger(name)
    logger.debug("Raw frame %s", {"Secret": TOKEN, "Message": PRIVATE})
    try:
        raise ValueError(PRIVATE + TOKEN)
    except ValueError:
        logger.log(logging.ERROR, "Failed payload: %s", PRIVATE, exc_info=True)
    assert PRIVATE not in caplog.text
    assert TOKEN not in caplog.text
    assert "ValueError" in caplog.text


def test_url_and_registered_secret_redacted_outside_protocol_logger(caplog):
    caplog.set_level(logging.DEBUG)
    install_log_privacy(secrets=(TOKEN, "FAKE_AMI_SECRET"))
    factory = logging.getLogRecordFactory()
    install_log_privacy(secrets=("FAKE_HA_SECRET",))
    assert logging.getLogRecordFactory() is factory
    logging.getLogger("phonelet.anywhere").info(
        "https://api.telegram.org/bot%s/sendMessage %s Bearer FAKE_HA_SECRET",
        TOKEN, "FAKE_AMI_SECRET",
    )
    assert TOKEN not in caplog.text
    assert "FAKE_AMI_SECRET" not in caplog.text
    assert "FAKE_HA_SECRET" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_registry_channel_secret_is_redacted_before_channel_construction(tmp_path, monkeypatch, caplog):
    from phonelet import main
    from phonelet.config import Settings
    from phonelet.notifiers import registry
    from tests.helpers import local_paths

    class ExtraSettings(Settings):
        extra_channel_token: str = 'SYNTHETIC_EXTRA_CHANNEL_CREDENTIAL'

    class ExtraChannel(TelegramNotifier):
        secret_settings = ('extra_channel_token',)

        @classmethod
        def from_settings(cls, config, *, ami):
            return cls(config.extra_channel_token, 'synthetic-chat')

    monkeypatch.setattr(registry, 'CHANNELS', [*registry.CHANNELS, ExtraChannel])
    config = ExtraSettings(**local_paths(tmp_path), api_key='fake', ami_secret='fake')
    caplog.set_level(logging.INFO)
    app = main.create_application(config)
    assert not hasattr(app.state, 'runtime')
    logging.getLogger('phonelet.anywhere').info('Channel credential %s', config.extra_channel_token)
    assert config.extra_channel_token not in caplog.text
    assert 'Channel credential [REDACTED]' in caplog.text


async def test_homeassistant_exception_logs_type_only(caplog):
    caplog.set_level(logging.DEBUG)
    notifier = HomeAssistantNotifier("https://ha.invalid", TOKEN)

    def respond(request):
        raise httpx.ConnectError(f"{TOKEN} {PRIVATE}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        notifier._client = client
        assert not await notifier.notify_sms("+15550001122", PRIVATE)
        assert not await notifier.push_modem_sensor({"availability": "unavailable"})
    assert "ConnectError" in caplog.text
    assert TOKEN not in caplog.text
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("use_colors", [False, True])
@pytest.mark.parametrize("path, expected_path", [
    ("/api/sms", "/api/sms"),
    (f"/api/sms?token={TOKEN}&text=private-query-content", "/api/sms"),
    (f"/bot{TOKEN}/sendMessage", "/bot[REDACTED]/sendMessage"),
])
def test_uvicorn_access_formatter_preserves_structured_arguments(use_colors, path, expected_path):
    from uvicorn.config import LOGGING_CONFIG
    from uvicorn.logging import AccessFormatter

    install_log_privacy(secrets=(TOKEN,))
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1000", "GET", path, "1.1", 200), None,
    )
    formatter = AccessFormatter(
        fmt=LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=use_colors,
    )
    output = formatter.format(record)
    assert len(record.args) == 5
    assert record.args[-1] == 200
    assert "127.0.0.1:1000" in output
    assert f"GET {expected_path} HTTP/1.1" in output
    assert "200 OK" in output
    # The default handler and any propagated ordinary handler are both safe.
    for formatted in (output, logging.Formatter("%(message)s").format(record)):
        assert TOKEN not in formatted
        assert "private-query-content" not in formatted


def _private_cause():
    raise ValueError(PRIVATE)


def _private_failure(depth):
    if depth:
        return _private_failure(depth - 1)
    try:
        _private_cause()
    except ValueError as cause:
        raise RuntimeError(f"{TOKEN} {PRIVATE}") from cause


def assert_no_private_details(text):
    for value in PRIVATE_PARTS:
        assert value not in text
    # Neither the chained cause, nor a traceback or source line is printed.
    for value in ("ValueError", "_private_cause", "Traceback", "raise RuntimeError"):
        assert value not in text


@pytest.mark.parametrize("depth", [0, 10])
def test_exception_record_keeps_message_type_and_place(caplog, depth):
    caplog.set_level(logging.INFO)
    install_log_privacy(secrets=(TOKEN,))
    try:
        _private_failure(depth)
    except RuntimeError as error:
        innermost = traceback.extract_tb(error.__traceback__)[-1]
        logging.getLogger("phonelet.anywhere").exception(
            "Delivery failed: job=%s key=%s", 7, TOKEN)
    [record] = [r for r in caplog.records if r.name == "phonelet.anywhere"]
    message = record.getMessage()
    assert message.startswith("Delivery failed: job=7 key=[REDACTED]: RuntimeError at ")
    # Outermost (where caught) first, deepest last; file names have no directory.
    assert message.endswith(f" test_logging_privacy.py:{innermost.lineno} _private_failure")
    frames = FRAME.findall(message)
    assert frames[0].startswith("test_logging_privacy.py:")
    assert frames[0].endswith(" test_exception_record_keeps_message_type_and_place")
    if depth:
        assert len(frames) == 6
        assert f" at {frames[0]} > ... > {frames[1]} > " in message
        assert all(frame.endswith(" _private_failure") for frame in frames[1:])
    else:
        assert len(frames) == 2
        assert "..." not in message
    assert "/" not in message.split(" at ", 1)[1]
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert logging.Formatter().format(record) == message
    assert_no_private_details(caplog.text)


def fake_secret_frame_7f3a():
    raise RuntimeError(PRIVATE)


def test_exception_description_redacted_and_kept_on_one_line(caplog):
    caplog.set_level(logging.INFO)
    install_log_privacy(secrets=("fake_secret_frame_7f3a",))
    try:
        fake_secret_frame_7f3a()
    except RuntimeError:
        logging.getLogger("phonelet.anywhere").exception("Callback failed\n")
    [record] = [r for r in caplog.records if r.name == "phonelet.anywhere"]
    assert re.fullmatch(r"Callback failed: RuntimeError at test_logging_privacy\.py:\d+ "
                        r"test_exception_description_redacted_and_kept_on_one_line > "
                        r"test_logging_privacy\.py:\d+ \[REDACTED\]", record.getMessage())
    assert "fake_secret_frame_7f3a" not in caplog.text
    for value in PRIVATE_PARTS:
        assert value not in caplog.text


def test_third_party_exception_record_keeps_type_and_place_only(caplog):
    caplog.set_level(logging.INFO)
    install_log_privacy(secrets=(TOKEN,))

    async def failing_task():
        _private_failure(0)

    def failing_callback(payload):
        raise RuntimeError(payload)

    # asyncio's default handler puts exception reprs and callback arguments
    # into its own message ("Task exception was never retrieved", "Exception
    # in callback failing_callback('...')").
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(failing_task())
        loop.call_soon(failing_callback, PRIVATE)
        loop.run_until_complete(asyncio.sleep(0))
        assert task.done()
        del task  # reported as never retrieved once collected
        gc.collect()
    finally:
        loop.close()
    try:
        _private_failure(0)
    except RuntimeError as error:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application: %s", error)
    messages = [r.getMessage() for r in caplog.records if r.name in ("asyncio", "uvicorn.error")]
    assert len(messages) == 3
    for message in messages:
        assert re.fullmatch(r"Operation failed: RuntimeError at "
                            r"\S+\.py:\d+ \S+( > \S+\.py:\d+ \S+)*", message)
    for value in ("never retrieved", "Exception in callback", "ASGI application"):
        assert value not in caplog.text
    assert_no_private_details(caplog.text)


class Unprintable:
    def __str__(self):
        raise ValueError(PRIVATE)

    __repr__ = __str__


@pytest.mark.parametrize("exception", [False, True])
def test_unformattable_record_neither_raises_nor_leaks(caplog, exception):
    caplog.set_level(logging.INFO)
    install_log_privacy(secrets=(TOKEN,))
    logger = logging.getLogger("phonelet.anywhere")
    try:
        _private_failure(0)
    except RuntimeError:
        logger.info(f"Job %s backend %s key {TOKEN}", 7, exc_info=exception)
        logger.info("Payload %s", Unprintable(), exc_info=exception)
        logger.info(Unprintable(), exc_info=exception)
    messages = [r.getMessage() for r in caplog.records if r.name == "phonelet.anywhere"]
    expected = ["Job %s backend %s key [REDACTED]", "Payload %s", "<non-string message>"]
    suffix = (r": RuntimeError at test_logging_privacy\.py:\d+ \S+ > "
              r"test_logging_privacy\.py:\d+ _private_failure") if exception else ""
    assert len(messages) == 3
    for text, message in zip(expected, messages):
        assert re.fullmatch(re.escape(text + " [unformattable log arguments]") + suffix, message)
    assert "arguments for format string" not in caplog.text
    assert_no_private_details(caplog.text)


@pytest.mark.parametrize("name, level", [
    ("httpx", logging.ERROR), ("httpcore.http11", logging.WARNING),
    ("aiosqlite", logging.ERROR),
])
def test_protocol_exception_record_keeps_type_and_place_only(caplog, name, level):
    caplog.set_level(logging.DEBUG)
    install_log_privacy(secrets=(TOKEN,))
    try:
        _private_failure(0)
    except RuntimeError:
        logging.getLogger(name).log(level, "Failed payload: %s", PRIVATE, exc_info=True)
    [record] = [r for r in caplog.records if r.name == name]
    assert re.fullmatch(r"Protocol details suppressed: RuntimeError at "
                        r"\S+\.py:\d+ \S+ > test_logging_privacy\.py:\d+ _private_failure",
                        record.getMessage())
    assert "Failed payload" not in caplog.text
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert_no_private_details(caplog.text)


async def test_malformed_cdr_error_carries_no_event_data():
    # The CDR replay logs a MalformedCdr by type only; its text must not carry the row either.
    handler = CdrHandler(AsyncMock(), AsyncMock())
    with pytest.raises(MalformedCdr) as error:
        await handler.handle_cdr({
            "Channel": "Dongle/dongle0-0100000000", "LastApplication": "Dial",
            "Source": "+15550001122", "Disposition": "ANSWERED", "BillableSeconds": PRIVATE,
            "UniqueID": "1700000000.1", "StartTime": "2026-09-25 12:00:00",
        })
    for value in (*PRIVATE_PARTS, "+15550001122", "1700000000.1"):
        assert value not in str(error.value)


async def test_ingress_replay_logs_db_failure_place_without_sms(db, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    install_log_privacy()
    fields = ["1", *(value.encode().hex() for value in (
        "dongle0", "sim1", "2026-09-25 12:00:00 +00:00", "+15550001122")),
        "42", "1", "1", PRIVATE.encode().hex(), "1", "AABB"]
    (tmp_path / "one.sms").write_bytes(("\t".join(fields) + "\n").encode())

    async def failing_ingest(fragment, **kwargs):
        raise sqlite3.OperationalError(f"disk I/O error {fragment['sender']} {fragment['text']}")
    monkeypatch.setattr(db, "ingest_fragment", failing_ingest)
    consumer = IngressReplay(db, CdrHandler(AsyncMock(), AsyncMock()),
                             tmp_path, tmp_path / "phonelet.csv")
    await consumer.replay_once()
    assert consumer.errors == 1
    [record] = [r for r in caplog.records if r.name == "phonelet.ingress"]
    assert record.levelno == logging.ERROR
    assert re.fullmatch(r"SMS journal record retained for replay: OperationalError at "
                        r"ingress\.py:\d+ replay_once > test_logging_privacy\.py:\d+ failing_ingest",
                        record.getMessage())
    assert "disk I/O error" not in caplog.text
    for value in PRIVATE_PARTS:
        assert value not in caplog.text


async def test_notification_loop_logs_db_failure_place(caplog):
    caplog.set_level(logging.INFO)
    install_log_privacy()

    class Backend:
        backend_id = "telegram"

    class FailingQueue:
        async def claim_delivery(self, backends):
            raise sqlite3.OperationalError(f"database is locked {PRIVATE}")

    def records():
        return [r for r in caplog.records if r.name == "phonelet.notification"]

    task = asyncio.create_task(delivery_loop(FailingQueue(), [Backend()]))
    try:
        async with asyncio.timeout(5):
            while not records():
                await asyncio.sleep(0)
    finally:
        task.cancel()
    [record] = records()
    assert record.levelno == logging.ERROR
    assert re.fullmatch(r"Notification queue unavailable; will retry: OperationalError at "
                        r"notification\.py:\d+ delivery_loop > "
                        r"notification\.py:\d+ deliver_pending > "
                        r"test_logging_privacy\.py:\d+ claim_delivery",
                        record.getMessage())
    assert "database is locked" not in caplog.text
    for value in PRIVATE_PARTS:
        assert value not in caplog.text
