import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from phonelet.ami_client import Message

from phonelet.modem_status import (
    parse_device_entry,
    rssi_to_dbm,
    ModemStatus,
    ModemStatusMonitor,
)
from phonelet.constants import (
    NET_NOT_REGISTERED,
    NET_REGISTERED_HOME,
    NET_REGISTERED_ROAMING,
    NET_SEARCHING,
    NET_UNKNOWN,
)


# --- parse_device_entry ---


class TestParseDeviceEntry:
    def test_registered_home(self):
        entry = {
            "Device": "dongle0",
            "GSMRegistrationStatus": "Registered, home network",
            "RSSI": "18, -77 dBm",
            "ProviderName": "MTS",
        }
        s = parse_device_entry(entry)
        assert s.registration_status == NET_REGISTERED_HOME
        assert s.signal_rssi == 18
        assert s.operator == "MTS"
        assert s.network_registered is True
        assert s.roaming is False

    def test_roaming(self):
        entry = {
            "Device": "dongle0",
            "GSMRegistrationStatus": "Registered, roaming",
            "RSSI": "12, -89 dBm",
            "ProviderName": "Yettel",
        }
        s = parse_device_entry(entry)
        assert s.registration_status == NET_REGISTERED_ROAMING
        assert s.signal_rssi == 12
        assert s.operator == "Yettel"
        assert s.roaming is True

    def test_not_registered(self):
        entry = {
            "Device": "dongle0",
            "GSMRegistrationStatus": "Not registered",
            "RSSI": "99, -113 dBm",
            "ProviderName": "",
        }
        s = parse_device_entry(entry)
        assert s.registration_status == NET_NOT_REGISTERED
        assert s.network_registered is False
        assert s.signal_rssi == 99

    def test_searching(self):
        s = parse_device_entry({"GSMRegistrationStatus": "Not registered, but searching"})
        assert s.registration_status == NET_SEARCHING
        assert s.network_registered is False

    @pytest.mark.parametrize("raw, expected_rssi, expected_dbm", [
        ("31, >= -51 dBm", 31, -51),
        ("0, <= -113 dBm", 0, -113),
        ("12, -89 dBm", 12, -89),
        ("99, Unknown", 99, None),
    ])
    def test_rssi_formats(self, raw, expected_rssi, expected_dbm):
        status = parse_device_entry({"RSSI": raw})
        assert status.signal_rssi == expected_rssi
        assert status.signal_dbm == expected_dbm

    def test_missing_fields_use_defaults(self):
        s = parse_device_entry({})
        assert s.registration_status == NET_UNKNOWN
        assert s.network_registered is None
        assert s.signal_rssi == 99
        assert s.operator == ""


class TestRssiToDbm:
    def test_zero(self):
        assert rssi_to_dbm(0) == -113

    def test_max(self):
        assert rssi_to_dbm(31) == -51

    def test_mid(self):
        assert rssi_to_dbm(15) == -83

    def test_unknown(self):
        assert rssi_to_dbm(99) is None

    def test_out_of_range(self):
        assert rssi_to_dbm(50) is None


# --- ModemStatus dataclass ---


class TestModemStatus:
    def test_defaults_unknown(self):
        s = ModemStatus()
        assert s.network_registered is None
        assert s.roaming is False
        assert s.signal_dbm is None

    def test_registered_home(self):
        s = ModemStatus(registration_status=NET_REGISTERED_HOME, signal_rssi=18)
        assert s.network_registered is True
        assert s.roaming is False
        assert s.signal_dbm == -77

    def test_roaming(self):
        s = ModemStatus(registration_status=NET_REGISTERED_ROAMING)
        assert s.network_registered is True
        assert s.roaming is True

    def test_to_dict(self):
        s = ModemStatus(registration_status=NET_REGISTERED_HOME, signal_rssi=20, operator="MTS")
        d = s.to_dict()
        assert d["network_registered"] is True
        assert d["roaming"] is False
        assert d["signal_rssi"] == 20
        assert d["signal_dbm"] == -73
        assert d["operator"] == "MTS"


# --- ModemStatusMonitor ---


@pytest.fixture
def ami_client():
    client = MagicMock()
    client.connected = True
    client.authenticated = True
    client.send_action = AsyncMock()
    return client


@pytest.fixture
def monitor(ami_client):
    return ModemStatusMonitor(
        ami_client=ami_client,
        alert_sink=AsyncMock(),
        status_receivers=[],
        poll_interval=1,
    )


def _make_device_event(**overrides):
    """Helper to build a fake DongleDeviceEntry event (dict-like)."""
    base = {
        "Event": "DongleDeviceEntry",
        "Device": "dongle0",
        "State": "Free",
        "GSMRegistrationStatus": "Registered, home network",
        "RSSI": "18, -77 dBm",
        "ProviderName": "MTS",
    }
    base.update(overrides)
    return base


async def test_poll_once_success(monitor, ami_client):
    ami_client.send_action.return_value = [_make_device_event()]
    status = await monitor._poll_once()
    assert status.network_registered is True
    assert status.signal_rssi == 18
    assert status.operator == "MTS"


async def test_poll_once_device_not_found(monitor, ami_client):
    ami_client.send_action.return_value = [_make_device_event(Device="dongle1")]
    with pytest.raises(ValueError, match="dongle0 not found"):
        await monitor._poll_once()


async def test_driver_marker_is_from_configured_device(monitor, ami_client):
    ami_client.send_action.return_value = [{'Device': 'other', 'SMSJournalVersion': '1', 'SMSJournalStatus': 'ok'}]
    with pytest.raises(ValueError, match="dongle0 not found"):
        await monitor._poll_once()
    assert monitor.driver_ingress == {}
    ami_client.send_action.return_value = [{'Device': 'dongle0', 'SMSJournalVersion': '1', 'SMSJournalStatus': 'error'}]
    await monitor._poll_once()
    assert monitor.driver_ingress == {'version': '1', 'status': 'error'}


@pytest.mark.parametrize('container', [lambda item: item, lambda item: [item], lambda item: (item,)])
async def test_driver_marker_reads_case_insensitive_messages(monitor, ami_client, container):
    ami_client.send_action.return_value = container(Message({'device': 'dongle0', 'smsjournalversion': '1',
                                                             'smsjournalstatus': 'ok'}))
    await monitor._poll_once()
    assert monitor.driver_ingress == {'version': '1', 'status': 'ok'}


def _alert_methods(sink):
    return [call.args[0] for call in sink.await_args_list]


async def test_network_change_fires_notification(ami_client):
    sink = AsyncMock()
    monitor = ModemStatusMonitor(ami_client=ami_client, alert_sink=sink, poll_interval=1)

    ami_client.send_action.return_value = [_make_device_event()]
    await monitor.refresh()
    sink.assert_not_awaited()  # Startup success is not a recovery.

    ami_client.send_action.return_value = [
        _make_device_event(GSMRegistrationStatus="Not registered", RSSI="99, -113 dBm", ProviderName=""),
    ]
    await monitor.refresh()
    assert _alert_methods(sink) == ["notify_network_lost"]

    await monitor.refresh()  # Still unregistered: no repeated alert.
    assert _alert_methods(sink) == ["notify_network_lost"]


async def test_network_restore_fires_notification(ami_client):
    sink = AsyncMock()
    monitor = ModemStatusMonitor(ami_client=ami_client, alert_sink=sink, poll_interval=1)

    ami_client.send_action.return_value = [
        _make_device_event(GSMRegistrationStatus="Not registered", RSSI="99, -113 dBm", ProviderName=""),
    ]
    await monitor.refresh()
    assert _alert_methods(sink) == ["notify_network_lost"]

    ami_client.send_action.return_value = [_make_device_event(RSSI="20, -73 dBm")]
    await monitor.refresh()
    assert _alert_methods(sink) == ["notify_network_lost", "notify_network_restored"]
    assert sink.await_args.args[1] == {"operator": "MTS", "signal_rssi": 20}

    await monitor.refresh()  # Still registered: no repeated alert.
    assert _alert_methods(sink) == ["notify_network_lost", "notify_network_restored"]


async def test_ha_sensor_push_called(ami_client):
    ha = MagicMock()
    ha.push_modem_sensor = AsyncMock(return_value=True)

    monitor = ModemStatusMonitor(
        ami_client=ami_client,
        status_receivers=[ha],
        poll_interval=1,
    )
    status = ModemStatus(registration_status=NET_REGISTERED_HOME, signal_rssi=18, operator="MTS")
    await monitor._push_status(status.to_dict())
    ha.push_modem_sensor.assert_called_once()
    call_data = ha.push_modem_sensor.call_args[0][0]
    assert call_data["network_registered"] is True
    assert call_data["operator"] == "MTS"


@pytest.mark.parametrize("failure, reason, present", [
    ("disconnected", "ami_disconnected", None),
    ("unauthenticated", "ami_unauthenticated", None),
    ("missing", "device_missing", False),
    ("device_not_connected", "device_not_connected", True),
    ("timeout", "poll_timeout", None),
    ("error", "poll_failed", None),
])
async def test_failure_invalidates_current_sample_and_recovers(ami_client, failure, reason, present):
    sink = AsyncMock()
    ha = MagicMock(push_modem_sensor=AsyncMock(return_value=True))
    monitor = ModemStatusMonitor(ami_client, status_receivers=[ha], alert_sink=sink)
    ami_client.send_action.return_value = [_make_device_event()]
    await monitor.refresh()
    assert monitor.readiness["ready"] is True
    old = monitor.status["last_sample"]
    sink.assert_not_awaited()  # Startup success is not a recovery.
    if failure == "disconnected":
        ami_client.connected = False
    elif failure == "unauthenticated":
        ami_client.authenticated = False
    elif failure == "missing":
        ami_client.send_action.return_value = []
    elif failure == "device_not_connected":
        ami_client.send_action.return_value = [_make_device_event(State="Not connected")]
    elif failure == "timeout":
        ami_client.send_action.side_effect = TimeoutError("private modem response")
    else:
        ami_client.send_action.return_value = [{"Response": "Error", "Message": "private"}]
    await monitor.refresh()
    state = monitor.status
    assert state["availability"] == "unavailable"
    assert state["availability_reason"] == reason
    assert state["device_present"] is present
    assert state["network_registered"] is None
    assert state["signal_rssi"] is None
    assert state["modem_status_updated"] is None
    assert state["last_sample"] == old
    assert monitor.readiness["ready"] is False
    assert ha.push_modem_sensor.call_args.args[0] == state
    sink.assert_awaited_once_with("notify_modem_unavailable", {"reason": reason}, ("unavailable", None))
    await monitor.refresh()
    assert sink.await_count == 1
    ami_client.connected = ami_client.authenticated = True
    ami_client.send_action.side_effect = None
    ami_client.send_action.return_value = [_make_device_event()]
    await monitor.refresh()
    assert monitor.readiness["ready"] is True
    assert sink.call_args.args[0] == "notify_network_restored"


@pytest.mark.parametrize("device_state", ["Not connected", "Stopped"])
@pytest.mark.parametrize("previous_state", [None, ("available", None)])
async def test_disconnected_device_alert_survives_monitor_restart(ami_client, db, device_state, previous_state):
    db.backends = ("telegram",)  # Queue only; no delivery transport is run.
    if previous_state is not None:
        # An older monitor classified the unopened port as available with unknown registration.
        await db.enqueue_monitor_alert("notify_modem_unavailable", {"reason": "registration_unknown"}, previous_state)
    sink = AsyncMock(wraps=db.enqueue_monitor_alert)
    load_state = AsyncMock(wraps=db.monitor_alert_state)
    ami_client.send_action.return_value = [_make_device_event(
        State=device_state, GSMRegistrationStatus="Unknown", RSSI="0, <= -113 dBm", ProviderName="NONE",
        SMSJournalVersion="1", SMSJournalStatus="ok",
    )]

    async def start_once():
        monitor = ModemStatusMonitor(ami_client, alert_sink=sink, load_alert_state=load_state)
        with patch("phonelet.modem_status.asyncio.sleep", side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await monitor.run()
        return monitor

    monitor = await start_once()
    state = await monitor.refresh()
    assert state["availability"] == "unavailable"
    assert state["availability_reason"] == "device_not_connected"
    assert state["device_present"] is True
    assert state["network_registered"] is None
    assert state["signal_rssi"] is None
    assert state["last_sample"] is None
    assert monitor.readiness["ready"] is False
    sink.assert_awaited_once_with(
        "notify_modem_unavailable", {"reason": "device_not_connected"}, ("unavailable", None),
    )
    assert await db.monitor_alert_state() == ("unavailable", None)
    queued = 1 + int(previous_state is not None)
    assert (await db.outbox_stats())[0]["count"] == queued

    await db.close()
    await db.init()
    restarted = await start_once()
    await restarted.refresh()
    assert load_state.await_count == 2
    assert sink.await_count == 1
    assert (await db.outbox_stats())[0]["count"] == queued
    assert restarted.readiness["ready"] is False

    ami_client.send_action.return_value = [_make_device_event()]
    await restarted.refresh()
    await restarted.refresh()
    assert restarted.readiness["ready"] is True
    assert sink.await_count == 2
    sink.assert_awaited_with(
        "notify_network_restored", {"operator": "MTS", "signal_rssi": 18}, ("available", True),
    )
    assert await db.monitor_alert_state() == ("available", True)
    assert (await db.outbox_stats())[0]["count"] == queued + 1


async def test_unauthenticated_client_never_trusts_transport():
    from types import SimpleNamespace
    ami = SimpleNamespace(connected=True, authenticated=False, send_action=AsyncMock())
    monitor = ModemStatusMonitor(ami)
    await monitor.refresh()
    assert monitor.status["ami_authenticated"] is False
    ami.send_action.assert_not_awaited()


async def test_status_immediately_invalidates_stale_or_disconnected_sample(monitor, ami_client):
    ami_client.send_action.return_value = [_make_device_event()]
    await monitor.refresh()
    with patch("phonelet.modem_status.time.monotonic", return_value=monitor._sample_time + 100):
        assert monitor.status["availability_reason"] == "stale"
        assert monitor.status["network_registered"] is None
        assert monitor.status["last_sample"]["network_registered"] is True
        assert monitor.readiness["ready"] is False
    ami_client.connected = False
    assert monitor.status["network_registered"] is None
    assert monitor.status["availability_reason"] == "ami_disconnected"


async def test_startup_unknown_and_initial_loss(ami_client):
    sink = AsyncMock()
    monitor = ModemStatusMonitor(ami_client, alert_sink=sink)
    assert monitor.status["availability"] == "unknown"
    assert monitor.status["last_sample"] is None
    assert monitor.status["network_registered"] is None
    ami_client.send_action.return_value = [_make_device_event(GSMRegistrationStatus="Not registered, but searching")]
    await monitor.refresh()
    assert monitor.status["registration_status"] == NET_SEARCHING
    sink.assert_awaited_once_with("notify_network_lost", {"operator": "MTS", "signal_rssi": 18}, ("available", False))


async def test_startup_login_latency_is_not_an_outage(ami_client):
    # AMI connects concurrently with the monitor; a healthy start sends no alert.
    sink = AsyncMock()
    ami_client.connected = ami_client.authenticated = False
    ami_client.send_action.return_value = [_make_device_event()]
    monitor = ModemStatusMonitor(ami_client, alert_sink=sink, poll_interval=1)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.15)
    assert ami_client.send_action.await_count == 0
    ami_client.connected = ami_client.authenticated = True
    for _ in range(50):
        if monitor.readiness["ready"]:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert monitor.readiness["ready"] is True
    sink.assert_not_awaited()


async def test_startup_without_ami_alerts_after_one_interval(ami_client):
    sink = AsyncMock()
    ami_client.connected = ami_client.authenticated = False
    monitor = ModemStatusMonitor(ami_client, alert_sink=sink, poll_interval=0.2)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.1)
    sink.assert_not_awaited()
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sink.assert_awaited_once_with("notify_modem_unavailable", {"reason": "ami_disconnected"}, ("unavailable", None))


async def test_unknown_registration_is_not_false_unregistered(ami_client):
    sink = AsyncMock()
    monitor = ModemStatusMonitor(ami_client, alert_sink=sink)
    ami_client.send_action.return_value = [_make_device_event(GSMRegistrationStatus="Unexpected driver state")]
    await monitor.refresh()
    assert monitor.status["network_registered"] is None
    assert monitor.status["device_present"] is True
    assert monitor.readiness["ready"] is False
    sink.assert_awaited_once_with("notify_modem_unavailable", {"reason": "registration_unknown"}, ("available", None))


async def test_alert_persistence_failure_retries_original_before_recovery(ami_client):
    sink = AsyncMock(side_effect=[OSError("disk full"), False, None, None])
    monitor = ModemStatusMonitor(ami_client, alert_sink=sink)
    ami_client.connected = False
    await monitor.refresh()
    assert len(monitor._pending_alerts) == 1
    ami_client.connected = True
    ami_client.send_action.return_value = [_make_device_event()]
    await monitor.refresh()
    assert monitor.readiness["ready"] is True
    assert len(monitor._pending_alerts) == 2
    await monitor.refresh()
    assert not monitor._pending_alerts
    assert [call.args[0] for call in sink.await_args_list] == [
        "notify_modem_unavailable", "notify_modem_unavailable", "notify_modem_unavailable", "notify_network_restored",
    ]


async def test_failed_ha_sensor_retries_without_invalidating_sample(ami_client):
    ha = MagicMock(push_modem_sensor=AsyncMock(side_effect=[False, RuntimeError("secret"), True]))
    monitor = ModemStatusMonitor(ami_client, status_receivers=[ha])
    ami_client.send_action.return_value = [_make_device_event()]
    for _ in range(3):
        await monitor.refresh()
        assert monitor.readiness["ready"] is True
    assert ha.push_modem_sensor.await_count == 3


@pytest.mark.parametrize('failure', ['false', 'exception', 'timeout'])
async def test_status_receivers_fail_independently_and_retry_next_poll(ami_client, monkeypatch, caplog, failure):
    monkeypatch.setattr('phonelet.modem_status._DELIVERY_TIMEOUT', 0.01)

    async def fail(status):
        if failure == 'exception':
            raise RuntimeError('synthetic failure')
        if failure == 'timeout':
            await asyncio.Event().wait()
        return False

    first = MagicMock(push_modem_sensor=AsyncMock(side_effect=fail))
    second = MagicMock(push_modem_sensor=AsyncMock(return_value=True))
    monitor = ModemStatusMonitor(ami_client, status_receivers=[first, second])
    ami_client.send_action.return_value = [_make_device_event()]
    status = await monitor.refresh()
    assert monitor.readiness['ready'] is True
    first.push_modem_sensor.assert_awaited_once_with(status)
    second.push_modem_sensor.assert_awaited_once_with(status)
    assert 'will retry next poll' in caplog.text
    assert any(r.levelname == 'WARNING' for r in caplog.records)

    first.push_modem_sensor.side_effect = None
    first.push_modem_sensor.return_value = True
    await monitor.refresh()
    assert first.push_modem_sensor.await_count == second.push_modem_sensor.await_count == 2


async def test_poll_timeout_and_cancellation(ami_client, monkeypatch):
    async def blocked(_):
        await asyncio.Event().wait()
    ami_client.send_action.side_effect = blocked
    monitor = ModemStatusMonitor(ami_client)
    monkeypatch.setattr("phonelet.modem_status._CMD_TIMEOUT", 0.01)
    await monitor.refresh()
    assert monitor.status["availability_reason"] == "poll_timeout"
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_poll_single_message(monitor, ami_client):
    ami_client.send_action.return_value = _make_device_event()
    await monitor.refresh()
    assert monitor.readiness["ready"] is True


async def test_run_polls_before_sleep(monitor, ami_client):
    ami_client.send_action.return_value = [_make_device_event()]
    with patch("phonelet.modem_status.asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await monitor.run()
    assert monitor.readiness["ready"] is True


async def test_slow_sensor_and_alert_timeout_do_not_drop_transition(ami_client, monkeypatch):
    monkeypatch.setattr("phonelet.modem_status._DELIVERY_TIMEOUT", 0.01)

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    sink = AsyncMock(side_effect=blocked)
    ha = MagicMock(push_modem_sensor=AsyncMock(side_effect=blocked))
    monitor = ModemStatusMonitor(ami_client, status_receivers=[ha], alert_sink=sink)
    ami_client.connected = False
    await monitor.refresh()
    assert monitor.status["network_registered"] is None
    assert len(monitor._pending_alerts) == 1
    sink.side_effect = None
    ha.push_modem_sensor.side_effect = None
    ha.push_modem_sensor.return_value = True
    await monitor.refresh()
    assert not monitor._pending_alerts
    assert sink.await_count == 2


async def test_refresh_serializes_overlapping_callers(ami_client):
    active = maximum = 0

    async def poll(_):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return [_make_device_event()]

    ami_client.send_action.side_effect = poll
    monitor = ModemStatusMonitor(ami_client)
    await asyncio.gather(monitor.refresh(), monitor.refresh())
    assert maximum == 1


async def test_driver_journal_error_alerts_once_per_episode(ami_client):
    """SMSJournalStatus other than ok for three answered polls queues one HEALTH alert (C3)."""
    sink = AsyncMock()
    monitor = ModemStatusMonitor(ami_client=ami_client, alert_sink=sink, poll_interval=1)

    def journal(status):
        ami_client.send_action.return_value = [_make_device_event(SMSJournalVersion="1", SMSJournalStatus=status)]

    def health():
        return [call.args[1] for call in sink.await_args_list if call.args[0] == "notify_health"]

    journal("error")
    await monitor.refresh()
    await monitor.refresh()
    assert health() == []  # the driver retries its journal every 30 s
    ami_client.authenticated = False  # an unanswered poll neither counts nor resets
    await monitor.refresh()
    ami_client.authenticated = True
    await monitor.refresh()
    [alert] = health()
    assert "SMS journal error" in alert["message"] and "3 polls" in alert["message"]
    for _ in range(3):
        await monitor.refresh()
    assert len(health()) == 1  # still the same episode
    journal("ok")
    await monitor.refresh()
    journal("error")
    for _ in range(3):
        await monitor.refresh()
    assert len(health()) == 2  # a new episode after the journal recovered


async def test_driver_without_journal_status_raises_no_journal_alert(ami_client):
    sink = AsyncMock()
    monitor = ModemStatusMonitor(ami_client=ami_client, alert_sink=sink, poll_interval=1)
    ami_client.send_action.return_value = [_make_device_event()]
    for _ in range(5):
        await monitor.refresh()
    sink.assert_not_awaited()
