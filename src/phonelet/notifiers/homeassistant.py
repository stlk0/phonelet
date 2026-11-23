import logging

from phonelet.constants import RSSI_UNKNOWN, Availability
from phonelet.notifiers.base import BaseNotifier
from phonelet.logging_privacy import install_log_privacy, safe_error

logger = logging.getLogger(__name__)


def signal_icon(rssi: int | None) -> str:
    if rssi is None or rssi == RSSI_UNKNOWN:
        return "mdi:signal-off"
    if rssi >= 20:
        return "mdi:signal-cellular-3"
    if rssi >= 10:
        return "mdi:signal-cellular-2"
    return "mdi:signal-cellular-1"


def modem_sensor_state(status: dict) -> dict:
    """Home Assistant state of sensor.phonelet_modem for a ModemStatus.to_dict() snapshot."""
    availability = status.get("availability", Availability.AVAILABLE)
    registered = status.get("network_registered")
    rssi = status.get("signal_rssi", RSSI_UNKNOWN)
    if availability != Availability.AVAILABLE or registered is None:
        # Home Assistant's own entity state, not the monitor's Availability.
        state, icon = "unavailable", "mdi:signal-off"
    else:
        state = "registered" if registered else "unregistered"
        icon = signal_icon(rssi)
    return {
        "state": state,
        "attributes": {
            "friendly_name": "Phonelet Modem",
            "availability": availability,
            "availability_reason": status.get("availability_reason"),
            "ami_authenticated": status.get("ami_authenticated"),
            "device_present": status.get("device_present"),
            "last_sample": status.get("last_sample"),
            "icon": icon,
            "signal_rssi": rssi,
            "signal_dbm": status.get("signal_dbm"),
            "operator": status.get("operator", ""),
            "roaming": status.get("roaming", False),
            "registration_status": status.get("registration_status"),
            "last_updated": status.get("modem_status_updated"),
        },
    }


class HomeAssistantNotifier(BaseNotifier):
    backend_id = "homeassistant"
    secret_settings = ("ha_token",)

    @classmethod
    def from_settings(cls, config, *, ami):
        if config.ha_url and config.ha_token:
            return cls(config.ha_url, config.ha_token)
        return None

    def __init__(self, url: str, token: str):
        super().__init__()
        install_log_privacy(secrets=(token,))
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _post(self, path: str, payload: dict, action: str) -> bool:
        async with self._http_client() as client:
            try:
                resp = await client.post(f"{self._url}{path}", json=payload, headers=self._headers)
                if not 200 <= resp.status_code < 300:
                    logger.error("HA request failed: HTTP %d", resp.status_code)
                    return False
                return True
            except Exception as e:
                logger.error("HA %s failed: %s", action, safe_error(e))
                return False

    async def _fire_event(self, event_type: str, data: dict) -> bool:
        return await self._post(f"/api/events/{event_type}", data, "event fire")

    async def notify_sms(self, sender: str, text: str) -> bool:
        return await self._fire_event(
            "sim_sms_received", {"sender": sender, "text": text}
        )

    async def notify_call(
        self, caller: str, status: str, duration: int = 0, note: str = ""
    ) -> bool:
        data = {"caller": caller, "status": status, "duration": duration}
        if note:
            data["note"] = note
        return await self._fire_event("sim_call_received", data)

    async def notify_network_lost(self, operator: str, signal_rssi: int) -> bool:
        return await self._fire_event(
            "sim_network_lost",
            {"operator": operator, "signal_rssi": signal_rssi},
        )

    async def notify_network_restored(self, operator: str, signal_rssi: int) -> bool:
        return await self._fire_event(
            "sim_network_restored",
            {"operator": operator, "signal_rssi": signal_rssi},
        )

    async def push_modem_sensor(self, status: dict) -> bool:
        """Push modem status as a HA sensor entity via POST /api/states/..."""
        try:
            payload = modem_sensor_state(status)
        except Exception as e:  # a malformed status is logged like a failed push, not raised
            logger.error("HA sensor push failed: %s", safe_error(e))
            return False
        return await self._post("/api/states/sensor.phonelet_modem", payload, "sensor push")

    async def notify_health(self, message: str) -> bool:
        return await self._fire_event("sim_health_alert", {"message": message})

    async def notify_modem_unavailable(self, reason: str) -> bool:
        return await self._fire_event("sim_modem_unavailable", {"reason": reason})
