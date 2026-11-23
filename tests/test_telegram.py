import json

import pytest
import httpx
from unittest.mock import AsyncMock, patch
from phonelet.cdr_events import SMS_REPORT_TYPES
from phonelet.notifiers.base import SMS_FAILURE_OUTCOMES
import re

from phonelet.notifiers.telegram import TelegramNotifier, format_duration, sms_chunks
from tests.helpers import deliver_sms


@pytest.fixture
def notifier():
    return TelegramNotifier(token="test-token", chat_id="12345")


async def test_sms_delivery(notifier):
    with patch.object(notifier, "_send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        result = await deliver_sms(notifier, "+15551234567", "Hello world")
        assert result == True
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "+15551234567" in msg
        assert "Hello world" in msg


async def test_sms_links_are_sent_without_link_previews(notifier):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    text = "Sign in: https://example.invalid/one-time-login"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        notifier._client = client
        assert await deliver_sms(notifier, "+15550000001", text)
    notifier._client = None
    assert requests == [{"chat_id": "12345", "text": f"SMS from +15550000001:\n{text}",
                         "link_preview_options": {"is_disabled": True}}]


async def test_notify_call_answered(notifier):
    with patch.object(notifier, "_send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        result = await notifier.notify_call(
            caller="+15551234567", status="answered", duration=150
        )
        assert result == True
        msg = mock_send.call_args[0][0]
        assert "+15551234567" in msg
        assert "2m30s" in msg


async def test_notify_call_missed(notifier):
    with patch.object(notifier, "_send", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        await notifier.notify_call(
            caller="anonymous", status="missed", duration=0
        )
        msg = mock_send.call_args[0][0]
        assert "anonymous number" in msg


async def test_send_failure_returns_false(notifier):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("timeout")
        result = await notifier._send("test message")
        assert result == False


def test_every_sms_report_type_has_an_alert_text():
    assert set(SMS_FAILURE_OUTCOMES) == set(SMS_REPORT_TYPES)


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(60) == "1m0s"
    assert format_duration(150) == "2m30s"
    assert format_duration(3661) == "61m1s"


async def test_unknown_and_answered_calls_never_claim_unconfirmed_recording(notifier):
    with patch.object(notifier, "_send", new_callable=AsyncMock, return_value=True) as send:
        await notifier.notify_call("anonymous", "unknown")
        assert send.call_args.args[0] == "Call from anonymous number — status unknown"
        await notifier.notify_call("+15550000", "answered", 150)
        assert "2m30s" in send.call_args.args[0]
        for call in send.call_args_list:
            assert "recording" not in call.args[0]


@pytest.mark.parametrize("body", ["a" * 9000, "😀" * 5000, "я🧑‍💻e\u0301\\,\n" * 2000])
async def test_long_sms_preserves_complete_unicode_within_limit(notifier, body):
    with patch.object(notifier, "_send", new_callable=AsyncMock, return_value=True) as send:
        assert await deliver_sms(notifier, "+1555000", body)
        chunks = [call.args[0] for call in send.call_args_list]
        assert len(chunks) > 1
        assert strip_headers("+1555000", chunks) == body
        assert all(0 < len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)
        assert all(chunk.encode("utf-8").decode("utf-8") == chunk for chunk in chunks)


def strip_headers(sender, chunks):
    """Check that every chunk carries its own numbered header and return the joined body."""
    body = []
    for number, chunk in enumerate(chunks, 1):
        header = f"SMS from {sender} ({number}/{len(chunks)}):\n"
        assert chunk.startswith(header)
        body.append(chunk[len(header):])
    return "".join(body)


@pytest.mark.parametrize("body", ["a" * 9000, "😀" * 5000, "😀" * 30000, "b" * 4078 * 9 + "c"])
def test_every_chunk_has_a_header_within_limit(body):
    chunks = sms_chunks("+15550000001", body)
    assert len(chunks) > 1
    assert strip_headers("+15550000001", chunks) == body
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)
    assert sms_chunks("+15550000001", body) == chunks


def test_header_digits_grow_with_the_part_count():
    # Nine parts fit with one-digit headers only if the body leaves no room; the
    # split must account for the wider header of ten or more parts.
    sender = "+15550000001"
    room = 4096 - len(f"SMS from {sender} (9/9):\n")
    chunks = sms_chunks(sender, "x" * (room * 9 + 1))
    assert re.match(rf"SMS from \{sender} \(1/{len(chunks)}\):\n", chunks[0])
    assert len(chunks) >= 10
    assert all(len(chunk) <= 4096 for chunk in chunks)


@pytest.mark.parametrize("body", ["", "short", "a" * (4096 - len("SMS from +1555000:\n"))])
def test_single_chunk_keeps_plain_header(body):
    assert sms_chunks("+1555000", body) == [f"SMS from +1555000:\n{body}"]


async def test_later_chunk_failure_does_not_claim_success(notifier):
    with patch.object(notifier, "_send", new_callable=AsyncMock, side_effect=[True, False]) as send:
        assert not await deliver_sms(notifier, "+1555000", "a" * 15000)
        assert send.await_count == 2


async def test_durable_chunk_checkpoint_resumes_on_new_notifier(notifier):
    import json
    payload = {"sender": "+1555000", "text": "😀" * 5000}
    persisted = json.dumps(payload)

    async def checkpoint(updated):
        nonlocal persisted
        persisted = json.dumps(updated)

    with patch.object(notifier, "_send", new_callable=AsyncMock, side_effect=[True, False]) as send:
        assert not await notifier.deliver("notify_sms", payload, checkpoint)
        first_success = send.call_args_list[0].args[0]
        failed_chunk = send.call_args_list[1].args[0]
    assert json.loads(persisted)["_next_chunk"] == 1
    restarted = TelegramNotifier("fake-restart-token", "fake-chat")
    with patch.object(restarted, "_send", new_callable=AsyncMock, return_value=True) as send:
        assert await restarted.deliver("notify_sms", json.loads(persisted), checkpoint)
        remaining = [call.args[0] for call in send.call_args_list]
        assert remaining[0] == failed_chunk
        assert strip_headers(payload["sender"], [first_success, *remaining]) == payload["text"]
    with patch.object(restarted, "_send", new_callable=AsyncMock) as send:
        assert await restarted.deliver("notify_sms", json.loads(persisted), checkpoint)
        send.assert_not_awaited()


async def test_chunk_checkpoint_failure_propagates_without_sending_next(notifier):
    checkpoint = AsyncMock(side_effect=OSError("fake disk full"))
    with patch.object(notifier, "_send", new_callable=AsyncMock, return_value=True) as send:
        with pytest.raises(OSError):
            await notifier.deliver("notify_sms", {"sender": "s", "text": "a" * 9000}, checkpoint)
        send.assert_awaited_once()


@pytest.mark.parametrize("reason, text", [
    ("i", "Outgoing SMS to +15550000001 was not sent or only partly sent (modem send error)"),
    ("e", "Outgoing SMS to +15550000001 was not delivered (delivery report)"),
    ("t", "Outgoing SMS to +15550000001 got no send result from the modem; it may not have been sent"),
    ("r", "Outgoing SMS to +15550000001 may not have been sent: the driver rejected it "
          "(modem not registered, invalid number or message)"),
    ("unknown", "Outgoing SMS to +15550000001: outcome unknown"),
])
async def test_notify_sms_failed(notifier, reason, text):
    with patch.object(notifier, "_send", new_callable=AsyncMock, return_value=True) as send:
        assert await notifier.deliver("notify_sms_failed", {"recipient": "+15550000001", "reason": reason},
                                      AsyncMock())
        send.assert_awaited_once_with(text)


async def test_bad_checkpoint_not_reported_as_success(notifier):
    with pytest.raises(ValueError):
        await notifier.deliver("notify_sms", {"sender": "s", "text": "body", "_next_chunk": 100}, AsyncMock())


@pytest.mark.parametrize("status, payload, expected", [
    (200, {"ok": True}, True), (200, {"ok": False}, False),
    (429, {"ok": False}, False), (500, {}, False), (200, {}, False),
])
async def test_api_acknowledgment_required(notifier, status, payload, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload)
    )) as client:
        notifier._client = client
        assert await notifier._send("fake private content") is expected
    notifier._client = None
