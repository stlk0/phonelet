import pytest
import httpx
from unittest.mock import AsyncMock, patch
from phonelet.notifiers.homeassistant import HomeAssistantNotifier


@pytest.fixture
def notifier():
    return HomeAssistantNotifier(url="http://192.0.2.10:8123", token="test-token")


async def test_notify_sms(notifier):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200)
        result = await notifier.notify_sms(sender="+15551234567", text="Hello")
        assert result == True
        call_args = mock_post.call_args
        assert "sim_sms_received" in call_args[0][0]
        assert call_args[1]["json"]["sender"] == "+15551234567"


async def test_notify_call(notifier):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(200)
        result = await notifier.notify_call(
            caller="+15551234567", status="answered", duration=120
        )
        assert result == True
        call_args = mock_post.call_args
        assert "sim_call_received" in call_args[0][0]


async def test_send_failure_returns_false(notifier):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("timeout")
        result = await notifier.notify_sms(sender="+7", text="fail")
        assert result == False


@pytest.mark.parametrize("availability, registered", [("unavailable", None), ("unknown", None), ("available", None)])
async def test_unknown_state_publishes_unavailable(notifier, availability, registered):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        post.return_value = httpx.Response(200)
        status = {"availability": availability, "network_registered": registered,
                  "availability_reason": "ami_disconnected", "last_sample": {"network_registered": True}}
        assert await notifier.push_modem_sensor(status)
        payload = post.call_args.kwargs["json"]
        assert payload["state"] == "unavailable"
        assert payload["attributes"]["last_sample"] == status["last_sample"]
        assert payload["attributes"]["icon"] == "mdi:signal-off"


@pytest.mark.parametrize("registered, expected", [(True, "registered"), (False, "unregistered")])
async def test_known_state_publishes_registration(notifier, registered, expected):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        post.return_value = httpx.Response(200)
        assert await notifier.push_modem_sensor({"availability": "available", "network_registered": registered})
        assert post.call_args.kwargs["json"]["state"] == expected


async def test_monitor_unavailable_event_and_stable_backend(notifier):
    assert notifier.backend_id == "homeassistant"
    with patch.object(notifier, "_fire_event", new_callable=AsyncMock, return_value=True) as send:
        assert await notifier.notify_modem_unavailable("device_missing")
        send.assert_awaited_once_with("sim_modem_unavailable", {"reason": "device_missing"})


async def test_failed_outgoing_sms_is_a_health_alert(notifier):
    # No own event type: the base default sends it through notify_health.
    with patch.object(notifier, "_fire_event", new_callable=AsyncMock, return_value=True) as send:
        assert await notifier.notify_sms_failed(recipient="+15550000001", reason="i")
        send.assert_awaited_once_with(
            "sim_health_alert",
            {"message": "Outgoing SMS to +15550000001 was not sent or only partly sent (modem send error)"})


@pytest.mark.parametrize("status", [301, 401, 500])
async def test_event_rejection_returns_false(notifier, status):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        post.return_value = httpx.Response(status)
        assert not await notifier.notify_health("Fake alert")


async def test_empty_sensor_input_is_unknown(notifier):
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post:
        post.return_value = httpx.Response(200)
        assert await notifier.push_modem_sensor({})
        assert post.call_args.kwargs["json"]["state"] == "unavailable"
