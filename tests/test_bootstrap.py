"""Host bootstrap, Asterisk archives and builds, target profiles and units."""
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from tests.helpers import ROOT, executable, script


@pytest.fixture
def asterisk_fetch(tmp_path):
    """A checkout copy with fetch-asterisk.sh, a synthetic lock and curl that only copies a local fixture."""
    archive = tmp_path / 'build/asterisk-20.22.0-arm64.tar.gz'
    archive.parent.mkdir()
    archive.write_bytes(b'fake asterisk build')
    checkout = tmp_path / 'checkout'
    (checkout / 'scripts').mkdir(parents=True)
    shutil.copytree(ROOT / 'asterisk', checkout / 'asterisk')
    (checkout / 'scripts/fetch-asterisk.sh').symlink_to(ROOT / 'scripts/fetch-asterisk.sh')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    lock = checkout / 'asterisk/artifacts.lock'
    lock.write_text('# Synthetic archive checksums\n' + ''.join(
        f'{arch} {digest} {"0" * 64} {"1" * 64} '
        f'https://example.invalid/asterisk-20.22.0-{arch}.tar.gz\n' for arch in ('arm64', 'amd64')))
    curl_log, curl = tmp_path / 'curl.log', tmp_path / 'bin/curl'
    executable(curl, '''printf '%s\\n' "$@" > "$CURL_LOG"
while (( $# )); do
  if [[ "$1" == --output ]]; then cp -- "$CURL_SOURCE" "$2"; shift; fi
  shift
done
exit "$CURL_STATUS"
''')
    env = {'PATH': str(curl.parent) + ':' + os.environ['PATH'], 'CURL_LOG': str(curl_log),
           'CURL_SOURCE': str(archive), 'CURL_STATUS': '0'}
    destination = tmp_path / 'fetched build'

    def run(arguments=None, extra_env=None):
        return subprocess.run(['bash', str(checkout / 'scripts/fetch-asterisk.sh')] + (
            ['arm64', str(destination)] if arguments is None else arguments),
            cwd=tmp_path, capture_output=True, text=True, env=os.environ | env | (extra_env or {}))

    return SimpleNamespace(run=run, checkout=checkout, lock=lock, destination=destination, archive=archive,
                           curl=curl, curl_log=curl_log, digest=digest, root=tmp_path)


def test_fetch_refuses_the_placeholder_sums_of_an_unpublished_build(asterisk_fetch):
    f = asterisk_fetch
    f.lock.write_text(f.lock.read_text().replace(f.digest, '0' * 64))
    result = f.run()
    assert result.returncode == 1 and 'not published yet' in result.stderr
    assert not f.curl_log.exists() and not f.destination.exists()
    # The shipped lock may hold such placeholders: then every fetch stops there, before curl.
    shipped = [line.split()[1] for line in (ROOT / 'asterisk/artifacts.lock').read_text().splitlines()
               if line and not line.startswith('#')]
    assert all(re.fullmatch('[0-9a-f]{64}', value) for value in shipped)


@pytest.mark.parametrize('arch', ['arm64', 'amd64'])
def test_fetch_produces_a_build_directory(asterisk_fetch, arch):
    f = asterisk_fetch
    result = f.run([arch, str(f.destination)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(f.destination)
    archive_name = f'asterisk-20.22.0-{arch}.tar.gz'
    assert sorted(path.name for path in f.destination.iterdir()) == ['artifacts.sha256', archive_name]
    assert (f.destination / archive_name).read_bytes() == f.archive.read_bytes()
    assert (f.destination / 'artifacts.sha256').read_text() == (
        f'{f.digest}  {archive_name}\n' + '0' * 64 + '  stage/usr/sbin/asterisk\n' +
        '1' * 64 + '  stage/usr/lib/asterisk/modules/chan_dongle.so\n')
    args = f.curl_log.read_text().splitlines()
    assert args[:9] == ['--fail', '--location', '--silent', '--show-error', '--proto', '=https',
                        '--proto-redir', '=https', '--output']
    assert Path(args[9]).parent.parent == f.destination.parent
    assert args[10:] == [f'https://example.invalid/{archive_name}']
    assert not list(f.destination.parent.glob(f.destination.name + '.tmp.*'))


def test_fetch_checksum_mismatch_removes_partial_download_and_names_both_sums(asterisk_fetch):
    f = asterisk_fetch
    damaged = f.root / 'damaged.tar.gz'
    damaged.write_bytes(b'damaged download')
    result = f.run(extra_env={'CURL_SOURCE': str(damaged)})
    assert result.returncode == 1
    actual = hashlib.sha256(damaged.read_bytes()).hexdigest()
    assert f'Checksum mismatch for {f.archive.name}: expected {f.digest}, got {actual}' in result.stderr
    assert not f.destination.exists()
    assert not list(f.destination.parent.glob(f.destination.name + '.tmp.*'))


@pytest.mark.parametrize('arguments', [[], ['arm64'], ['armhf', 'DEST'], ['arm64', ''],
                                      ['arm64', 'DEST', 'archive', 'extra']])
def test_fetch_usage_errors_never_call_curl(asterisk_fetch, arguments):
    f = asterisk_fetch
    result = f.run([str(f.destination) if arg == 'DEST' else arg for arg in arguments])
    assert result.returncode == 2
    assert not f.curl_log.exists() and not f.destination.exists()


@pytest.mark.parametrize('kind', ['directory', 'file', 'dangling-symlink'])
def test_fetch_existing_destination_is_never_changed(asterisk_fetch, kind):
    f = asterisk_fetch
    if kind == 'directory':
        f.destination.mkdir()
    elif kind == 'file':
        f.destination.write_bytes(b'keep me')
    else:
        f.destination.symlink_to(f.destination.parent / 'absent')
    def identity(stat):  # not st_atime: reading a symlink may update it
        return stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns

    before = identity(f.destination.lstat())
    result = f.run()
    assert result.returncode == 2 and not f.curl_log.exists()
    assert identity(f.destination.lstat()) == before


def test_fetch_curl_failure_removes_partial_download(asterisk_fetch):
    f = asterisk_fetch
    result = f.run(extra_env={'CURL_STATUS': '22'})
    assert result.returncode == 22 and f.curl_log.exists()
    assert not f.destination.exists()
    assert not list(f.destination.parent.glob(f.destination.name + '.tmp.*'))


def test_fetch_refuses_destination_created_during_download(asterisk_fetch):
    f = asterisk_fetch
    f.curl.write_text(f.curl.read_text().replace('exit "$CURL_STATUS"',
                                           'mkdir -- "$FETCH_DESTINATION"\nexit "$CURL_STATUS"'))
    result = f.run(extra_env={'FETCH_DESTINATION': str(f.destination)})
    assert result.returncode == 1 and f'Destination already exists: {f.destination}' in result.stderr
    assert f.destination.is_dir() and list(f.destination.iterdir()) == []
    assert not list(f.destination.parent.glob(f.destination.name + '.tmp.*'))


@pytest.mark.parametrize('damaged', [False, True])
def test_fetch_local_archive_is_checked_without_curl(asterisk_fetch, damaged):
    f = asterisk_fetch
    if damaged:
        f.archive.write_bytes(b'damaged local archive')
    result = f.run(['arm64', str(f.destination), str(f.archive)])
    assert result.returncode == (1 if damaged else 0), result.stderr
    assert not f.curl_log.exists()
    assert f.destination.exists() == (not damaged)
    if damaged:
        actual = hashlib.sha256(f.archive.read_bytes()).hexdigest()
        assert f'expected {f.digest}, got {actual}' in result.stderr
    else:
        assert (f.destination / f.archive.name).read_bytes() == f.archive.read_bytes()
        assert len((f.destination / 'artifacts.sha256').read_text().splitlines()) == 3
    assert not list(f.destination.parent.glob(f.destination.name + '.tmp.*'))


@pytest.mark.parametrize('damage', ['archive-sum', 'asterisk-sum', 'dongle-sum', 'uppercase-sum', 'http',
                                   'other-arch', 'version', 'duplicate', 'missing', 'extra-field', 'missing-field'])
def test_fetch_refuses_malformed_lock_before_curl(asterisk_fetch, damage):
    f = asterisk_fetch
    fields = f.lock.read_text().splitlines()[1].split()
    if damage in ('archive-sum', 'asterisk-sum', 'dongle-sum'):
        fields[{'archive-sum': 1, 'asterisk-sum': 2, 'dongle-sum': 3}[damage]] = 'a' * 63
    elif damage == 'uppercase-sum':
        fields[1] = 'A' * 64
    elif damage == 'http':
        fields[4] = fields[4].replace('https://', 'http://')
    elif damage == 'other-arch':
        fields[4] = fields[4].replace('-arm64.tar.gz', '-amd64.tar.gz')
    elif damage == 'version':
        fields[4] = fields[4].replace('20.22.0', '20.22.1')
    elif damage == 'extra-field':
        fields.append('extra')
    elif damage == 'missing-field':
        fields.pop()
    line = ' '.join(fields) + '\n'
    f.lock.write_text('' if damage == 'missing' else line * (2 if damage == 'duplicate' else 1))
    result = f.run()
    assert result.returncode == 1 and not f.curl_log.exists()
    assert not f.destination.exists()
    assert not list(f.destination.parent.glob(f.destination.name + '.tmp.*'))


def test_artifacts_lock_arm64_is_well_formed_and_matches_sources_lock():
    rows = [line.split() for line in (ROOT / 'asterisk/artifacts.lock').read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    arm64 = [row for row in rows if row[0] == 'arm64']
    assert len(arm64) == 1 and len(arm64[0]) == 5
    assert all(re.fullmatch('[0-9a-f]{64}', value) for value in arm64[0][1:4])
    version = re.search(r'^ASTERISK_VERSION=(.+)$', (ROOT / 'asterisk/sources.lock').read_text(), re.M)[1]
    # The release tag is asterisk-VERSION-N, N counting builds of that version.
    assert re.fullmatch(r'https://github\.com/stlk0/phonelet/releases/download/asterisk-' + re.escape(version)
                        + r'-[1-9][0-9]*/asterisk-' + re.escape(version) + r'-arm64\.tar\.gz', arm64[0][4])


def test_artifacts_lock_every_architecture_is_well_formed():
    text = (ROOT / 'asterisk/artifacts.lock').read_text()
    rows = [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert sorted(row[0] for row in rows) == ['amd64', 'arm64']
    version = re.search(r'^ASTERISK_VERSION=(.+)$', (ROOT / 'asterisk/sources.lock').read_text(), re.M)[1]
    for row in rows:
        assert len(row) == 5
        assert all(re.fullmatch('[0-9a-f]{64}', value) for value in row[1:4])
        assert re.fullmatch(r'https://.*/asterisk-' + re.escape(version) + '-' + row[0] + r'\.tar\.gz', row[4])


def test_bootstrap_installs_boot_only_serial_lock_cleanup_idempotently(tmp_path):
    assert (ROOT / 'asterisk/systemd/phonelet-serial-locks.conf').read_bytes() == b'r! /var/lock/LCK..ttyUSB*\n'
    remote = (ROOT / 'scripts/bootstrap.sh').read_text()
    step = remote[remote.index('# Remove serial locks'):remote.index('bash "$CHECKOUT/scripts/provision-storage.sh"')]
    destination = tmp_path / 'etc/tmpfiles.d/phonelet-serial-locks.conf'
    step = step.replace('/etc/tmpfiles.d/phonelet-serial-locks.conf', str(destination))
    commands, log = tmp_path / 'install-bin', tmp_path / 'install.log'
    executable(commands / 'install', '''echo install >> "$INSTALL_LOG"
exec /usr/bin/install "$@"
''')
    env = os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'],
                        'CHECKOUT': str(ROOT), 'INSTALL_LOG': str(log)}
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + step],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == b'r! /var/lock/LCK..ttyUSB*\n'
    assert destination.stat().st_mode & 0o777 == 0o644
    before = destination.stat().st_mtime_ns
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + step],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ['install']
    assert destination.stat().st_mtime_ns == before
    assert destination.read_bytes() == b'r! /var/lock/LCK..ttyUSB*\n'


def test_shell_scripts_contain_no_target_names_or_board_details():
    names = [path.name for path in (ROOT / 'targets').iterdir() if path.is_dir()]
    for path in (ROOT / 'scripts').glob('*.sh'):
        text = path.read_text()
        assert not any(name in text for name in names), path
        assert not re.search(r'orangepi|Orange Pi|PH4|Armbian|sunxi|USB-C', text), path


@pytest.fixture
def bootstrap_host_steps(tmp_path):
    """Execute host checks and package/profile/udev steps with temporary paths and fake commands."""
    commands, stage = tmp_path / 'bin', tmp_path / 'stage'
    stage.mkdir()
    log = tmp_path / 'commands.log'
    watchdog_state = tmp_path / 'watchdog-state'
    for directory in ('etc/default', 'etc/udev/rules.d', 'stage/target', 'stage/etc-asterisk'):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / 'etc/os-release').write_text('ID=debian\nVERSION_CODENAME=trixie\n')
    ramlog = tmp_path / 'etc/default/armbian-ramlog'
    ramlog.write_text('ENABLED=true\n')
    shutil.copyfile(ROOT / 'targets/orangepi-zero2w/phonelet-modem-power.service', stage / 'target/phonelet-modem-power.service')
    (stage / 'etc-asterisk/dongle.conf').write_text(f'data={tmp_path}/dev/dongle0-at\naudio={tmp_path}/dev/dongle0-audio\n')
    (tmp_path / 'dev').mkdir()
    for name in ('dongle0-at', 'dongle0-audio'):
        (tmp_path / 'dev' / name).symlink_to('/dev/null')
    executable(commands / 'uname', 'echo "${FAKE_ARCH:-aarch64}"\n')
    # The test may itself run in a container: only FAKE_CONTAINER=1 is one.
    executable(commands / 'systemd-detect-virt',
               '[[ "$*" == "--container --quiet" ]] || exit 2; [[ "${FAKE_CONTAINER:-}" == 1 ]]\n')
    executable(commands / 'dpkg', '[[ "$*" == --print-architecture ]] || exit 1; echo "${FAKE_DPKG_ARCH:-arm64}"\n')
    executable(commands / 'findmnt', '''
if [[ "$*" == '-n /var/log' ]]; then
  [[ "$RAMLOG_MOUNT" == 1 ]] && echo ramlog
elif [[ "$*" == '-n -o FSTYPE,SOURCE --target /var/log' ]]; then
  printf '%s\\n' "$LOG_STORAGE"
else
  [[ "$1 $2 $3 $4" == '-n -o FSTYPE,SOURCE --target' && $# == 5 ]] || exit 1
  echo "$5" >> "$FINDMNT_LOG"
  printf '%s\\n' "$SMS_STORAGE"
fi
exit 0
''')
    executable(commands / 'apt-get', 'echo "apt-get $*" >> "$COMMAND_LOG"\n')
    executable(commands / 'curl', 'echo "unexpected curl" >&2; exit 1\n')
    executable(commands / 'udevadm', 'echo "udevadm $*" >> "$COMMAND_LOG"\n')
    executable(commands / 'sleep', 'echo "sleep $*" >> "$COMMAND_LOG"\n')
    executable(commands / 'tar', 'echo "tar $*" >> "$COMMAND_LOG"\n')
    executable(commands / 'ldconfig', 'echo ldconfig >> "$COMMAND_LOG"\n')
    executable(commands / 'pidof', 'echo "pidof $*" >> "$COMMAND_LOG"; exit 1\n')
    executable(commands / 'systemctl', '''echo "systemctl $*" >> "$COMMAND_LOG"
case "$*" in
 'show -p RuntimeWatchdogUSec --value') cat "$WATCHDOG_STATE" 2>/dev/null || echo 0 ;;
 daemon-reexec) echo 15s > "$WATCHDOG_STATE" ;;
esac
''')

    def sandbox(text):
        for path in ('/etc/', '/usr/sbin/', '/usr/lib/asterisk/', '/var/lib/phonelet/',
                     '/var/spool/', '/dev/dongle0-at', '/dev/dongle0-audio'):
            text = text.replace(path, str(tmp_path) + path)
        text = text.replace('test -c /dev/watchdog0', 'test -c /dev/null')
        return text

    remote = (ROOT / 'scripts/bootstrap.sh').read_text()
    profile = sandbox((ROOT / 'targets/orangepi-zero2w/profile.sh').read_text())
    (stage / 'target/profile.sh').write_text(profile)
    preamble = sandbox(remote[:remote.index('wait_for_asterisk()')])
    install = sandbox(remote[remote.index('export DEBIAN_FRONTEND'):remote.index('if ! asterisk_installed "$ASTERISK_SUM"')])
    unpack = sandbox(remote[remote.index('if ! asterisk_installed "$ASTERISK_SUM"'):remote.index('# First install only')])
    helpers = sandbox(remote[remote.index('wait_for_asterisk()'):remote.index("USAGE='Usage:")])

    def run(settings=None, phase='install', mounted=False, archive='', unpack_archive=False,
            log_storage=None, sms_storage='ext4 /dev/sda1', arguments=None):
        source = preamble
        if phase == 'prepare-branch':  # the whole script in prepare mode
            source = sandbox(remote)
            phase = 'prepare'
        else:
            source += f'\ncheck_host {phase} "{archive}"\n'
        if phase == 'install':
            source += install
            if unpack_archive:
                source += helpers + '\nasterisk_installed() { return 1; }\n' + unpack
        script_path = tmp_path / 'bootstrap.sh'
        script_path.write_text(source)
        env = os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'], 'STAGE': str(stage),
                            'COMMAND_LOG': str(log), 'RAMLOG_MOUNT': str(int(mounted)),
                            'LOG_STORAGE': log_storage if log_storage is not None else ('tmpfs ramlog' if mounted else 'ext4 /dev/sda1'),
                            'ARCHIVE': str(stage / archive) if archive else '', 'ASTERISK_SUM': '', 'DONGLE_SUM': '',
                            'SMS_STORAGE': sms_storage, 'FINDMNT_LOG': str(tmp_path / 'findmnt.log'),
                            'WATCHDOG_STATE': str(watchdog_state), 'PHONELET_BOOTSTRAP_TARGET': '',
                            'TARGET_DIR': str(stage / 'target') if (settings or {}).get('PHONELET_BOOTSTRAP_TARGET') else '',
                            'PHONELET_BOOTSTRAP_WIFI': '0', 'PHONELET_BOOTSTRAP_MODEM_USB_ID': '12d1:1001'} | (settings or {})
        result = subprocess.run(['bash', str(script_path), phase] + (arguments or []), capture_output=True, text=True,
                                env=env)
        return result, log.read_text().splitlines() if log.exists() else []

    return SimpleNamespace(run=run, root=tmp_path, ramlog=ramlog, stage=stage, remote=remote, sandbox=sandbox)


def test_bootstrap_generic_host_leaves_board_and_optional_network_settings_alone(bootstrap_host_steps):
    b = bootstrap_host_steps
    result, calls = b.run()
    assert result.returncode == 0, result.stderr
    assert b.ramlog.read_text() == 'ENABLED=true\n'
    assert not any(word in '\n'.join(calls) for word in ('gpiod', 'watchdog', 'modem-power', 'tailscale', 'resolved', 'netplan'))
    for path in ('etc/systemd/system.conf.d/90-phonelet-watchdog.conf',
                 'etc/systemd/system/phonelet-modem-power.service'):
        assert not (b.root / path).exists()
    rules = (b.root / 'etc/udev/rules.d/99-phonelet-modem.rules').read_text()
    assert rules.count('ATTRS{idVendor}=="12d1", ATTRS{idProduct}=="1001"') == 3
    for interface, name in (('00', 'modem'), ('01', 'audio'), ('02', 'at')):
        assert f'ENV{{ID_USB_INTERFACE_NUM}}=="{interface}", SYMLINK+="dongle0-{name}"' in rules
    assert 'PHONELET_BOOTSTRAP_MODEM_USB_ID=12d1:PID' in result.stderr
    assert calls[-1] == 'udevadm settle'


@pytest.mark.parametrize('phase', ['prepare', 'install'])
@pytest.mark.parametrize('hooks', [(), ('target_check_storage',), ('target_install_packages',), ('target_setup',),
                                  ('target_check_storage', 'target_setup')])
def test_bootstrap_skips_undefined_hooks_and_provides_checkout_target_files_in_both_phases(bootstrap_host_steps,
                                                                                       phase, hooks):
    b = bootstrap_host_steps
    profile = ''
    for hook in hooks:
        profile += hook + '''() {
  [[ -f "$TARGET_DIR/profile.sh" ]] || die 'target files missing'
  echo "''' + hook + ''' ${1:-install}" >> "$COMMAND_LOG"
}
'''
    (b.stage / 'target/profile.sh').write_text(profile)
    result, calls = b.run({'PHONELET_BOOTSTRAP_TARGET': 'synthetic'}, phase=phase)
    assert result.returncode == 0, result.stderr
    expected = [hook + ' ' + phase for hook in hooks if phase == 'install' or hook == 'target_check_storage']
    assert [call for call in calls if call.startswith('target_')] == expected


def test_bootstrap_profile_and_all_options_keep_the_original_host_setup_order(bootstrap_host_steps):
    b = bootstrap_host_steps
    result, calls = b.run({'PHONELET_BOOTSTRAP_TARGET': 'orangepi-zero2w', 'PHONELET_BOOTSTRAP_WIFI': '1'})
    assert result.returncode == 0, result.stderr
    markers = ['apt-get install -y --no-install-recommends netplan.io wpasupplicant iw',
               'apt-get install -y --no-install-recommends gpiod', 'systemctl daemon-reexec',
               'systemctl enable phonelet-modem-power', 'systemctl start phonelet-modem-power',
               'systemctl is-active --quiet phonelet-modem-power', 'udevadm control --reload-rules', 'udevadm settle']
    order = [calls.index(marker) for marker in markers]
    assert order == sorted(order)
    assert 'RuntimeWatchdogSec=15s' in (b.root / 'etc/systemd/system.conf.d/90-phonelet-watchdog.conf').read_text()
    remote = (ROOT / 'scripts/bootstrap.sh').read_text()
    assert remote.index('then target_setup; fi') < remote.rindex('\nstart_native_asterisk\n')


def test_bootstrap_board_profile_does_not_enable_optional_networking(bootstrap_host_steps):
    result, calls = bootstrap_host_steps.run({'PHONELET_BOOTSTRAP_TARGET': 'orangepi-zero2w'})
    assert result.returncode == 0, result.stderr
    assert 'apt-get install -y --no-install-recommends gpiod' in calls
    assert 'systemctl start phonelet-modem-power' in calls
    assert not any(word in '\n'.join(calls) for word in ('tailscale', 'resolved', 'netplan', 'wpasupplicant', ' iw'))


@pytest.mark.parametrize('option, marker', [
    ('WIFI', 'apt-get install -y --no-install-recommends netplan.io wpasupplicant iw'),
])
def test_bootstrap_optional_settings_are_independent_of_the_board(bootstrap_host_steps, option, marker):
    result, calls = bootstrap_host_steps.run({'PHONELET_BOOTSTRAP_' + option: '1'})
    assert result.returncode == 0, result.stderr
    assert marker in calls
    assert not any('gpiod' in call or 'modem-power' in call or 'Watchdog' in call for call in calls)


@pytest.mark.parametrize('selected', [False, True])
def test_bootstrap_only_selected_profile_disables_ramlog_and_requests_reboot(bootstrap_host_steps, selected):
    b = bootstrap_host_steps
    result, calls = b.run({'PHONELET_BOOTSTRAP_TARGET': 'orangepi-zero2w' if selected else ''},
                          phase='prepare', mounted=True)
    assert result.returncode == (3 if selected else 1)
    assert b.ramlog.read_text() == ('ENABLED=false\n' if selected else 'ENABLED=true\n')
    assert ('reboot the board and run again' in result.stderr) == selected
    assert calls == ['systemctl list-unit-files --no-legend']


def test_bootstrap_huawei_usb_id_is_used_for_every_interface(bootstrap_host_steps):
    b = bootstrap_host_steps
    result, _ = b.run({'PHONELET_BOOTSTRAP_MODEM_USB_ID': '12d1:1003'})
    assert result.returncode == 0, result.stderr
    rules = (b.root / 'etc/udev/rules.d/99-phonelet-modem.rules').read_text()
    assert rules.count('ATTRS{idProduct}=="1003"') == 3
    assert '1001' not in rules


@pytest.mark.parametrize('arch, os_release', [('armv7l', 'ID=debian\nVERSION_CODENAME=trixie\n'),
                                            ('aarch64', 'ID=debian\nVERSION_CODENAME=bookworm\n'),
                                            ('aarch64', 'ID=ubuntu\nVERSION_CODENAME=trixie\n')])
def test_bootstrap_rejects_unsupported_hosts_before_installation(bootstrap_host_steps, arch, os_release):
    b = bootstrap_host_steps
    (b.root / 'etc/os-release').write_text(os_release)
    result, calls = b.run({'FAKE_ARCH': arch})
    assert result.returncode == 1 and calls == []


@pytest.mark.parametrize('arch, debian_arch', [('aarch64', 'arm64'), ('x86_64', 'amd64')])
def test_bootstrap_unpacks_the_archive_matching_the_target_debian_architecture(bootstrap_host_steps, arch, debian_arch):
    b = bootstrap_host_steps
    archive = b.stage / f'asterisk-20.22.0-{debian_arch}.tar.gz'
    archive.write_bytes(b'fake build')
    digest = subprocess.run(['sha256sum', str(archive)], check=True, capture_output=True, text=True).stdout.split()[0]
    (b.stage / 'artifacts.sha256').write_text(f'{digest}  {archive.name}\n')
    result, calls = b.run({'FAKE_ARCH': arch, 'FAKE_DPKG_ARCH': debian_arch}, archive=archive.name,
                          unpack_archive=True)
    assert result.returncode == 0, result.stderr
    assert calls[-2:] == [f'tar -C / --exclude=./var/run --keep-directory-symlink --no-overwrite-dir -xzf {archive}', 'ldconfig']
    assert archive.read_bytes() == b'fake build'


@pytest.mark.parametrize('phase', ['prepare', 'install'])
@pytest.mark.parametrize('arch, debian_arch, archive_arch', [('aarch64', 'arm64', 'amd64'),
                                                        ('x86_64', 'amd64', 'arm64')])
def test_bootstrap_refuses_archive_mismatch_before_any_host_changes(bootstrap_host_steps, phase, arch,
                                                                 debian_arch, archive_arch):
    b = bootstrap_host_steps
    result, calls = b.run({'FAKE_ARCH': arch, 'FAKE_DPKG_ARCH': debian_arch,
                           'PHONELET_BOOTSTRAP_TARGET': 'orangepi-zero2w'}, phase=phase, mounted=True,
                          archive=f'asterisk-20.22.0-{archive_arch}.tar.gz')
    assert result.returncode == 1 and calls == []
    assert f'archive architecture {archive_arch} does not match host architecture {debian_arch}' in result.stderr
    assert b.ramlog.read_text() == 'ENABLED=true\n'


@pytest.mark.parametrize('phase', ['prepare', 'install'])
@pytest.mark.parametrize('debian_arch', ['armhf', 'i386', 'riscv64'])
def test_bootstrap_refuses_unsupported_debian_architecture(bootstrap_host_steps, phase, debian_arch):
    result, calls = bootstrap_host_steps.run({'FAKE_ARCH': 'x86_64', 'FAKE_DPKG_ARCH': debian_arch}, phase=phase)
    assert result.returncode == 1 and calls == []
    assert f'Unsupported host architecture: {debian_arch}' in result.stderr


@pytest.mark.parametrize('phase', ['prepare', 'install'])
def test_bootstrap_remote_refuses_an_unsupported_archive_architecture(bootstrap_host_steps, phase):
    result, calls = bootstrap_host_steps.run(phase=phase, archive='asterisk-20.22.0-armhf.tar.gz')
    assert result.returncode == 1 and calls == []
    assert 'Unsupported Asterisk archive' in result.stderr


@pytest.mark.parametrize('phase', ['prepare', 'install'])
@pytest.mark.parametrize('storage, mounted, persistent', [
    pytest.param('tmpfs tmpfs', True, False, id='separate-tmpfs'),
    pytest.param('tmpfs tmpfs', False, False, id='inherited-tmpfs'),
    pytest.param('ext4 /dev/zram0', True, False, id='separate-zram'),
    pytest.param('ext4 /dev/zram0[/log]', False, False, id='inherited-zram'),
    pytest.param('ext4 /dev/sda2', True, True, id='persistent-partition'),
    pytest.param('ext4 /dev/sda1', False, True, id='root-filesystem'),
])
def test_bootstrap_requires_persistent_log_storage(bootstrap_host_steps, phase, storage, mounted, persistent):
    result, calls = bootstrap_host_steps.run(phase=phase, mounted=mounted, log_storage=storage)
    assert result.returncode == (0 if persistent else 1), result.stderr
    assert ('/var/log is on tmpfs, ramfs or zram; use persistent /var/log storage' in result.stderr) == (not persistent)
    if not persistent:
        assert calls == ['systemctl list-unit-files --no-legend']


ASTERISK_SUMS = ['a' * 64, 'b' * 64]


def install_synthetic_asterisk(root, binary=b'asterisk binary', module=b'chan_dongle module', marker=True):
    """Write a synthetic installed Asterisk under the sandbox root; return the sums of what was written."""
    for path, content in (('usr/sbin/asterisk', binary), ('usr/lib/asterisk/modules/chan_dongle.so', module)):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    if marker:
        (root / 'var/lib/phonelet').mkdir(parents=True, exist_ok=True)
        (root / 'var/lib/phonelet/asterisk-bootstrap').write_text('asterisk-20.22.0-arm64.tar.gz\n')
    return [hashlib.sha256(binary).hexdigest(), hashlib.sha256(module).hexdigest()]


@pytest.mark.parametrize('foreign', ['binary', 'configs'])
def test_bootstrap_prepare_refuses_a_foreign_asterisk_before_any_change(bootstrap_host_steps, foreign):
    b = bootstrap_host_steps
    if foreign == 'binary':
        install_synthetic_asterisk(b.root, marker=False)
    else:
        (b.root / 'etc/asterisk').mkdir()
        (b.root / 'etc/asterisk/modules.conf').write_text('[modules]\n')
    # The selected profile would disable ramlog: the refusal comes first.
    result, calls = b.run({'PHONELET_BOOTSTRAP_TARGET': 'orangepi-zero2w'}, phase='prepare-branch', mounted=True,
                          arguments=['asterisk-20.22.0-arm64.tar.gz'])
    assert result.returncode == 1 and result.stdout == ''
    assert 'Another Asterisk installation found' in result.stderr
    assert 'bootstrap does not replace it' in result.stderr and '"Manual installation"' in result.stderr
    assert calls == ['systemctl list-unit-files --no-legend']
    assert b.ramlog.read_text() == 'ENABLED=true\n'


@pytest.mark.parametrize('state, answer', [
    pytest.param('fresh', 'missing', id='fresh-host'),
    pytest.param('empty-configs', 'missing', id='empty-etc-asterisk'),
    pytest.param('ours', 'installed', id='marker-and-both-sums'),
    pytest.param('other-module', 'missing', id='marker-and-chan-dongle-differs'),
    pytest.param('other-binary', 'missing', id='marker-and-asterisk-differs'),
    pytest.param('marker-only', 'missing', id='interrupted-before-unpack'),
])
def test_bootstrap_recognises_only_our_asterisk_build(bootstrap_host_steps, state, answer):
    b = bootstrap_host_steps
    sums = ASTERISK_SUMS
    if state == 'empty-configs':
        (b.root / 'etc/asterisk').mkdir()
    elif state == 'marker-only':
        (b.root / 'var/lib/phonelet').mkdir(parents=True)
        (b.root / 'var/lib/phonelet/asterisk-bootstrap').write_text('asterisk-20.22.0-arm64.tar.gz\n')
    elif state != 'fresh':
        sums = install_synthetic_asterisk(b.root)
        (b.root / 'etc/asterisk').mkdir()
        (b.root / 'etc/asterisk/modules.conf').write_text('[modules]\n')
        if state == 'other-module':
            sums = [sums[0], 'c' * 64]
        elif state == 'other-binary':
            sums = ['c' * 64, sums[1]]
    # The install phase unpacks the archive again unless this holds.
    helpers = b.sandbox(b.remote[:b.remote.index("USAGE='Usage:")])
    result = subprocess.run(['bash', '-c', helpers + f'\nif asterisk_installed {sums[0]} {sums[1]}; '
                             'then echo installed; else echo missing; fi\n'],
                            capture_output=True, text=True, env=os.environ | {'TARGET_DIR': ''})
    assert result.returncode == 0, result.stderr
    assert result.stdout == answer + '\n'
    # Every state also passes the host check of prepare (a rerun on a marked host included).
    prepared, _ = b.run(phase='prepare-branch', arguments=['asterisk-20.22.0-arm64.tar.gz'])
    assert prepared.returncode == 0, prepared.stderr


def test_bootstrap_prepare_takes_at_most_the_archive_name(bootstrap_host_steps):
    result, calls = bootstrap_host_steps.run(phase='prepare-branch', arguments=['a' * 64, 'b' * 64])
    assert result.returncode == 1 and calls == []
    assert 'Usage: bootstrap.sh prepare [ARCHIVE]' in result.stderr


def test_bootstrap_prepare_refuses_a_container_before_any_change(bootstrap_host_steps):
    b = bootstrap_host_steps
    # The selected profile would disable ramlog: the refusal comes first.
    result, calls = b.run({'PHONELET_BOOTSTRAP_TARGET': 'orangepi-zero2w', 'FAKE_CONTAINER': '1'},
                          phase='prepare-branch', mounted=True, arguments=['asterisk-20.22.0-arm64.tar.gz'])
    assert result.returncode == 1 and result.stdout == ''
    assert 'Containers are not supported' in result.stderr
    assert calls == []
    assert b.ramlog.read_text() == 'ENABLED=true\n'


def test_bootstrap_install_writes_the_ownership_marker_before_unpacking(bootstrap_host_steps):
    b = bootstrap_host_steps
    archive = b.stage / 'asterisk-20.22.0-arm64.tar.gz'
    archive.write_bytes(b'fake build')
    (b.stage / 'artifacts.sha256').write_text(f'{hashlib.sha256(b"fake build").hexdigest()}  {archive.name}\n')
    result, calls = b.run(archive=archive.name, unpack_archive=True)
    assert result.returncode == 0, result.stderr
    marker = b.root / 'var/lib/phonelet/asterisk-bootstrap'
    assert marker.read_text() == archive.name + '\n'
    assert marker.stat().st_mode & 0o777 == 0o644
    assert calls[-2].startswith('tar -C / ')
    unpack = b.remote[b.remote.index('unpack_asterisk() {'):]
    assert unpack.index('> "$ASTERISK_MARKER"') < unpack.index('tar -C /')


@pytest.mark.parametrize('phase', ['prepare', 'install'])
@pytest.mark.parametrize('storage, persistent', [('tmpfs tmpfs', False), ('ramfs ramfs', False),
                                                 ('ext4 /dev/zram1', False), ('ext4 /dev/sda1', True)])
def test_bootstrap_requires_persistent_sms_journal_storage(bootstrap_host_steps, phase, storage, persistent):
    b = bootstrap_host_steps
    (b.root / 'var/spool/asterisk').mkdir(parents=True)
    result, calls = b.run(phase=phase, sms_storage=storage)
    assert result.returncode == (0 if persistent else 1), result.stderr
    assert ('/var/spool/asterisk/phonelet-sms is on tmpfs, ramfs or zram' in result.stderr) == (not persistent)
    # The journal directory does not exist yet: its nearest existing ancestor is checked.
    assert (b.root / 'findmnt.log').read_text().splitlines() == [str(b.root / 'var/spool/asterisk')]
    if not persistent:
        assert calls == ['systemctl list-unit-files --no-legend']


@pytest.mark.parametrize('mounted, log_storage, status', [
    pytest.param(True, 'tmpfs ramlog', 1, id='still-volatile-mount'),
    pytest.param(True, 'ext4 /dev/sda2', 1, id='persistent-separate-mount'),
    pytest.param(False, 'ext4 /dev/sda1', 0, id='rebooted-root-filesystem'),
])
def test_bootstrap_board_profile_with_ramlog_already_disabled(bootstrap_host_steps, mounted, log_storage, status):
    b = bootstrap_host_steps
    b.ramlog.write_text('ENABLED=false\n')
    result, calls = b.run({'PHONELET_BOOTSTRAP_TARGET': 'orangepi-zero2w'}, phase='prepare', mounted=mounted,
                          log_storage=log_storage)
    assert result.returncode == status, result.stderr
    assert ('armbian-ramlog is already off, but /var/log is still a separate mount' in result.stderr) == mounted
    assert ('reboot it first' in result.stderr) == mounted
    assert 'reboot the board and run again' not in result.stderr
    assert b.ramlog.read_text() == 'ENABLED=false\n'


def test_bootstrap_installs_logrotate_with_the_runtime_packages(bootstrap_host_steps):
    result, calls = bootstrap_host_steps.run()
    assert result.returncode == 0, result.stderr
    packages = next(call for call in calls if call.startswith('apt-get install') and 'python3' in call).split()
    assert 'logrotate' in packages


@pytest.mark.parametrize('arch', ['arm64', 'amd64'])
def test_asterisk_build_names_archive_and_checksums_by_container_architecture(tmp_path, arch):
    commands = tmp_path / 'bin'
    executable(commands / 'dpkg', f'[[ "$*" == --print-architecture ]] || exit 1; echo {arch}\n')
    modules = tmp_path / 'stage/usr/lib/asterisk/modules'
    modules.mkdir(parents=True)
    (modules / 'chan_dongle.so').write_bytes(b'fake module')
    (tmp_path / 'stage/usr/sbin').mkdir(parents=True)
    (tmp_path / 'stage/usr/sbin/asterisk').write_bytes(b'fake binary')
    source = (ROOT / 'scripts/build-asterisk.sh').read_text()
    packaging = source[source.rindex('cd /work'):].replace('/work', str(tmp_path))
    result = subprocess.run(['bash', '-euc', packaging], capture_output=True, text=True,
                            env=os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'],
                                              'ASTERISK_VERSION': '20.22.0'})
    assert result.returncode == 0, result.stderr
    assert [path.name for path in tmp_path.glob('*.tar.gz')] == [f'asterisk-20.22.0-{arch}.tar.gz']
    checked = subprocess.run(['sha256sum', '--check', 'artifacts.sha256'], cwd=tmp_path,
                             capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    assert len(checked.stdout.splitlines()) == 3


def test_asterisk_build_ships_the_list_of_its_files_in_the_archive(tmp_path):
    """docs/operations.md, "Removing Phonelet": the archive's regular files and symlinks, itself included."""
    commands = tmp_path / 'bin'
    executable(commands / 'dpkg', '[[ "$*" == --print-architecture ]] || exit 1; echo arm64\n')
    stage = tmp_path / 'stage'
    for name in ('usr/lib/asterisk/modules/chan_dongle.so', 'usr/sbin/asterisk', 'usr/lib/libasteriskpj.so.2',
                 'usr/lib/systemd/system/asterisk.service', 'etc/asterisk/extensions.conf'):
        (stage / name).parent.mkdir(parents=True, exist_ok=True)
        (stage / name).write_bytes(b'fake ' + name.encode())
    (stage / 'usr/lib/libasteriskpj.so').symlink_to('libasteriskpj.so.2')
    (stage / 'var/run/asterisk').mkdir(parents=True)  # directories are not listed
    source = (ROOT / 'scripts/build-asterisk.sh').read_text()
    packaging = source[source.rindex('cd /work'):].replace('/work', str(tmp_path))
    result = subprocess.run(['bash', '-euc', packaging], capture_output=True, text=True,
                            env=os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'],
                                              'ASTERISK_VERSION': '20.22.0'})
    assert result.returncode == 0, result.stderr
    listed = subprocess.run(['tar', '-xzOf', str(tmp_path / 'asterisk-20.22.0-arm64.tar.gz'),
                             './usr/share/phonelet/asterisk.files'], check=True, capture_output=True, text=True).stdout
    assert listed.splitlines() == ['/etc/asterisk/extensions.conf', '/usr/lib/asterisk/modules/chan_dongle.so',
                                   '/usr/lib/libasteriskpj.so', '/usr/lib/libasteriskpj.so.2',
                                   '/usr/lib/systemd/system/asterisk.service', '/usr/sbin/asterisk',
                                   '/usr/share/phonelet/asterisk.files']
    members = subprocess.run(['tar', '-tzf', str(tmp_path / 'asterisk-20.22.0-arm64.tar.gz')], check=True,
                             capture_output=True, text=True).stdout.splitlines()
    assert sorted(member[1:] for member in members if not member.endswith('/')) == listed.splitlines()


def test_asterisk_build_installs_the_upstream_unit_and_no_init_script_or_defaults():
    source = (ROOT / 'scripts/build-asterisk.sh').read_text()
    assert 'make config' not in source.replace('make config\'s', '')
    assert ('install -D -m 0644 contrib/systemd/asterisk.service /work/stage/usr/lib/systemd/system/asterisk.service'
            in source)
    assert '/repo/asterisk/systemd/asterisk.service.d/10-phonelet.conf' in source
    assert 'test ! -e /work/stage/etc/init.d\n' in source and 'test ! -e /work/stage/etc/default\n' in source
    dropin = (ROOT / 'asterisk/systemd/asterisk.service.d/10-phonelet.conf').read_text()
    settings = [line for line in dropin.splitlines() if line and not line.startswith(('#', '['))]
    # Every difference from the upstream unit; the upstream ExecStart, Restart=always and PrivateTmp stay.
    assert settings == ['Wants=network-online.target', 'After=network-online.target',
                        'After=time-set.target', 'StartLimitIntervalSec=10min', 'StartLimitBurst=5', 'Type=simple', 'User=', 'Group=',
                        'TimeoutStopSec=45', 'KillMode=process', 'LimitCORE=0', 'UMask=0077', 'StandardError=journal',
                        'NoNewPrivileges=yes', 'ProtectSystem=full', 'ProtectHome=read-only',
                        'ProtectKernelTunables=yes', 'ProtectKernelModules=yes', 'ProtectControlGroups=yes',
                        'LockPersonality=yes']


def test_modem_power_unit_holds_high_in_the_foreground_before_asterisk():
    unit = (ROOT / 'targets/orangepi-zero2w/phonelet-modem-power.service').read_text()
    assert 'Type=simple\n' in unit and 'Restart=always\n' in unit and 'RestartSec=2\n' in unit
    assert 'Environment=MODEM_POWER_CHIP=gpiochip1\n' in unit
    assert 'Environment=MODEM_POWER_LINE=228\n' in unit
    assert ('ExecStart=/usr/bin/gpioset -c ${MODEM_POWER_CHIP} -C modem-power '
            '${MODEM_POWER_LINE}=1\n') in unit
    assert 'Before=asterisk.service\n' in unit
    assert 'WantedBy=multi-user.target asterisk.service\n' in unit
    assert 'StartLimitIntervalSec=0\n' in unit  # a holder that keeps exiting is retried forever
    assert not re.search(r'ExecStop|\}=0\b|(?:^|\s)-z\b', unit)


@pytest.fixture
def modem_power_bootstrap(tmp_path):
    """Run only the power/udev bootstrap step, with fake systemctl, time and USB ports."""
    commands, stage = tmp_path / 'bin', tmp_path / 'stage'
    (stage / 'target').mkdir(parents=True)
    source = ROOT / 'targets/orangepi-zero2w/phonelet-modem-power.service'
    shutil.copyfile(source, stage / 'target' / source.name)
    unit, log, ticks = [tmp_path / name for name in ('installed.service', 'commands.log', 'ticks')]
    ports = [tmp_path / name for name in ('at', 'audio')]
    executable(commands / 'systemctl', '''echo "systemctl $*" >> "$COMMAND_LOG"
if [[ "$1" == start && "$POWER_STATE" == start-failed ]]; then exit 1; fi
if [[ "$1" == is-active && "$POWER_STATE" == busy ]]; then exit 1; fi
''')
    executable(commands / 'install', '''echo install >> "$COMMAND_LOG"
exec /usr/bin/install "$@"
''')
    executable(commands / 'udevadm', 'echo "udevadm $*" >> "$COMMAND_LOG"\n')
    executable(commands / 'sleep', '''echo "sleep $*" >> "$COMMAND_LOG"
count=$(cat "$TICKS" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$TICKS"
if [[ "$count" == "$PORTS_AFTER" ]]; then
  ln -s /dev/null "$AT_PORT"
  ln -s /dev/null "$AUDIO"
fi
''')
    remote = (ROOT / 'scripts/bootstrap.sh').read_text()
    profile = (ROOT / 'targets/orangepi-zero2w/profile.sh').read_text()
    assert 'apt-get install -y --no-install-recommends gpiod' in profile
    step = profile[profile.index('  # Power must be on'):profile.rindex('\n}')] + '\n'
    step += remote[remote.index('# Read the validated, rendered paths'):remote.index('if ! asterisk_installed "$ASTERISK_SUM"')]
    assert 'modem not found' in step
    assert step.index('systemctl start phonelet-modem-power') < step.index('modem not found')
    step = step.replace('/etc/udev/rules.d/99-phonelet-modem.rules', str(tmp_path / 'udev.rules'))
    step = step.replace('/dev/dongle0-at', str(ports[0])).replace('/dev/dongle0-audio', str(ports[1]))

    def run(state='running', ports_after=4, installed=False, env=''):
        if installed:
            shutil.copyfile(source, unit)
        (stage / 'etc-asterisk').mkdir(exist_ok=True)
        values = script('deploy-release').dotenv_values(stream=io.StringIO(env))
        data = values.get('AST_DONGLE_DATA', str(ports[0]))
        audio = values.get('AST_DONGLE_AUDIO', str(ports[1]))
        (stage / 'etc-asterisk/dongle.conf').write_text(f'data={data}\naudio={audio}\n')
        result = subprocess.run(['bash', '-c', 'set -euo pipefail\n'
                                 'die() { echo "$*" >&2; exit 1; }\n' + step],
                                capture_output=True, text=True,
                                env=os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'],
                                                  'TARGET_DIR': str(stage / 'target'), 'MODEM_POWER_UNIT': str(unit),
                                                  'STAGE': str(stage),
                                                  'PHONELET_BOOTSTRAP_MODEM_USB_ID': '12d1:1001',
                                                  'COMMAND_LOG': str(log), 'POWER_STATE': state,
                                                  'TICKS': str(ticks), 'PORTS_AFTER': str(ports_after),
                                                  'AT_PORT': str(ports[0]), 'AUDIO': str(ports[1])})
        return result, log.read_text().splitlines()

    return SimpleNamespace(run=run, unit=unit, source=source, log=log)


def test_bootstrap_powers_modem_before_usb_and_does_not_reinstall_or_restart(modem_power_bootstrap):
    b = modem_power_bootstrap
    result, calls = b.run()
    assert result.returncode == 0, result.stderr
    assert b.unit.read_bytes() == b.source.read_bytes()
    assert calls[:5] == ['install', 'systemctl daemon-reload', 'systemctl enable phonelet-modem-power',
                         'systemctl start phonelet-modem-power', 'sleep 1']
    assert calls.index('systemctl is-active --quiet phonelet-modem-power') < calls.index('udevadm settle')
    assert calls.count('sleep 1') == 4  # delayed USB enumeration is accepted
    before = b.unit.stat().st_mtime_ns
    result, calls = b.run()
    assert result.returncode == 0, result.stderr
    assert calls.count('install') == calls.count('systemctl daemon-reload') == 1
    assert b.unit.stat().st_mtime_ns == before
    assert not any('restart' in call or 'stop' in call or 'kill' in call for call in calls)


@pytest.mark.parametrize('state', ['busy', 'start-failed'])
def test_bootstrap_stops_on_a_failed_gpio_holder_without_killing_it(modem_power_bootstrap, state):
    result, calls = modem_power_bootstrap.run(state=state, installed=True)
    assert result.returncode == 1
    assert 'Device or resource busy' in result.stderr
    assert 'targets/orangepi-zero2w/README.md#modem-power' in result.stderr
    assert not any('udevadm' in call or 'kill' in call or 'stop' in call for call in calls)
    assert 'install' not in calls and 'systemctl daemon-reload' not in calls


def test_bootstrap_waits_30_seconds_before_reporting_a_missing_modem(modem_power_bootstrap):
    result, calls = modem_power_bootstrap.run(ports_after=100)
    assert result.returncode == 1
    assert 'modem not found after 30 s' in result.stderr
    assert calls.count('sleep 1') == 31  # holder startup check, then USB wait
    assert not any(word in result.stderr for word in ('Orange Pi', 'board', 'USB-C'))
    assert 'USB host port' in result.stderr
    assert any('udevadm' in call for call in calls)


def test_bootstrap_waits_for_the_rendered_modem_ports_from_quoted_dotenv_keys(modem_power_bootstrap, tmp_path):
    b = modem_power_bootstrap
    data, audio = tmp_path / 'ttyCustomData', tmp_path / 'ttyCustomAudio'
    env = f"'AST_DONGLE_DATA'='{data}'\nexport AST_DONGLE_AUDIO=\"{audio}\" # interface 01\n"
    # The default ports appear, the configured ones do not: the wait names the configured ones.
    result, calls = b.run(ports_after=2, env=env)
    assert result.returncode == 1
    assert 'modem not found after 30 s' in result.stderr
    assert f'({data}, {audio})' in result.stderr
    assert str(tmp_path / 'at') not in result.stderr and 'dongle0' not in result.stderr
    assert calls.count('sleep 1') == 31
    data.symlink_to('/dev/null')
    audio.symlink_to('/dev/null')
    b.log.unlink()
    result, calls = b.run(env=env)
    assert result.returncode == 0, result.stderr
    assert calls.count('sleep 1') == 1  # only the holder startup check: the ports are there


def test_bootstrap_scripts_send_no_modem_commands_and_unpack_over_the_run_link():
    text = ''.join(path.read_text() for path in [ROOT / 'install.sh', ROOT / 'scripts/bootstrap.sh',
                                                *sorted((ROOT / 'targets').glob('*/profile.sh'))])
    assert not re.search(r'\bAT[+^&]|dongle cmd|ulimit', text)
    extractions = re.findall(r'^\s*tar .*-x.*$', text, re.M)
    assert extractions == ['  tar -C / --exclude=./var/run "$@" --keep-directory-symlink --no-overwrite-dir '
                           '-xzf "$archive"']


@pytest.mark.parametrize('phase', ['install', 'upgrade'])
def test_bootstrap_extraction_skips_the_archive_var_run(tmp_path, phase):
    # The layout of scripts/build-asterisk.sh: make install puts ./var/run/asterisk/ into the stage.
    stage = tmp_path / 'stage'
    for name in ('usr/sbin', 'var/run/asterisk', 'var/lib/asterisk', 'etc/asterisk'):
        (stage / name).mkdir(parents=True)
    (stage / 'usr/sbin/asterisk').write_text('binary')
    (stage / 'etc/asterisk/extensions.conf').write_text('sample')
    archive = tmp_path / 'asterisk-20.22.0-amd64.tar.gz'
    subprocess.run(['tar', '-C', str(stage), '-czf', str(archive), '.'], check=True)
    root = tmp_path / 'root'
    (root / 'run').mkdir(parents=True)
    (root / 'var').mkdir()
    (root / 'var/run').symlink_to('../run')
    (root / 'etc/asterisk').mkdir(parents=True)
    (root / 'etc/asterisk/extensions.conf').write_text('deployed')
    remote = (ROOT / 'scripts/bootstrap.sh').read_text()
    line = re.search(r'^\s*(tar -C / .*-xzf) "\$archive"$', remote, re.M).group(1)
    # install unpacks everything; upgrade keeps the deployed /etc/asterisk.
    assert 'unpack_asterisk "$ARCHIVE"\n' in remote
    assert 'unpack_asterisk "$archive" --exclude=./etc/asterisk\n' in remote
    options = ['--exclude=./etc/asterisk'] if phase == 'upgrade' else []
    args = shlex.split(line.replace('"$@"', ' '.join(options)))
    args[args.index('/')] = str(root)
    subprocess.run([*args, str(archive)], check=True)
    assert (root / 'usr/sbin/asterisk').read_text() == 'binary'
    assert (root / 'var/lib/asterisk').is_dir()
    assert (root / 'var/run').is_symlink() and list((root / 'run').iterdir()) == []
    assert (root / 'etc/asterisk/extensions.conf').read_text() == ('deployed' if phase == 'upgrade' else 'sample')


@pytest.fixture
def asterisk_upgrade(tmp_path):
    """bootstrap.sh upgrade of a synthetic bootstrapped host: temporary root, fake systemd and Asterisk."""
    root, commands, stage = tmp_path / 'root', tmp_path / 'bin', tmp_path / 'stage'
    stage.mkdir()
    log, state = tmp_path / 'commands.log', tmp_path / 'running'
    # The previous bootstrapped build on the host: binaries, the native unit with its drop-in, the deployed config.
    install_synthetic_asterisk(root, binary=b'previous asterisk', module=b'previous chan_dongle')
    old = {'usr/lib/systemd/system/asterisk.service': b'[Service]\nExecStart=/usr/sbin/asterisk -mqf\n',
           'usr/lib/systemd/system/asterisk.service.d/10-phonelet.conf': b'[Service]\n# previous\nType=simple\n',
           'etc/asterisk/extensions.conf': b'deployed dialplan\n'}
    for name, content in old.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    state.write_text('100\n')  # the previous Asterisk, run by asterisk.service
    (tmp_path / 'fragment').write_text(str(root / 'usr/lib/systemd/system/asterisk.service') + '\n')
    # The new archive: new binaries, the native unit and samples that must not replace /etc/asterisk.
    build = tmp_path / 'build'
    files = {'usr/sbin/asterisk': b'new asterisk', 'usr/lib/asterisk/modules/chan_dongle.so': b'new chan_dongle',
             'usr/lib/systemd/system/asterisk.service': b'[Service]\nExecStart=/usr/sbin/asterisk -mqf\n',
             'usr/lib/systemd/system/asterisk.service.d/10-phonelet.conf': b'[Service]\nType=simple\n',
             'usr/share/phonelet/asterisk.files': b'/usr/sbin/asterisk\n', 'etc/asterisk/extensions.conf': b'sample\n'}
    for name, content in files.items():
        (build / name).parent.mkdir(parents=True, exist_ok=True)
        (build / name).write_bytes(content)
    (build / 'var/run/asterisk').mkdir(parents=True)
    archive = stage / 'asterisk-20.22.0-arm64.tar.gz'
    subprocess.run(['tar', '-C', str(build), '-czf', str(archive), '.'], check=True)
    digest = lambda content: hashlib.sha256(content).hexdigest()
    (stage / 'artifacts.sha256').write_text(
        f'{digest(archive.read_bytes())}  {archive.name}\n'
        f'{digest(files["usr/sbin/asterisk"])}  stage/usr/sbin/asterisk\n'
        f'{digest(files["usr/lib/asterisk/modules/chan_dongle.so"])}  stage/usr/lib/asterisk/modules/chan_dongle.so\n')
    (root / 'etc/os-release').write_text('ID=debian\nVERSION_CODENAME=trixie\n')
    executable(commands / 'uname', 'echo aarch64\n')
    executable(commands / 'dpkg', 'echo arm64\n')
    executable(commands / 'ldconfig', 'echo ldconfig >> "$COMMAND_LOG"\n')
    executable(commands / 'sleep', 'echo "sleep $*" >> "$COMMAND_LOG"\n')
    executable(commands / 'pidof', '[[ -s "$STATE" ]] || exit 1; cat "$STATE"\n')
    executable(commands / 'df', '''[[ "$*" == '--output=avail -B1 / /usr /var' ]] || exit 2
printf 'Avail\\n%s\\n%s\\n%s\\n' 1000000000 "${VAR_AVAIL:-1000000000}" 1000000000
''')
    executable(commands / 'asterisk', '''echo "asterisk $*" >> "$COMMAND_LOG"
case "$2" in
  'core show channels count') echo "$CALLS active calls" ;;
  'core waitfullybooted') [[ -s "$STATE" ]] ;;
esac
''')
    executable(commands / 'systemctl', '''echo "systemctl $*" >> "$COMMAND_LOG"
case "$*" in
  'stop asterisk') [[ "$STOP" == hangs ]] || : > "$STATE"; [[ "$STOP" != fails ]] ;;
  'start asterisk')
    starts=$(( $(cat "$STARTS" 2>/dev/null || echo 0) + 1 )); echo "$starts" > "$STARTS"
    [[ "$starts" != "$FAILING_START" ]] || exit 1
    [[ -s "$STATE" ]] || echo 4242 > "$STATE" ;;
  'is-active --quiet asterisk') [[ -s "$STATE" ]] ;;
  'show -p MainPID --value asterisk') if [[ -s "$STATE" ]]; then read -r main _ < "$STATE"; echo "$main"; else echo 0; fi ;;
  'show -p FragmentPath --value asterisk') cat "$FRAGMENT" 2>/dev/null || echo /run/systemd/generator.late/asterisk.service ;;
  daemon-reload) if [[ -e "$NATIVE_UNIT" ]]; then echo "$NATIVE_UNIT" > "$FRAGMENT"; else rm -f "$FRAGMENT"; fi ;;
esac
''')
    remote = (ROOT / 'scripts/bootstrap.sh').read_text()
    for path in ('/etc/', '/usr/sbin/', '/usr/lib/asterisk/', '/usr/lib/systemd/', '/var/lib/phonelet/'):
        # Not the stage/... names of artifacts.sha256 nor the archive-relative ./etc/asterisk exclusion.
        remote = re.sub(r'(?<![.\w])' + re.escape(path), lambda _: str(root) + path, remote)
    remote = remote.replace('tar -C / ', f'tar -C {root} ')
    # The real script requires STAGE under /root; the rest of the upgrade branch runs unchanged.
    remote = remote.replace('upgrade_asterisk "$3"', f'STAGE={shlex.quote(str(stage))}; upgrade_asterisk "$3"')
    script_path = tmp_path / 'bootstrap.sh'
    script_path.write_text(remote)

    def run(**settings):
        env = os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'], 'COMMAND_LOG': str(log),
                            'STATE': str(state), 'STARTS': str(tmp_path / 'starts'), 'FRAGMENT': str(tmp_path / 'fragment'),
                            'NATIVE_UNIT': str(root / 'usr/lib/systemd/system/asterisk.service'),
                            'CALLS': '0', 'STOP': 'works', 'FAILING_START': '0'} | settings
        result = subprocess.run(['bash', str(script_path), 'upgrade', '/root/phonelet-install.TEST1234', str(archive)],
                                capture_output=True, text=True, env=env)
        calls = log.read_text().splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return result, calls

    return SimpleNamespace(run=run, root=root, stage=stage, archive=archive, state=state, old=old, files=files)


def test_asterisk_upgrade_replaces_the_build_and_keeps_the_deployed_config(asterisk_upgrade):
    u = asterisk_upgrade
    result, calls = u.run()
    assert result.returncode == 0, result.stderr
    systemctl = [call for call in calls if call.startswith('systemctl')]
    assert systemctl == ['systemctl show -p MainPID --value asterisk', 'systemctl stop asterisk',
                         'systemctl daemon-reload', 'systemctl show -p FragmentPath --value asterisk',
                         'systemctl enable asterisk', 'systemctl reset-failed asterisk', 'systemctl start asterisk',
                         'systemctl show -p MainPID --value asterisk']
    assert calls.index('asterisk -rx core show channels count') < calls.index('systemctl stop asterisk')
    assert calls.index('ldconfig') < calls.index('systemctl daemon-reload')
    assert (u.root / 'usr/sbin/asterisk').read_bytes() == b'new asterisk'
    for name in ('usr/lib/systemd/system/asterisk.service', 'usr/lib/systemd/system/asterisk.service.d/10-phonelet.conf',
                 'usr/share/phonelet/asterisk.files'):
        assert (u.root / name).read_bytes() == u.files[name], name
    assert (u.root / 'etc/asterisk/extensions.conf').read_bytes() == b'deployed dialplan\n'
    assert not (u.root / 'var/run').exists()
    assert (u.root / 'var/lib/phonelet/asterisk-bootstrap').read_text() == u.archive.name + '\n'
    assert u.archive.exists() and u.state.read_text() == '4242\n'
    # A second run finds the build running under the native unit and touches nothing.
    result, calls = u.run()
    assert result.returncode == 0 and 'already runs under asterisk.service' in result.stdout
    assert not any(call.startswith(('systemctl stop', 'systemctl start', 'asterisk -rx core show')) for call in calls)


@pytest.mark.parametrize('failure, message', [
    ('calls', 'Asterisk has active calls; nothing changed'),
    ('marker', 'this Asterisk was not installed by bootstrap'),
    ('stop-fails', 'systemctl stop asterisk failed; nothing changed'),
    ('stop-hangs', 'Asterisk did not stop within 60 s'),
    ('untracked', 'runs outside asterisk.service'),
    ('space', 'Not enough free space to unpack'),
])
def test_asterisk_upgrade_refusals_leave_the_previous_build_running(asterisk_upgrade, failure, message):
    u = asterisk_upgrade
    settings = {'calls': {'CALLS': '1'}, 'stop-fails': {'STOP': 'fails'}, 'stop-hangs': {'STOP': 'hangs'},
                'space': {'VAR_AVAIL': '10'}}
    if failure == 'marker':
        (u.root / 'var/lib/phonelet/asterisk-bootstrap').unlink()
    if failure == 'untracked':
        u.state.write_text('100 200\n')
    result, calls = u.run(**settings.get(failure, {}))
    assert result.returncode == 1 and message in result.stderr
    assert (u.root / 'usr/sbin/asterisk').read_bytes() == b'previous asterisk' and u.archive.exists()
    assert all((u.root / name).read_bytes() == content for name, content in u.old.items())
    assert 'systemctl stop asterisk' not in calls or calls[-1] == 'systemctl start asterisk'
    assert u.state.read_text().strip()  # Asterisk is not left stopped
    if failure == 'stop-hangs':
        assert calls.count('sleep 1') == 60


def test_asterisk_upgrade_that_fails_after_the_stop_starts_asterisk_again(asterisk_upgrade):
    u = asterisk_upgrade
    result, calls = u.run(FAILING_START='1')
    assert result.returncode == 1
    assert 'asterisk.service was started again' in result.stderr
    assert calls[-2:] == ['systemctl daemon-reload', 'systemctl start asterisk']
    assert u.state.read_text() == '4242\n'
    assert (u.root / 'usr/sbin/asterisk').read_bytes() == b'new asterisk'


def test_asterisk_upgrade_never_loads_a_target_profile(asterisk_upgrade, tmp_path):
    profile = tmp_path / 'target'
    profile.mkdir()
    (profile / 'profile.sh').write_text('echo "unexpected profile load" >&2; exit 99\n')
    result, _ = asterisk_upgrade.run(TARGET_DIR=str(profile), PHONELET_BOOTSTRAP_TARGET='synthetic')
    assert result.returncode == 0, result.stderr
    assert 'unexpected profile load' not in result.stderr


@pytest.mark.parametrize('phase', ['install', 'upgrade'])
@pytest.mark.parametrize('archive', [None, ''])
def test_bootstrap_requires_archive_before_host_commands(tmp_path, phase, archive):
    commands = tmp_path / 'bin'
    executable(commands / 'uname', 'echo "Unexpected host check" >&2; exit 99\n')
    command = ['bash', str(ROOT / 'scripts/bootstrap.sh'), phase, '/root/phonelet-install.TEST1234']
    if archive is not None:
        command.append(archive)
    result = subprocess.run(command, capture_output=True, text=True,
                            env=os.environ | {'TARGET_DIR': '', 'PATH': str(commands) + ':' + os.environ['PATH']})
    assert result.returncode == 1
    assert 'install STAGE ARCHIVE | upgrade STAGE ARCHIVE' in result.stderr
    assert result.stdout == ''
