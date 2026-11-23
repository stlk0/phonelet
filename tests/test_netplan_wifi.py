"""NetplanWifi: a netplan change with rollback, on temp directories with a fake command runner."""
import json
import logging
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from phonelet import netplan_wifi
from phonelet.netplan_wifi import NetplanWifi, Outcome, render_netplan, run_command, unescape_wpa

# Invented networks only.
OLD_SSID, OLD_PASSWORD = 'Old-Example', 'old-example-pass'
OLD_FILE = render_netplan('wlan0', OLD_SSID, OLD_PASSWORD).encode() + b'# written by hand\n'
NEW_SSID, NEW_PASSWORD = 'New "Example": #1 Ж', 'new-example-pass'
NETPLAN_NAME = '30-wifis-dhcp.yaml'
SUPPLICANT_UNIT = 'netplan-wpa-wlan0.service'
RESTART = ['systemctl', 'restart', SUPPLICANT_UNIT]
STOP = ['systemctl', 'stop', SUPPLICANT_UNIT]
APPLY = ['netplan', 'apply']
STATUS = ['wpa_cli', '-i', 'wlan0', 'status']
ADDRESSES = ['ip', '-j', '-4', 'addr', 'show', 'dev', 'wlan0']
ROUTES = ['ip', '-j', '-4', 'route', 'show', 'default', 'dev', 'wlan0']
_QUOTED = r'"(?:[^"\\]|\\.)*"'


def printf_encode(raw):
    """wpa_supplicant's printf_encode, as wpa_cli prints an SSID."""
    special = {0x22: '\\"', 0x5c: '\\\\', 0x1b: '\\e', 0x0a: '\\n', 0x0d: '\\r', 0x09: '\\t'}
    return ''.join(special.get(b) or (chr(b) if 32 <= b <= 126 else f'\\x{b:02x}') for b in raw)


def parse_netplan(text):
    """(ssid, password or None) of a file render_netplan wrote."""
    match = re.search(rf'access-points:\n +({_QUOTED}):(?: {{}}|\n +password: ({_QUOTED}))\n', text)
    return json.loads(match.group(1)), json.loads(match.group(2)) if match.group(2) else None


class FakeSystem:
    """netplan, systemctl, wpa_cli and ip over a temp netplan file and a set of reachable networks.

    Like netplan 1.1.2, `netplan apply` alone leaves the running supplicant on its old
    network: only `systemctl restart netplan-wpa-wlan0.service` takes the new file.
    """
    def __init__(self, tmp_path, networks=None, apply_codes=(), restart_codes=()):
        self.file = tmp_path / 'netplan' / NETPLAN_NAME
        self.file.parent.mkdir()
        self.state_dir = tmp_path / 'state' / 'wifi'
        self.units_dir = tmp_path / 'run-systemd'  # stands for /run/systemd/system
        self.units_dir.mkdir()
        self.networks = {OLD_SSID: OLD_PASSWORD} if networks is None else networks
        self.apply_codes = list(apply_codes)  # return codes of successive netplan apply runs; 0 after
        self.restart_codes = list(restart_codes)  # the same for systemctl
        self.configured = self.associated = None  # (ssid, password) netplan rendered / the supplicant runs
        # Forced states of the next status polls: full, assoc (no address, no route), down (not associated),
        # empty and bad-json (ip output), no-route, no-address, handshake (not COMPLETED, address and route).
        self.polls = []
        self.state = 'down'
        self.now = 0.0
        self.calls, self.during_apply = [], []

    def netplan(self, timeout=10):
        return NetplanWifi(self.file, self.state_dir, 'wlan0', timeout, runner=self.run,
                           sleep=self.sleep, clock=lambda: self.now, units_dir=self.units_dir)

    def sleep(self, seconds):
        self.now += seconds

    def linked(self):
        return self.associated is not None and self.networks.get(self.associated[0]) == self.associated[1]

    def run(self, args, timeout):
        self.calls.append(args)
        if args == APPLY:
            self.during_apply.append({p.name: stat.S_IMODE(p.stat().st_mode) for p in self.state_dir.iterdir()})
            code = self.apply_codes.pop(0) if self.apply_codes else 0
            if code is None:
                self.now += timeout
                return None, ''
            if self.file.exists():
                ssid, password = parse_netplan(self.file.read_text())
                self.configured = (ssid, password or '')
            else:
                self.configured = None
            return code, ''
        if args in (RESTART, STOP):
            assert timeout == netplan_wifi.SUPPLICANT_TIMEOUT
            code = self.restart_codes.pop(0) if self.restart_codes else 0
            if code == 0:
                self.associated = self.configured if args == RESTART else None
            return code, ''
        if args == STATUS:
            self.state = self.polls.pop(0) if self.polls else ('full' if self.linked() else 'down')
            if self.state == 'down':
                return 0, 'wpa_state=SCANNING\n'
            ssid = (self.associated or ('Elsewhere',))[0]
            wpa_state = '4WAY_HANDSHAKE' if self.state == 'handshake' else 'COMPLETED'
            return 0, f'bssid=02:00:00:00:00:01\nssid={printf_encode(ssid.encode())}\nwpa_state={wpa_state}\n'
        if args == ADDRESSES:
            if self.state == 'bad-json':
                return 0, ''  # ip exited 0 and printed nothing
            if self.state == 'empty':
                return 0, '[{}]'
            info = ([{'family': 'inet', 'local': '192.0.2.10', 'prefixlen': 24}]
                    if self.state in ('full', 'no-route', 'handshake') else [])
            return 0, json.dumps([{'ifname': 'wlan0', 'addr_info': info}])
        if args == ROUTES:
            if self.state == 'no-route':
                return 0, '[{}]'
            return 0, json.dumps([{'dst': 'default', 'gateway': '192.0.2.1', 'dev': 'wlan0'}]
                                 if self.state in ('full', 'no-address', 'handshake') else [])
        raise AssertionError(args)  # any other command line, one with a password included

    def count(self, command):
        return self.calls.count(command)

    def polls_made(self):
        return self.count(STATUS)


@pytest.fixture
def system(tmp_path):
    fake = FakeSystem(tmp_path)
    fake.file.write_bytes(OLD_FILE)
    fake.file.chmod(0o600)
    fake.configured = fake.associated = (OLD_SSID, OLD_PASSWORD)
    return fake


def test_new_network_connects(system):
    system.networks[NEW_SSID] = NEW_PASSWORD
    # Wider modes left from before: the change makes them 0700 and 0600 (mkdir and open honour the umask only).
    system.state_dir.mkdir(parents=True)
    system.state_dir.chmod(0o755)
    leftover = system.file.with_name('.' + NETPLAN_NAME + '.tmp')
    leftover.write_bytes(b'leftover')
    leftover.chmod(0o644)
    outcome = system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert outcome == Outcome() and outcome.result == 'connected'
    assert parse_netplan(system.file.read_text()) == (NEW_SSID, NEW_PASSWORD)
    assert stat.S_IMODE(system.file.stat().st_mode) == 0o600
    assert stat.S_IMODE(system.state_dir.stat().st_mode) == 0o700
    # The marker and the 0600 copy existed while netplan changed, and are gone after.
    assert system.during_apply == [{'pending.json': 0o600, 'previous.yaml': 0o600}]
    assert list(system.state_dir.iterdir()) == []
    assert system.calls[:2] == [APPLY, RESTART] and system.polls_made() == 2  # two good polls in a row


def test_timeout_restores_the_previous_file_byte_for_byte(system):
    system.networks[NEW_SSID] = 'another-password'
    outcome = system.netplan(timeout=30).apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:connect_timeout; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and stat.S_IMODE(system.file.stat().st_mode) == 0o600
    assert system.associated == (OLD_SSID, OLD_PASSWORD) and system.now >= 30
    assert system.count(APPLY) == 2 and system.count(RESTART) == 2  # the rollback restarts it too
    assert list(system.state_dir.iterdir()) == []


def test_previous_network_that_does_not_come_back_is_reported(system):
    system.networks.clear()  # neither the new network nor the old one is in reach
    outcome = system.netplan(timeout=10).apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:connect_timeout; restored, previous network connected=no'
    assert outcome.message(NEW_SSID).endswith('previous configuration restored, previous network connected: no')
    assert system.file.read_bytes() == OLD_FILE and system.now >= 20  # the rollback waited its own timeout too


def test_same_ssid_with_a_new_password_needs_the_supplicant_restart(system):
    # The old supplicant would stay COMPLETED on this SSID with the old password: a false success.
    outcome = system.netplan().apply(OLD_SSID, 'changed-example-pw')
    assert outcome.reason == 'connect_timeout' and system.file.read_bytes() == OLD_FILE
    assert system.calls[:2] == [APPLY, RESTART]


def test_old_association_that_survives_the_restart_is_not_success(system):
    # netplan's --no-block stop: the old supplicant can stay COMPLETED on the old SSID.
    system.networks[NEW_SSID] = NEW_PASSWORD
    netplan = system.netplan()
    skipped = []

    def runner(args, timeout):
        if args == RESTART and not skipped:  # the change's restart leaves the old association in place
            skipped.append(args)
            return 0, ''
        return system.run(args, timeout)
    netplan.runner = runner
    outcome = netplan.apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:connect_timeout; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE


def test_supplicant_restart_failure_rolls_back(system):
    system.networks[NEW_SSID] = NEW_PASSWORD
    system.restart_codes = [1]
    outcome = system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:wpa_restart_failed; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and system.count(RESTART) == 2


def test_without_a_previous_file_rollback_removes_it(tmp_path):
    system = FakeSystem(tmp_path, networks={})
    outcome = system.netplan().apply(NEW_SSID, '')
    assert outcome.result == 'failed:connect_timeout; restored, no previous Wi-Fi configuration'
    assert 'no previous Wi-Fi configuration' in outcome.message(NEW_SSID)
    assert not system.file.exists() and list(system.state_dir.iterdir()) == []
    assert system.during_apply[0] == {'pending.json': 0o600}  # nothing to copy
    assert system.calls[-1] == STOP and system.associated is None


def test_start_up_rollback_without_a_previous_file_removes_it(tmp_path):
    system = FakeSystem(tmp_path, networks={})
    interrupted_change(system)
    assert system.netplan().recover().recovery_message() == (
        'Wi-Fi: interrupted change rolled back, no previous Wi-Fi configuration')
    assert not system.file.exists() and list(system.state_dir.iterdir()) == []
    assert system.calls[-1] == STOP


def test_without_a_previous_file_another_netplan_file_keeps_the_supplicant(tmp_path):
    # The unit still exists after netplan apply: some other file defines wlan0.
    system = FakeSystem(tmp_path, networks={})
    (system.units_dir / SUPPLICANT_UNIT).write_text('[Unit]\n')
    outcome = system.netplan().apply(NEW_SSID, '')
    assert outcome.result == 'failed:connect_timeout; restored, no previous Wi-Fi configuration'
    assert system.count(RESTART) == 2 and system.count(STOP) == 0


@pytest.mark.parametrize('code,reason', [(1, 'netplan_apply_failed'), (None, 'netplan_apply_timeout')])
def test_netplan_apply_error_rolls_back(system, code, reason):
    system.networks[NEW_SSID] = NEW_PASSWORD
    system.apply_codes = [code]
    outcome = system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == f'failed:{reason}; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and system.count(APPLY) == 2


def test_gradual_connection_counts_once_complete(system):
    # Associated first, the DHCP lease later: COMPLETED alone is not connected.
    system.networks[NEW_SSID] = NEW_PASSWORD
    system.polls = ['down', 'assoc', 'assoc', 'full']
    assert system.netplan().apply(NEW_SSID, NEW_PASSWORD).result == 'connected'
    assert system.polls_made() == 5


@pytest.mark.parametrize('polls', [['empty'] * 6, ['no-route'] * 6, ['no-address'] * 6, ['bad-json'] * 6,
                                   ['handshake'] * 6, ['full', 'down'] * 3, ['assoc', 'full'] * 3],
                         ids=['ip-empty-objects', 'no-default-route', 'no-address', 'ip-output-not-json',
                              'handshake-not-completed', 'blips', 'alternating'])
def test_no_two_good_polls_in_a_row_is_a_timeout(system, polls):
    system.networks[NEW_SSID] = NEW_PASSWORD
    system.polls = list(polls)  # the six polls of a 10 s wait; the rollback's polls are real
    outcome = system.netplan(timeout=10).apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:connect_timeout; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE


def test_state_directory_not_writable_changes_nothing(system):
    system.state_dir.parent.mkdir()
    system.state_dir.write_text('not a directory')
    outcome = system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:state_write_failed; netplan unchanged'
    assert outcome.message(NEW_SSID).endswith('not applied (state_write_failed); netplan unchanged')
    assert system.file.read_bytes() == OLD_FILE and system.calls == []


def raising_runner(system, matches, failing=lambda count: True, error=RuntimeError):
    """system.run, except that the n-th call for which matches(args) holds raises `error` while failing(n)."""
    count = 0

    def runner(args, timeout):
        nonlocal count
        if matches(args):
            count += 1
            if failing(count):
                raise error('unexpected runner failure')
        return system.run(args, timeout)
    return runner


def test_unexpected_error_during_the_change_rolls_back(system):
    netplan = system.netplan()
    netplan.runner = raising_runner(system, lambda args: args == STATUS, lambda count: count == 1)
    outcome = netplan.apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:error; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []


def test_run_command_never_raises(caplog):
    # Harmless local processes only: the interpreter itself.
    code, out = run_command([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"ssid=\\xff\\xfe ok")'], 30)
    assert code == 0 and out == 'ssid=\ufffd\ufffd ok'
    caplog.set_level(logging.WARNING, logger='phonelet.netplan_wifi')
    assert run_command(['/nonexistent/phonelet-test-command'], 5) == (None, '')
    assert run_command([sys.executable, '-c', 'import time; time.sleep(30)'], 0.2) == (None, '')
    # Why it did not run, without the exception text.
    assert [r.getMessage() for r in caplog.records] == [
        '/nonexistent/phonelet-test-command did not finish: FileNotFoundError',
        f'{sys.executable} did not finish: TimeoutExpired']


def test_error_inside_the_rollback_keeps_the_marker(system):
    system.networks[NEW_SSID] = NEW_PASSWORD
    system.apply_codes = [1]  # the change fails; then every rollback netplan apply raises
    netplan = system.netplan()
    netplan.runner = raising_runner(system, lambda args: args == APPLY, lambda count: count > 1)
    outcome = netplan.apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:error; rollback failed'
    assert netplan.marker.exists() and netplan.backup.read_bytes() == OLD_FILE
    # The marker check and recovery at the top of apply() do not raise either.
    assert netplan.apply(NEW_SSID, NEW_PASSWORD).result == 'failed:error; rollback failed'
    assert netplan.backup.read_bytes() == OLD_FILE
    netplan.runner = system.run  # the runner works again: the state it left still rolls back
    assert netplan.recover().result == 'failed:interrupted; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []


@pytest.mark.parametrize('apply_codes,restart_codes,result,journal', [
    ([0, None], [], 'failed:connect_timeout; restored, previous network connected=no',
     'Wi-Fi rollback: netplan apply timed out'),
    ([], [0, 1], 'failed:connect_timeout; restored, previous network connected=no',
     'Wi-Fi rollback: supplicant exited 1'),
    ([1, 1], [1], 'failed:netplan_apply_failed; restored, previous network connected=yes',
     'Wi-Fi rollback: netplan apply exited 1'),
], ids=['netplan-apply-times-out', 'supplicant-restart-fails', 'both-exit-1'])
def test_rollback_step_that_fails_still_restores_the_file_and_blocks_nothing(system, caplog, apply_codes,
                                                                             restart_codes, result, journal):
    # Not retried (docs/sms-commands.md#wi-fi-command): the file is back, the journal says what failed.
    system.apply_codes, system.restart_codes = apply_codes, restart_codes
    assert system.netplan().apply(NEW_SSID, NEW_PASSWORD).result == result
    assert journal in caplog.text
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []
    # No marker: the owner's next command is not answered with rollback_pending.
    system.networks[NEW_SSID] = NEW_PASSWORD
    assert system.netplan().apply(NEW_SSID, NEW_PASSWORD).result == 'connected'


def test_marker_that_cannot_be_removed_rolls_the_change_back_at_the_next_look(system, monkeypatch):
    system.networks[NEW_SSID] = NEW_PASSWORD
    real = netplan_wifi._remove

    def remove(path):
        if path.name == 'pending.json':
            raise OSError(5, 'Input/output error')
        real(path)
    netplan = system.netplan()
    with monkeypatch.context() as patched:
        patched.setattr(netplan_wifi, '_remove', remove)
        assert netplan.apply(NEW_SSID, NEW_PASSWORD).result == 'connected'
    assert netplan.backup.read_bytes() == OLD_FILE  # the copy stays with its marker
    assert netplan.recover().result == 'failed:interrupted; restored, previous network connected=yes'


@pytest.mark.parametrize('marker', [b'', b'not json', b'{}', b'[]', b'null', b'{"existed": true}'],
                         ids=['empty', 'not-json', 'no-field', 'list', 'null', 'copy-missing'])
def test_unreadable_marker_or_missing_copy_touches_nothing(system, marker):
    system.state_dir.mkdir(parents=True)
    (system.state_dir / 'pending.json').write_bytes(marker)
    netplan = system.netplan()
    assert netplan.recover().result == 'failed:interrupted; rollback failed'
    assert netplan.apply(NEW_SSID, NEW_PASSWORD).result == 'failed:rollback_pending; netplan unchanged'
    assert system.file.read_bytes() == OLD_FILE and system.calls == [] and netplan.marker.exists()


class PowerCut(BaseException):
    """Not an Exception: nothing in NetplanWifi catches it, as nothing runs after a kill."""


@pytest.mark.parametrize('step,nth', [(APPLY, 1), (RESTART, 1), (STATUS, 1), (APPLY, 2), (RESTART, 2), (STATUS, 7)],
                         ids=['change-apply', 'change-restart', 'change-wait',
                              'rollback-apply', 'rollback-restart', 'rollback-wait'])
def test_power_cut_at_any_step_rolls_back_at_the_next_start(system, step, nth):
    netplan = system.netplan()  # the new network is out of reach: the change times out and rolls back
    netplan.runner = raising_runner(system, lambda args: args == step, lambda count: count == nth, PowerCut)
    with pytest.raises(PowerCut):
        netplan.apply(NEW_SSID, NEW_PASSWORD)
    assert system.netplan().recover().result == 'failed:interrupted; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []


@pytest.mark.parametrize('name,unfinished', [('previous.yaml', False), ('pending.json', False), (NETPLAN_NAME, True)])
def test_power_cut_while_a_file_is_written(system, monkeypatch, name, unfinished):
    real = os.rename

    def rename(source, target):
        if Path(target).name == name:
            raise PowerCut
        real(source, target)
    with monkeypatch.context() as patched:
        patched.setattr(netplan_wifi.os, 'rename', rename)
        with pytest.raises(PowerCut):
            system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert system.file.read_bytes() == OLD_FILE  # never half-written
    directory = system.file.parent if name == NETPLAN_NAME else system.state_dir
    assert (directory / f'.{name}.tmp').exists()  # a kill removes nothing; the next write reuses it
    assert system.netplan().has_unfinished_change() is unfinished  # a marker always has its copy
    system.networks[NEW_SSID] = NEW_PASSWORD
    assert system.netplan().apply(NEW_SSID, NEW_PASSWORD).result == 'connected'  # leftovers do not block


@pytest.mark.parametrize('new_network_in_reach', [True, False], ids=['after-success', 'after-rollback'])
def test_power_cut_between_the_two_removals_leaves_no_unfinished_change(system, monkeypatch, new_network_in_reach):
    if new_network_in_reach:
        system.networks[NEW_SSID] = NEW_PASSWORD
    real, removed = netplan_wifi._remove, []

    def remove(path):
        removed.append(path.name)
        if len(removed) == 2:
            raise PowerCut
        real(path)
    with monkeypatch.context() as patched:
        patched.setattr(netplan_wifi, '_remove', remove)
        with pytest.raises(PowerCut):
            system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert removed == ['pending.json', 'previous.yaml']
    assert system.netplan().recover() is None
    if new_network_in_reach:
        assert parse_netplan(system.file.read_text()) == (NEW_SSID, NEW_PASSWORD)
    else:
        assert system.file.read_bytes() == OLD_FILE


def interrupted_change(system):
    """The state a crash during the change leaves: marker, copy and the new file."""
    system.netplan()._begin_change()
    system.file.write_text(render_netplan('wlan0', NEW_SSID, NEW_PASSWORD))


def test_marker_at_start_rolls_back(system):
    interrupted_change(system)
    outcome = system.netplan().recover()
    assert outcome.result == 'failed:interrupted; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []
    assert system.netplan().recover() is None and system.count(APPLY) == 1


def test_start_up_rollback_whose_network_does_not_come_back_says_so(system):
    interrupted_change(system)
    system.networks.clear()
    assert system.netplan().recover().recovery_message() == (
        'Wi-Fi: interrupted change rolled back, previous network connected: no')


class FailingNetplanWrites:
    """_write_atomic raises ENOSPC, quoting the data, for writes of the file `name` for which when(data) holds.

    Only while `on`, and for the first `times` of them (None: every one); `refused` counts them.
    """
    def __init__(self, monkeypatch, when=lambda data: True, times=None, name=NETPLAN_NAME):
        self.on, self.refused = True, 0
        real = netplan_wifi._write_atomic

        def write(path, data):
            if self.on and path.name == name and when(data) and (times is None or self.refused < times):
                self.refused += 1
                raise OSError(28, 'No space left on device: ' + data.decode())
            return real(path, data)
        monkeypatch.setattr(netplan_wifi, '_write_atomic', write)


def test_new_file_that_cannot_be_written_rolls_back(system, monkeypatch):
    FailingNetplanWrites(monkeypatch, times=1)
    outcome = system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:write_failed; restored, previous network connected=yes'
    assert system.file.read_bytes() == OLD_FILE and list(system.state_dir.iterdir()) == []


@pytest.mark.parametrize('name', ['previous.yaml', 'pending.json'])
def test_copy_or_marker_that_cannot_be_written_changes_nothing(system, monkeypatch, name):
    FailingNetplanWrites(monkeypatch, name=name)
    netplan = system.netplan()
    assert netplan.apply(NEW_SSID, NEW_PASSWORD).result == 'failed:state_write_failed; netplan unchanged'
    assert system.file.read_bytes() == OLD_FILE and system.calls == [] and not netplan.has_unfinished_change()


def test_failed_rollback_blocks_new_changes_until_it_succeeds(system, monkeypatch):
    interrupted_change(system)
    interrupted = system.file.read_bytes()
    failing = FailingNetplanWrites(monkeypatch)
    netplan = system.netplan()
    assert netplan.recover().result == 'failed:interrupted; rollback failed'
    # A command meanwhile touches nothing: the copy is the only good configuration.
    outcome = netplan.apply(OLD_SSID, 'some-other-pass')
    assert outcome.result == 'failed:rollback_pending; netplan unchanged'
    assert netplan.backup.read_bytes() == OLD_FILE
    assert system.file.read_bytes() == interrupted and system.count(APPLY) == 0
    failing.on = False
    system.networks[NEW_SSID] = NEW_PASSWORD
    # The next change finishes the rollback first, then applies.
    assert netplan.apply(NEW_SSID, NEW_PASSWORD).result == 'connected'
    assert system.count(APPLY) == 2 and list(system.state_dir.iterdir()) == []


def test_rendered_netplan_is_this_text():
    head = ('network:\n  version: 2\n  wifis:\n    "wlan0":\n      dhcp4: true\n      dhcp6: true\n'
            '      access-points:\n')
    assert render_netplan('wlan0', OLD_SSID, OLD_PASSWORD) == (
        head + '        "Old-Example":\n          password: "old-example-pass"\n')
    assert render_netplan('wlan0', OLD_SSID, '') == head + '        "Old-Example": {}\n'
    # Raw, not \ud83d\ude00: libyaml rejects surrogate-pair escapes.
    assert render_netplan('wlan0', 'Кафе 😀 ü', '') == head + '        "Кафе 😀 ü": {}\n'


@pytest.mark.parametrize('ssid,password', [
    pytest.param('a"b\\c: #d', 'p"a\\s:s #w {x}', id='yaml-and-json-specials'),
    pytest.param('Кафе 😀 ü', '', id='non-ascii-open-network'),
    pytest.param('ctl\x00\x01\x1b\t\n\x7f\x85', '- [x] *&!|>%@`\'', id='control-characters'),
    pytest.param('\u2028\u2029\ufeff\ufffe\uffff yes', 'no ~ null', id='yaml-line-breaks-bom-and-noncharacters'),
])
def test_yaml_values_read_back_as_json(ssid, password):
    text = render_netplan('wlan0', ssid, password)
    assert parse_netplan(text) == (ssid, password or None)  # None: the open-network form
    assert all(c == '\n' or c.isprintable() for c in text)  # no raw control, line-break or BOM character


def test_atomic_write_writes_everything_when_the_system_writes_less(tmp_path, monkeypatch):
    real_fdopen, real_write = os.fdopen, os.write

    def short_fdopen(fd, *args, **kwargs):  # under whatever stream the code opens
        stream = real_fdopen(fd, *args, **kwargs)
        raw = getattr(stream, 'raw', stream)
        raw_write = raw.write
        raw.write = lambda data: raw_write(data[:2])  # write(2) may take fewer bytes than asked
        return stream
    data = OLD_FILE * 1000  # larger than the stream's buffer
    path = tmp_path / 'previous.yaml'
    with monkeypatch.context() as patched:
        patched.setattr(netplan_wifi.os, 'fdopen', short_fdopen)
        patched.setattr(netplan_wifi.os, 'write', lambda fd, data: real_write(fd, data[:2]))
        netplan_wifi._write_atomic(path, data)
    assert path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ['previous.yaml']


def test_atomic_write_that_fails_leaves_no_temp_file(tmp_path, monkeypatch):
    def failing(fd):
        raise OSError(28, 'No space left on device')
    with monkeypatch.context() as patched:
        patched.setattr(netplan_wifi.os, 'fsync', failing)  # the temp file's fsync comes first
        with pytest.raises(OSError):
            netplan_wifi._write_atomic(tmp_path / 'x.yaml', b'password: "secret"')
    assert list(tmp_path.iterdir()) == []


def test_new_file_not_renamed_without_a_previous_file_leaves_no_password(tmp_path, monkeypatch):
    system = FakeSystem(tmp_path, networks={})
    real = os.rename

    def rename(source, target):
        if Path(target).name == NETPLAN_NAME:
            raise OSError(5, 'Input/output error')
        real(source, target)
    with monkeypatch.context() as patched:
        patched.setattr(netplan_wifi.os, 'rename', rename)
        outcome = system.netplan().apply(NEW_SSID, NEW_PASSWORD)
    assert outcome.result == 'failed:write_failed; restored, no previous Wi-Fi configuration'
    assert list(system.file.parent.iterdir()) == [] and list(system.state_dir.iterdir()) == []


def test_wpa_cli_escapes_decode():
    for raw in ('Ж-net'.encode(), b'a"b\\c\x1b\n\r\t\x00\xff', b'plain'):
        assert unescape_wpa(printf_encode(raw)) == raw
    assert unescape_wpa('trailing\\') == b'trailing\\' and unescape_wpa('\\xZZ') == b'\\xZZ'
