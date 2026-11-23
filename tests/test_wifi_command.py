"""The Wi-Fi command: its codec, WifiCommand in the worker, its loop and at start-up, and its logs."""
import asyncio
import logging

import pytest

from phonelet import main
from phonelet.config import Settings
from phonelet.logging_privacy import install_log_privacy
from phonelet.netplan_wifi import NetplanWifi, Outcome, render_netplan
from phonelet.sms_commands import envelope
from phonelet.sms_commands.commands import wifi
from phonelet.sms_commands.commands.wifi import WifiCommand
from phonelet.sms_commands.worker import by_code, command_loop, process_commands
from tests.helpers import (StopLoop, command_rows, health_by_event, health_messages, journal_record, local_paths,
                           stop_after)
from tests.test_lifespan import OfflineAmi, QuietMonitor
from tests.test_netplan_wifi import (APPLY, NEW_PASSWORD, NEW_SSID, OLD_FILE, OLD_PASSWORD, OLD_SSID,
                                     FailingNetplanWrites, interrupted_change, raising_runner)
from tests.test_netplan_wifi import system  # noqa: F401 (fixture)
from tests.test_sms_commands import (BAD_NETWORKS, FINGERPRINT, KEY, KEY_TEXT, LONG, PASSPHRASE, PASSWORD, SSID,
                                     SCTS, STAMP, VECTOR, board_clock, receive_command, wifi_sms)
from tests.test_sms_commands import db  # noqa: F401 (fixture)

ROLLED_BACK_REPORT = 'Wi-Fi: interrupted change rolled back, previous network connected: yes'  # pinned text


def wifi_args(ssid=SSID.encode(), password=PASSWORD.encode(), ssid_len=None):
    """Packed Wi-Fi arguments built by hand, valid or not."""
    return bytes([len(ssid) if ssid_len is None else ssid_len]) + ssid + password


# --- the codec -------------------------------------------------------------------------------

@pytest.mark.parametrize('ssid,password,field', BAD_NETWORKS)
def test_tools_and_board_reject_the_same_networks(ssid, password, field):
    for check in (lambda: wifi.pack_args(ssid, password),
                  lambda: wifi.unpack_args(wifi_args(ssid.encode(), password.encode()))):
        with pytest.raises(ValueError) as caught:
            check()
        assert str(caught.value).startswith(f'{field} must be')
        assert password not in str(caught.value) and '0x' not in str(caught.value)


@pytest.mark.parametrize('args,error', [
    pytest.param(b'', 'No SSID length', id='no-length-byte'),
    pytest.param(wifi_args(ssid=b'\xff\xfe'), 'not UTF-8', id='ssid-not-utf8'),
    pytest.param(wifi_args(ssid_len=40), 'Truncated SSID', id='ssid-truncated'),
])
def test_malformed_wifi_bytes_are_rejected(args, error):
    with pytest.raises(ValueError, match=error) as caught:
        wifi.unpack_args(args)
    assert '0x' not in str(caught.value)


@pytest.mark.parametrize('ssid,password', [('Кафе "x\\y": #1', 'pa"ss\\word: #'), ('😀' * 8, ''), ('a', 'x' * 63),
                                           ('n', ' ' * 8), ('Ж' * 16, '~' * 63)],
                         ids=['quotes-and-backslashes', 'emoji-open-network', 'longest-password',
                              'spaces-only-password', 'longest-ssid-and-password'])
def test_wifi_arguments_round_trip(ssid, password):
    decoded = envelope.decode(KEY, wifi_sms(ssid, password, STAMP))
    assert decoded.stamp_ms == STAMP and wifi.unpack_args(decoded.packed_args) == (ssid, password)


def test_longest_wifi_command_needs_two_sms_and_about_80_bytes_fit_one():
    assert len(LONG) == 4 + 176  # 1 + 12 + (6 + 1 + 1 + 32 + 63) + 16 = 132 bytes of base64url
    assert envelope.sms_parts(LONG) == 2
    assert envelope.sms_parts(wifi_sms('x' * 32, 'y' * 48, STAMP)) == 1  # 80 bytes
    assert envelope.sms_parts(wifi_sms('x' * 32, 'y' * 49, STAMP)) == 2


# --- in the worker ---------------------------------------------------------------------------

class FakeNetplan:
    def __init__(self, outcome=None):
        self.applied = []
        self.outcome = outcome or Outcome()

    def apply(self, ssid, password):
        self.applied.append((ssid, password))
        return self.outcome


async def receive_wifi(db, ssid, password, stamp):
    return await receive_command(db, wifi_sms(ssid, password, stamp))


async def test_accepted_command_is_applied_and_reported(db):
    db.backends = ('telegram',)
    row = await receive_command(db, VECTOR)
    fake = FakeNetplan()
    await process_commands(db, KEY, by_code([WifiCommand(fake)]), clock=board_clock)
    assert fake.applied == [(SSID, PASSWORD)]
    [done] = await command_rows(db)
    assert (done['status'], done['name'], done['result'], done['ciphertext']) == ('done', 'wifi', 'connected', None)
    assert await db.sms_command_last_stamp() == STAMP
    assert await health_by_event(db) == {f"command:{row['id']}": 'Wi-Fi: connected to "Example-Net"'}


async def test_failed_change_is_reported_with_the_previous_network(db):
    db.backends = ('telegram',)
    row = await receive_command(db, VECTOR)
    outcome = Outcome('connect_timeout', previous_connected=True)
    await process_commands(db, KEY, by_code([WifiCommand(FakeNetplan(outcome))]), clock=board_clock)
    assert (await command_rows(db))[0]['result'] == 'failed:connect_timeout; restored, previous network connected=yes'
    assert await health_by_event(db) == {
        f"command:{row['id']}": 'Wi-Fi: "Example-Net" did not connect (connect_timeout); '
                                'previous configuration restored, previous network connected: yes'}
    assert await db.sms_command_last_stamp() == STAMP  # taken: a replay of it stays a replay


async def test_bad_wifi_arguments_are_rejected_as_bad_format(db):
    db.backends = ('telegram',)
    row = await receive_command(db, envelope.encode(KEY, wifi.CODE, wifi_args(password=b'short12'), STAMP))
    fake = FakeNetplan()
    await process_commands(db, KEY, by_code([WifiCommand(fake)]), clock=board_clock)
    [done] = await command_rows(db)
    assert (done['status'], done['name'], done['result']) == ('done', 'wifi', 'rejected:bad_format')
    assert await health_by_event(db) == {f"command:{row['id']}": 'Wi-Fi command rejected: bad_format'}
    assert fake.applied == [] and await db.sms_command_last_stamp() is None


# --- the loop --------------------------------------------------------------------------------

def loop(db, netplan):
    return command_loop(db, KEY, by_code([WifiCommand(netplan)]), 5, clock=board_clock)


async def test_loop_retries_a_failed_rollback_and_reports_it_once(db, system, monkeypatch):
    db.backends = ('telegram',)
    interrupted_change(system)
    failing = FailingNetplanWrites(monkeypatch)
    command = WifiCommand(system.netplan())
    await command.recover_at_start()  # as Runtime.lifespan does, before the database opens
    assert command.netplan.marker.exists()  # that rollback failed
    await receive_wifi(db, OLD_SSID, OLD_PASSWORD, STAMP)

    def between(passes):
        if passes == 2:
            failing.on = False
    stop_after(monkeypatch, 4, between)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([command]), 5, clock=board_clock)
    [row] = await command_rows(db)
    assert row['result'] == 'failed:rollback_pending; netplan unchanged'
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []
    assert await health_messages(db) == [
        'Wi-Fi: interrupted change not rolled back yet; retrying, check the board',
        f'Wi-Fi: "{OLD_SSID}" not applied (rollback_pending); netplan unchanged',
        ROLLED_BACK_REPORT,
    ]


async def test_loop_retries_a_rollback_that_failed_during_a_command(db, system, monkeypatch):
    db.backends = ('telegram',)
    FailingNetplanWrites(monkeypatch, lambda data: data == OLD_FILE, times=1)
    # A network that does not connect: the change times out, its rollback write fails once.
    await receive_wifi(db, NEW_SSID, NEW_PASSWORD, STAMP)
    stop_after(monkeypatch, 2)
    with pytest.raises(StopLoop):
        await loop(db, system.netplan())
    [command] = await command_rows(db)
    assert command['result'] == 'failed:connect_timeout; rollback failed'
    # No further command: the second pass alone restored the file.
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []
    assert await health_messages(db) == [
        f'Wi-Fi: "{NEW_SSID}" did not connect (connect_timeout); rollback failed, check the board',
        ROLLED_BACK_REPORT,
    ]


async def test_rollback_is_retried_while_the_result_waits_to_be_saved(db, system, monkeypatch):
    db.backends = ('telegram',)
    real_finish = db.finish_sms_command
    restored = []

    async def finish(*args, **kwargs):  # the database refuses the result for two passes
        if len(restored) < 2:
            raise RuntimeError('database is locked')
        return await real_finish(*args, **kwargs)

    def between(passes):
        restored.append(system.file.read_bytes() == OLD_FILE)
    FailingNetplanWrites(monkeypatch, lambda data: data == OLD_FILE, times=1)
    monkeypatch.setattr(db, 'finish_sms_command', finish)
    # A network that does not connect: the change times out, its rollback write fails once.
    await receive_wifi(db, NEW_SSID, NEW_PASSWORD, STAMP)
    stop_after(monkeypatch, 3, between)
    with pytest.raises(StopLoop):
        await loop(db, system.netplan())
    assert restored == [False, True]  # the second pass rolled back, its result still not saved
    [row] = await command_rows(db)
    assert row['result'] == 'failed:connect_timeout; rollback failed' and list(system.state_dir.iterdir()) == []
    assert await health_messages(db) == [
        ROLLED_BACK_REPORT,
        f'Wi-Fi: "{NEW_SSID}" did not connect (connect_timeout); rollback failed, check the board',
    ]


async def test_rollback_finished_by_a_later_change_is_reported_once(db, system, monkeypatch):
    db.backends = ('telegram',)
    # The first change's rollback and the next tick's retry fail.
    failing = FailingNetplanWrites(monkeypatch, lambda data: data == OLD_FILE, times=2)
    # A network that does not connect: the change times out and its rollback fails.
    await receive_wifi(db, NEW_SSID, NEW_PASSWORD, STAMP)
    passes = []

    async def sleep(seconds):
        passes.append(seconds)
        if len(passes) == 1:  # the second change arrives after the first one ended
            await receive_wifi(db, OLD_SSID, OLD_PASSWORD, STAMP + 1)
        if len(passes) == 4:  # one pass more: the success is not reported again
            raise StopLoop
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    with pytest.raises(StopLoop):
        await loop(db, system.netplan())
    assert failing.refused == 2
    assert [r['result'] for r in await command_rows(db)] == ['failed:connect_timeout; rollback failed', 'connected']
    assert list(system.state_dir.iterdir()) == []
    # The second change finished the rollback before it applied: that success is reported too.
    assert await health_messages(db) == [
        f'Wi-Fi: "{NEW_SSID}" did not connect (connect_timeout); rollback failed, check the board',
        'Wi-Fi: interrupted change not rolled back yet; retrying, check the board',
        f'Wi-Fi: connected to "{OLD_SSID}"',
        'Wi-Fi: the earlier unfinished change was rolled back before the last command ran',
    ]


async def test_restart_mid_change_rolls_back_and_ends_the_command_interrupted(db, system, monkeypatch):
    db.backends = ('telegram',)
    command = await receive_wifi(db, NEW_SSID, NEW_PASSWORD, STAMP)
    await db.begin_sms_command(command['id'], STAMP, 'wifi')
    interrupted_change(system)
    stop_after(monkeypatch, 2)
    with pytest.raises(StopLoop):
        await loop(db, system.netplan())
    [done] = await command_rows(db)
    assert (done['status'], done['result'], done['ciphertext']) == ('done', 'interrupted', None)
    assert system.file.read_bytes() == OLD_FILE and system.count(APPLY) == 1  # the rollback's only
    assert await health_messages(db) == [
        ROLLED_BACK_REPORT, 'Wi-Fi: change interrupted by a restart; not repeated, network left as it ended']


async def test_start_up_rollback_that_raises_is_finished_by_the_first_tick(db, system, monkeypatch):
    db.backends = ('telegram',)
    interrupted_change(system)
    netplan = system.netplan()
    netplan.runner = raising_runner(system, lambda args: args == APPLY)
    command = WifiCommand(netplan)
    await command.recover_at_start()  # logs, does not raise: startup goes on
    assert netplan.marker.exists()
    netplan.runner = system.run
    stop_after(monkeypatch, 1)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([command]), 5, clock=board_clock)
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []
    assert await health_messages(db) == [ROLLED_BACK_REPORT]


async def test_a_rollback_report_the_database_refuses_is_queued_by_the_next_tick(db, system, monkeypatch):
    db.backends = ('telegram',)
    interrupted_change(system)
    command = WifiCommand(system.netplan())
    await command.recover_at_start()
    real = db.enqueue_notification

    async def locked(*args, **kwargs):
        monkeypatch.setattr(db, 'enqueue_notification', real)  # only the first one fails
        raise RuntimeError('database is locked')
    monkeypatch.setattr(db, 'enqueue_notification', locked)
    with pytest.raises(RuntimeError):
        await command.tick(db)
    await command.tick(db)
    await command.tick(db)  # queued once, not again
    assert await health_messages(db) == [ROLLED_BACK_REPORT]
    assert system.count(APPLY) == 1  # rolled back once


# --- at start-up -----------------------------------------------------------------------------

def app_config(tmp_path, system, **settings):
    paths = {**local_paths(tmp_path / 'app'), 'wifi_netplan_file': str(system.file),
             'wifi_state_dir': str(system.state_dir)}
    return Settings(**paths, api_key='fake', ami_secret='fake', **settings)


@pytest.fixture
def offline(monkeypatch, system):
    monkeypatch.setattr(main, 'AmiClient', OfflineAmi)
    monkeypatch.setattr(main, 'ModemStatusMonitor', QuietMonitor)
    monkeypatch.setattr(wifi.shutil, 'which', lambda program: f'/fake/bin/{program}')
    # The real class with the settings the factory gives it; commands and clock are fake.
    monkeypatch.setattr(wifi, 'NetplanWifi', lambda *settings: NetplanWifi(
        *settings, runner=system.run, sleep=system.sleep, clock=lambda: system.now, units_dir=system.units_dir))


@pytest.mark.parametrize('programs', [(), ('netplan',), ('wpa_cli',), ('netplan', 'wpa_cli')],
                         ids=['neither', 'no-wpa-cli', 'no-netplan', 'both'])
async def test_host_programs_control_wifi_availability_and_execution(
        tmp_path, system, offline, monkeypatch, caplog, programs):
    monkeypatch.setattr(wifi.shutil, 'which', lambda program: f'/fake/bin/{program}' if program in programs else None)
    config = app_config(tmp_path, system, sms_command_key=KEY_TEXT)
    caplog.set_level(logging.INFO)
    runtime = main.Runtime(config)
    available = len(programs) == 2
    assert set(runtime.commands) == ({wifi.CODE} if available else set())
    assert f'Available SMS commands: {"wifi" if available else "none"}' in caplog.text
    if available:
        command = runtime.commands[wifi.CODE]
        assert isinstance(command, WifiCommand)
        assert command.netplan.netplan_file == system.file
        assert command.netplan.state_dir == system.state_dir
        assert command.netplan.interface == config.wifi_interface
        assert command.netplan.connect_timeout == config.wifi_connect_timeout
    runtime.db.backends = ('telegram',)
    await runtime.db.init()
    try:
        received = await receive_wifi(runtime.db, NEW_SSID, NEW_PASSWORD, STAMP)
        system.networks[NEW_SSID] = NEW_PASSWORD
        await process_commands(runtime.db, runtime.command_key, runtime.commands, clock=board_clock)
        [done] = await command_rows(runtime.db)
        assert done['result'] == ('connected' if available else 'rejected:unknown_command')
        assert done['ciphertext'] is None
        if available:
            assert system.count(APPLY) == 1
        else:
            assert system.calls == [] and system.file.read_bytes() == OLD_FILE
            assert not system.state_dir.exists()
            assert await runtime.db.sms_command_last_stamp() is None
        messages = await health_messages(runtime.db)
        assert len(messages) == 1
        secrets = (KEY_TEXT, KEY.hex(), received['ciphertext'], NEW_PASSWORD)
        assert all(secret not in message for message in messages for secret in secrets)
        if not available:
            assert messages == ['SMS command rejected: unknown_command']
    finally:
        await runtime.db.close()
    assert all(secret not in caplog.text for secret in secrets)


async def test_start_rolls_back_even_if_the_database_does_not_open(tmp_path, system, offline, monkeypatch):
    interrupted_change(system)

    async def broken(self):
        raise RuntimeError('database does not open')
    monkeypatch.setattr(main.Database, 'init', broken)
    app = main.create_application(app_config(tmp_path, system))
    with pytest.raises(RuntimeError, match='does not open'):
        async with app.router.lifespan_context(app):
            pytest.fail('must not yield')
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []


async def test_start_rolls_back_before_the_database_and_reports_after(tmp_path, system, offline, monkeypatch):
    interrupted_change(system)
    calls_at_init, queued = [], []
    reported = asyncio.Event()
    real_init, real_enqueue = main.Database.init, main.Database.enqueue_notification

    async def init(self):
        calls_at_init.append(list(system.calls))
        await real_init(self)

    async def enqueue(self, method, payload, event_key=None):
        queued.append(payload['message'])
        reported.set()
        return await real_enqueue(self, method, payload, event_key)
    monkeypatch.setattr(main.Database, 'init', init)
    monkeypatch.setattr(main.Database, 'enqueue_notification', enqueue)
    app = main.create_application(app_config(tmp_path, system))
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(reported.wait(), 2)
    assert APPLY in calls_at_init[0]  # rolled back before the database opened
    assert queued == [ROLLED_BACK_REPORT]
    assert system.file.read_bytes() == OLD_FILE and system.count(APPLY) == 1


async def test_start_rejects_an_old_command_from_the_journal_as_stale(tmp_path, system, offline, monkeypatch):
    # The journal replayed into an empty database, with the board's real clock: weeks after the SMS came.
    config = app_config(tmp_path, system, sms_command_key=KEY_TEXT, ingress_poll_interval=0.05)
    (tmp_path / 'app' / 'sms' / 'a.sms').write_bytes(journal_record(VECTOR, stamp=SCTS))
    finished, done = [], asyncio.Event()
    real_finish = main.Database.finish_sms_command

    async def finish(self, command_id, result, message, name=None):
        finished.append(result)
        done.set()
        return await real_finish(self, command_id, result, message, name)
    monkeypatch.setattr(main.Database, 'finish_sms_command', finish)
    app = main.create_application(config)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(done.wait(), 5)
    assert finished == ['rejected:stale'] and system.calls == []  # netplan untouched


async def test_start_log_names_the_commands_and_the_key_fingerprint(tmp_path, caplog, offline):
    caplog.set_level(logging.INFO, logger='phonelet.main')
    main.Runtime(Settings(**local_paths(tmp_path / 'on'), sms_command_key=KEY_TEXT, api_key='fake',
                          ami_secret='fake'))
    assert 'Available SMS commands: wifi' in caplog.text
    assert f'SMS commands on: key fingerprint {FINGERPRINT}' in caplog.text and KEY_TEXT not in caplog.text
    main.Runtime(Settings(**local_paths(tmp_path / 'off'), api_key='fake', ami_secret='fake'))
    assert 'SMS commands off: PHONELET_SMS_COMMAND_KEY not set' in caplog.text


# --- logs ------------------------------------------------------------------------------------

async def test_logs_never_carry_secrets(db, system, caplog, monkeypatch):
    # The production policy (main installs it), with no secret registered: nothing may depend on redaction.
    install_log_privacy()
    caplog.set_level(logging.DEBUG)
    db.backends = ('telegram',)
    commands = by_code([WifiCommand(system.netplan())])
    texts = []

    async def receive(ssid, password, stamp):
        texts.append((await receive_wifi(db, ssid, password, stamp))['ciphertext'])

    for password, stamp in [(NEW_PASSWORD, STAMP), ('wrong-example-pw', STAMP + 1), (NEW_PASSWORD, STAMP)]:
        await receive(NEW_SSID, password, stamp)
    system.networks[NEW_SSID] = NEW_PASSWORD
    await process_commands(db, KEY, commands, clock=board_clock)
    # A failure inside the change logs a traceback whose exception text carries the password.
    await receive(OLD_SSID, OLD_PASSWORD, STAMP + 2)
    FailingNetplanWrites(monkeypatch, lambda data: data == render_netplan('wlan0', OLD_SSID, OLD_PASSWORD).encode())
    await process_commands(db, KEY, commands, clock=board_clock)
    # And an unexpected error in the check (here: a broken SMSC stamp).
    await receive(OLD_SSID, OLD_PASSWORD, STAMP + 3)
    await db._conn.execute("UPDATE sms SET timestamp='broken' WHERE id=(SELECT sms_id FROM sms_commands "
                           "WHERE status='pending')")
    await db._conn.commit()
    await process_commands(db, KEY, commands, clock=board_clock)
    assert [row['result'] for row in await command_rows(db)] == [
        'connected', 'failed:connect_timeout; restored, previous network connected=yes', 'rejected:replay',
        'failed:write_failed; restored, previous network connected=yes', 'rejected:error']
    logged = caplog.text  # the privacy factory has already folded the arguments into the message
    # repr: one line, whatever the SSID.
    assert f'SMS command accepted: sms=1 command=wifi ssid={NEW_SSID!r}\n' in logged
    assert 'reason=replay' in logged and 'reason=error' in logged
    for secret in (NEW_PASSWORD, 'wrong-example-pw', OLD_PASSWORD, KEY_TEXT, PASSPHRASE, *texts,
                   *(t[4:40] for t in texts)):
        assert secret not in logged
    assert repr(KEY) not in logged
