"""Local installation with temporary host paths and synthetic commands only."""
import base64
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPTIONS = ['--country-prefix', '+1', '--trunk-prefix', '0', '--sip-domain', '192.0.2.10']


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/bash\nset -euo pipefail\n' + text)
    path.chmod(0o755)


@pytest.fixture
def installer(tmp_path):
    tree, host, commands = (tmp_path / name for name in ('tree', 'host', 'bin'))
    tree.mkdir()
    (host / 'root').mkdir(parents=True)
    log = tmp_path / 'commands.log'
    masks = tmp_path / 'umasks.log'

    (host / 'run').mkdir()

    def sandbox(text):
        return text.replace('/root/', str(host / 'root') + '/').replace(
            '/etc/systemd/system/', str(host / 'systemd') + '/').replace('/run/', str(host / 'run') + '/')

    for name in ('scripts', 'asterisk', 'targets'):
        shutil.copytree(ROOT / name, tree / name)
    (tree / 'src').symlink_to(ROOT / 'src')
    for name in ('pyproject.toml', 'requirements.lock', 'archive-revision'):
        shutil.copyfile(ROOT / name, tree / name)
    (tree / 'install.sh').write_text(sandbox((ROOT / 'install.sh').read_text()))
    helper = tree / 'scripts/release-common.sh'
    helper.write_text(sandbox(helper.read_text()))
    target = tree / 'targets/synthetic'
    target.mkdir(parents=True)
    (target / 'profile.sh').write_text('''echo "profile-loaded" >> "$COMMAND_LOG"
[[ -f "$TARGET_DIR/asset" ]] || exit 99
target_check_storage() {
  echo "profile $1 ${TARGET_DIR##*/}" >> "$COMMAND_LOG"
}
''')
    (target / 'asset').write_text('synthetic profile asset\n')
    # No host command can escape this fake bootstrap in either phase.
    (tree / 'scripts/bootstrap.sh').write_text('''#!/bin/bash
set -euo pipefail
if [[ "$1" != upgrade && -n "${TARGET_DIR:-}" ]]; then source "$TARGET_DIR/profile.sh"; fi
target_name=${TARGET_DIR:-}
target_name=${target_name##*/}
printf '%s %s %s %s\\n' "$1" "$target_name" "$PHONELET_BOOTSTRAP_WIFI" \
  "$PHONELET_BOOTSTRAP_MODEM_USB_ID" >> "$COMMAND_LOG"
if [[ "$1" == prepare ]]; then
  if [[ -n "${TARGET_DIR:-}" ]]; then
    if declare -F target_check_storage >/dev/null; then target_check_storage prepare; fi
  fi
  echo "prepare-args ${*:2}" >> "$COMMAND_LOG"
  if [[ "$PREPARE_STATUS" == 1 ]]; then echo 'Another Asterisk installation found' >&2; fi
  exit "$PREPARE_STATUS"
fi
if [[ "$1" == install && -n "${TARGET_DIR:-}" ]]; then
  if declare -F target_check_storage >/dev/null; then target_check_storage install; fi
fi
cp -r "$2" "$SNAPSHOT"
cp "$3" "$SNAPSHOT/archive"
exit "$INSTALL_STATUS"
''')
    build = tmp_path / 'build'
    build.mkdir()
    archive = build / 'asterisk-20.22.0-arm64.tar.gz'
    archive.write_bytes(b'synthetic archive')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sums = (f'{digest}  {archive.name}\n' + '0' * 64 + '  stage/usr/sbin/asterisk\n'
            + '1' * 64 + '  stage/usr/lib/asterisk/modules/chan_dongle.so\n')
    (build / 'artifacts.sha256').write_text(sums)
    (tree / 'asterisk/artifacts.lock').write_text(''.join(
        f'{arch} {digest} {"0" * 64} {"1" * 64} https://example.invalid/asterisk-20.22.0-{arch}.tar.gz\n'
        for arch in ('arm64', 'amd64')))
    executable(commands / 'id', 'echo "${FAKE_UID:-0}"\n')
    executable(commands / 'stat', '''if [[ "$1 $2" == '-c %u' ]]; then
  if [[ "${*: -1}" == "${FOREIGN_DIRECTORY:-}" ]]; then echo 1000; else echo 0; fi
else
  exec /usr/bin/stat "$@"
fi
''')
    executable(commands / 'dpkg', 'echo architecture >> "$COMMAND_LOG"; echo "${HOST_ARCH-arm64}"\n')
    executable(commands / 'dpkg-query', '''echo python-packages >> "$COMMAND_LOG"
[[ "$PYTHON_INSTALLED" == 1 ]] || exit 1
printf 'install ok installed\\ninstall ok installed\\n'
''')
    executable(commands / 'apt-get', '''echo "apt-get $*" >> "$COMMAND_LOG"
echo "apt-get $(umask)" >> "$UMASK_LOG"
''')
    executable(commands / 'hostname', 'echo 192.0.2.10 2001:db8::1\n')
    executable(commands / 'curl', '''echo download >> "$COMMAND_LOG"
while (( $# )); do
  if [[ "$1" == --output ]]; then cp "$ARCHIVE_SOURCE" "$2"; shift; fi
  shift
done
''')
    executable(commands / 'sha256sum', 'echo verify >> "$COMMAND_LOG"\nexec /usr/bin/sha256sum "$@"\n')
    for name in ('ssh', 'systemctl', 'asterisk'):
        executable(commands / name, 'echo "unexpected command" >&2; exit 99\n')
    # Only venv creation and pip/engine execution are fake. The staged interpreter delegates
    # configuration generation and bootstrap rendering to the test venv.
    # The install fixture fakes host preflight just as it fakes deployment. CLI integration with
    # the static preflight is covered by test_operations; validate the actual env file here.
    preflight = tmp_path / 'preflight.py'
    preflight.write_text('''import runpy
import sys
module = runpy.run_path(sys.argv[1])
def deploy(stage, root, etc, systemd, helper, *, pre_upgrade, allow_restart, env_file):
    assert pre_upgrade
    assert allow_restart == ('--allow-transport-change' in sys.argv)
    module['configuration'](env_file)
module['main'].__globals__['deploy'] = deploy
module['main'](sys.argv[2:])
''')
    interpreter = commands / 'staged-python'
    executable(interpreter, f'COMMAND_LOG={shlex.quote(str(log))}\nTEST_PYTHON={shlex.quote(sys.executable)}\nPREFLIGHT={shlex.quote(str(preflight))}\n' + '''if [[ "$1" == -m && "$2" == pip ]]; then
  echo pip >> "$COMMAND_LOG"
  exit "$PIP_STATUS"
fi
if [[ "$2" == --check-env ]]; then
  if [[ "${*: -1}" == --allow-transport-change ]]; then
    echo check-env-restart >> "$COMMAND_LOG"
  else
    echo check-env >> "$COMMAND_LOG"
  fi
  exec "$TEST_PYTHON" "$PREFLIGHT" "$@"
fi
if [[ "$2" == --render-configs ]]; then
  echo render >> "$COMMAND_LOG"
  exec "$TEST_PYTHON" "$@"
fi
if [[ "$2" == --init-env ]]; then
  echo init-env >> "$COMMAND_LOG"
  exec "$TEST_PYTHON" "$@"
fi
echo "engine $*" >> "$COMMAND_LOG"
if [[ -n "${LOCK_PROBE:-}" ]]; then  # can anyone else take the deploy lock now?
  if flock -n "$LOCK_PROBE" true; then echo 'lock free' >> "$COMMAND_LOG"; else echo 'lock busy' >> "$COMMAND_LOG"; fi
  echo "lock fd ${PHONELET_DEPLOY_LOCK_FD:-none}" >> "$COMMAND_LOG"
fi
if [[ "${*: -1}" == --check ]]; then exit "$CHECK_STATUS"; fi
exit "$DEPLOY_STATUS"
''')
    executable(commands / 'python3', '''[[ "$*" == '-m venv .venv' ]] || exit 99
echo venv >> "$COMMAND_LOG"
echo "venv $(umask)" >> "$UMASK_LOG"
mkdir -p .venv/bin
cp "$STAGED_PYTHON" .venv/bin/python
''')
    env_file = host / 'root/phonelet/.env'
    dropin = host / 'systemd/phonelet.service.d/20-phonelet-release.conf'

    def existing_env():
        env_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        module = deploy_module()
        module.init_env(env_file, '+1', '0', '192.0.2.10')
        with env_file.open('a') as stream:
            stream.write('# Keep this comment byte for byte.\n')
        return env_file.read_bytes()

    def update():
        existing_env()
        dropin.parent.mkdir(parents=True)
        dropin.touch()

    def run(arguments=None, extra_env=None, cwd=tmp_path):
        return subprocess.run(['bash', str(tree / 'install.sh'), *(arguments or [])], cwd=cwd,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, env=os.environ | {
                                  'PATH': str(commands) + ':' + os.environ['PATH'],
                                  'COMMAND_LOG': str(log), 'UMASK_LOG': str(masks),
                                  'PREPARE_STATUS': '0', 'INSTALL_STATUS': '0',
                                  'CHECK_STATUS': '0', 'DEPLOY_STATUS': '0',
                                  'PIP_STATUS': '0', 'PYTHON_INSTALLED': '0',
                                  'STAGED_PYTHON': str(interpreter), 'TEST_PYTHON': sys.executable,
                                  'ARCHIVE_SOURCE': str(archive), 'SNAPSHOT': str(tmp_path / 'snapshot'),
                                  'PHONELET_BOOTSTRAP_TARGET': '', 'PHONELET_BOOTSTRAP_WIFI': '0',
                                  'PHONELET_BOOTSTRAP_MODEM_USB_ID': '12d1:1001',
                              } | (extra_env or {}))

    return SimpleNamespace(run=run, root=tmp_path, tree=tree, host=host, commands=commands, build=build, archive=archive,
                           env_file=env_file, dropin=dropin, log=log, existing_env=existing_env, update=update,
                           snapshot=tmp_path / 'snapshot', masks=masks)


def deploy_module():
    spec = importlib.util.spec_from_file_location('deploy_release_install_test', ROOT / 'scripts/deploy-release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calls(installer):
    return installer.log.read_text().splitlines() if installer.log.exists() else []


def assert_clean(installer):
    assert not list((installer.host / 'root').glob('phonelet-install.*'))


def releases(installer):
    return list((installer.host / 'root/phonelet-releases').glob('release.*'))


def test_install_not_root_touches_nothing(installer):
    result = installer.run(extra_env={'FAKE_UID': '1000'})
    assert result.returncode == 1 and 'root' in result.stderr
    assert calls(installer) == []
    assert list((installer.host / 'root').iterdir()) == []


@pytest.mark.parametrize('arguments', [
    ['--bogus'], ['--asterisk-archive'], ['--country-prefix'], ['--check'],
    ['--allow-asterisk-restart'], ['--restart-asterisk'],
    ['--asterisk-build', 'a', '--asterisk-archive', 'b'],
    ['--check', '--restart-asterisk'], ['--sip-domain', 'a', '--sip-domain', 'b'],
])
def test_install_bad_arguments_before_commands(installer, arguments):
    result = installer.run(arguments)
    assert result.returncode == 2
    assert calls(installer) == []
    assert list((installer.host / 'root').iterdir()) == []


@pytest.mark.parametrize('source', ['download', 'local', 'build'])
def test_install_first_install_order_and_private_configuration(installer, source):
    i = installer
    arguments = {'download': [], 'local': ['--asterisk-archive', str(i.archive)],
                 'build': ['--asterisk-build', str(i.build)]}[source]
    result = i.run(arguments + OPTIONS)
    assert result.returncode == 0, result.stderr
    log = calls(i)
    bootstrap_install = 'install  0 12d1:1001'
    markers = ['verify', 'prepare ', 'python-packages', 'apt-get update',
               'apt-get install', 'venv', 'pip', 'init-env', 'render', bootstrap_install, 'engine ', 'engine ']
    positions, start = [], 0
    for marker in markers:
        if marker == bootstrap_install:
            pos = log.index(bootstrap_install, start)
        else:
            pos = next(j for j in range(start, len(log)) if log[j].startswith(marker))
        positions.append(pos)
        start = pos + 1
    assert positions == sorted(positions)
    engines = [line for line in log if line.startswith('engine ')]
    assert len(engines) == 2 and engines[0].endswith(' --check')
    assert '--check' not in engines[1]
    assert engines[0].removesuffix(' --check') == engines[1]
    assert ('download' in log) == (source == 'download')
    assert i.env_file.stat().st_mode & 0o777 == 0o600
    assert i.env_file.parent.stat().st_mode & 0o777 == 0o700
    assert i.masks.read_text().splitlines() == ['apt-get 0022', 'apt-get 0022', 'venv 0077']
    assert len(releases(i)) == 1 and releases(i)[0].stat().st_mode & 0o777 == 0o700
    assert f'prepare-args {i.archive.name}' in log
    assert i.snapshot.stat().st_mode & 0o777 == 0o700
    assert not (i.snapshot / 'env').exists()
    assert (i.snapshot / 'archive').read_bytes() == i.archive.read_bytes()
    for path in i.host.rglob('*'):
        if path.name.startswith('phonelet-install.'):
            pytest.fail('private work directory leaked')
    assert 'Summary:' in result.stdout and 'test SMS and a call' in result.stdout
    assert_clean(i)


@pytest.mark.parametrize('status', ['1', '3'])
def test_install_prepare_refusal_or_reboot_stops_before_changes(installer, status):
    result = installer.run(OPTIONS, {'PREPARE_STATUS': status})
    assert result.returncode == int(status)
    log = calls(installer)
    assert not any(item.startswith(('python-packages', 'apt-get', 'venv', 'init-env', 'install ')) for item in log)
    assert list((installer.host / 'root').iterdir()) == []
    assert_clean(installer)


@pytest.mark.parametrize('source', ['download', 'local', 'build'])
def test_install_checksum_mismatch_stops_before_prepare(installer, source):
    i = installer
    i.archive.write_bytes(b'corrupt synthetic archive')
    arguments = {'download': [], 'local': ['--asterisk-archive', str(i.archive)],
                 'build': ['--asterisk-build', str(i.build)]}[source]
    result = i.run(arguments + OPTIONS)
    assert result.returncode == 1 and 'Checksum mismatch' in result.stderr
    assert not any(line.startswith('prepare') for line in calls(i))
    assert not i.env_file.exists() and releases(i) == []
    assert_clean(i)


def test_install_reuses_environment_and_installed_python_and_asterisk(installer):
    i = installer
    before = i.existing_env()
    result = i.run(['--asterisk-build', str(i.build)], {'PYTHON_INSTALLED': '1'})
    assert result.returncode == 0, result.stderr
    assert i.env_file.read_bytes() == before
    assert not (i.snapshot / 'env').exists()
    assert (i.snapshot / 'archive').read_bytes() == i.archive.read_bytes()
    assert not any(line.startswith(('apt-get', 'init-env')) for line in calls(i))
    assert_clean(i)


def test_install_answers_with_existing_environment_are_usage_error(installer):
    before = installer.existing_env()
    result = installer.run(OPTIONS)
    assert result.returncode == 2 and calls(installer) == []
    assert installer.env_file.read_bytes() == before


@pytest.mark.parametrize('setting', [
    {'PHONELET_BOOTSTRAP_TARGET': '../bad'}, {'PHONELET_BOOTSTRAP_TARGET': 'missing'},
    {'PHONELET_BOOTSTRAP_WIFI': 'yes'}, {'PHONELET_BOOTSTRAP_MODEM_USB_ID': '1234:1001'},
    {'PHONELET_BOOTSTRAP_WIFI': '1; echo unsafe'}, {'PHONELET_BOOTSTRAP_MODEM_USB_ID': "12d1:1001'"},
])
def test_install_settings_are_validated_before_commands(installer, setting):
    result = installer.run(OPTIONS, setting)
    assert result.returncode == 2 and calls(installer) == []


def test_install_profile_and_settings_reach_both_phases(installer):
    settings = {'PHONELET_BOOTSTRAP_TARGET': 'synthetic', 'PHONELET_BOOTSTRAP_WIFI': '1',
                'PHONELET_BOOTSTRAP_MODEM_USB_ID': '12d1:1003'}
    result = installer.run(OPTIONS, settings)
    assert result.returncode == 0, result.stderr
    log = calls(installer)
    assert 'prepare synthetic 1 12d1:1003' in log
    assert 'install synthetic 1 12d1:1003' in log
    assert 'profile prepare synthetic' in log and 'profile install synthetic' in log
    assert log.count('profile-loaded') == 2
    assert not (installer.snapshot / 'target').exists()
    assert (installer.tree / 'targets/synthetic/asset').read_text() == 'synthetic profile asset\n'


@pytest.mark.parametrize('failure', ['INSTALL_STATUS', 'CHECK_STATUS', 'DEPLOY_STATUS', 'PIP_STATUS'])
def test_install_failure_cleans_private_stages_and_can_be_rerun(installer, failure):
    i = installer
    result = i.run(OPTIONS, {failure: '73'})
    assert result.returncode == 1
    assert_clean(i)
    if failure == 'INSTALL_STATUS':
        assert not any(line.startswith('engine ') for line in calls(i))
    if failure == 'CHECK_STATUS':
        assert len([line for line in calls(i) if line.startswith('engine ')]) == 1
    before = i.env_file.read_bytes() if i.env_file.exists() else None
    shutil.rmtree(i.snapshot, ignore_errors=True)
    result = i.run([] if before else OPTIONS)
    assert result.returncode == 0, result.stderr
    if before:
        assert i.env_file.read_bytes() == before
    assert_clean(i)


@pytest.mark.parametrize('mode', ['', '--check', '--allow-asterisk-restart', '--restart-asterisk'])
@pytest.mark.parametrize('status', ['0', '73'])
def test_install_update_stages_only_and_passes_flags(installer, mode, status):
    i = installer
    i.update()
    before = i.env_file.read_bytes()
    result = i.run([mode] if mode else [], {'CHECK_STATUS' if mode == '--check' else 'DEPLOY_STATUS': status})
    assert result.returncode == (0 if status == '0' else 1), result.stderr
    log = calls(i)
    assert log[:2] == ['venv', 'pip'] and len(log) == 3
    assert log[2].startswith('engine scripts/deploy-release.py --stage ')
    assert log[2].endswith(mode) if mode else '--check' not in log[2]
    assert i.env_file.read_bytes() == before
    assert len(releases(i)) == (0 if mode == '--check' else 1)
    assert_clean(i)


@pytest.mark.parametrize('arguments', [OPTIONS, ['--check', '--asterisk-build', 'unused'],
                                      ['--asterisk-archive', 'unused', '--check'], ['--check', '--restart-asterisk']])
def test_install_update_rejects_options_before_commands(installer, arguments):
    installer.update()
    result = installer.run(arguments)
    assert result.returncode == 2 and calls(installer) == []


@pytest.mark.parametrize('option', ['--asterisk-build', '--asterisk-archive'])
@pytest.mark.parametrize('mode', ['', '--allow-asterisk-restart', '--restart-asterisk'])
def test_install_update_upgrades_asterisk_after_staging_and_before_deploy(installer, option, mode):
    """docs/operations.md, "Upgrading Asterisk": the checked archive goes to bootstrap.sh upgrade."""
    i = installer
    i.update()
    source = str(i.build) if option == '--asterisk-build' else str(i.archive)
    result = i.run([option, source] + ([mode] if mode else []))
    assert result.returncode == 0, result.stderr
    log = [line for line in calls(i) if line not in ('architecture', 'verify')]  # the archive's checks
    preflight = 'check-env-restart' if mode else 'check-env'
    assert log[:4] == ['venv', 'pip', preflight, 'upgrade  0 12d1:1001']  # static preflight before replacement
    log.pop(2)
    assert len(log) == 4 and log[3].startswith('engine scripts/deploy-release.py --stage ')
    assert log[3].endswith(mode) if mode else '--restart-asterisk' not in log[3]
    expected = ['archive', 'artifacts.sha256', 'asterisk']
    assert sorted(path.name for path in i.snapshot.iterdir()) == sorted(expected)
    assert (i.snapshot / 'archive').read_bytes() == i.archive.read_bytes()
    assert i.archive.exists()
    assert_clean(i)


def test_install_update_upgrade_refuses_a_busy_deploy_lock_without_changes(installer):
    i = installer
    i.update()
    with open(i.host / 'root/phonelet/.deploy.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another deployment
        result = i.run(['--asterisk-build', str(i.build)])
    assert result.returncode == 1 and 'another deployment holds' in result.stderr
    assert [line for line in calls(i) if line != 'verify'] == []  # no release, no upgrade, no deploy
    assert releases(i) == [] and not i.snapshot.exists()
    assert_clean(i)


def test_install_update_holds_the_deploy_lock_from_upgrade_through_deploy(installer):
    i = installer
    i.update()
    lock = i.host / 'root/phonelet/.deploy.lock'
    result = i.run(['--asterisk-build', str(i.build)], {'LOCK_PROBE': str(lock)})
    assert result.returncode == 0, result.stderr
    log = calls(i)
    assert log[log.index('upgrade  0 12d1:1001') + 1].startswith('engine ')
    assert log[-2] == 'lock busy' and re.fullmatch(r'lock fd [0-9]+', log[-1])  # still ours at deploy time
    # A plain update locks inside deploy-release.py as before: nothing is held or passed down.
    result = i.run([], {'LOCK_PROBE': str(lock)})
    assert result.returncode == 0, result.stderr
    assert calls(i)[-2:] == ['lock free', 'lock fd none']


def test_install_update_refused_configuration_stops_before_the_asterisk_upgrade(installer):
    i = installer
    i.update()
    with i.env_file.open('a') as stream:
        stream.write('PHONELET_RECORDINGS_DIR=/root/recordings\n')  # read-only for the native unit
    result = i.run(['--asterisk-build', str(i.build)])
    assert result.returncode == 1 and 'PHONELET_RECORDINGS_DIR must be outside' in result.stderr
    assert 'Asterisk not changed' in result.stderr
    assert not any(line.startswith(('upgrade', 'engine')) for line in calls(i))
    assert not i.snapshot.exists()
    assert_clean(i)


def test_install_update_failed_asterisk_upgrade_stops_before_deploy(installer):
    i = installer
    i.update()
    result = i.run(['--asterisk-build', str(i.build)], {'INSTALL_STATUS': '1'})
    assert result.returncode == 1
    assert not any(line.startswith('engine ') for line in calls(i))
    assert_clean(i)


def test_install_update_upgrade_refuses_an_archive_that_differs_from_the_lock(installer):
    i = installer
    i.update()
    damaged = i.root / 'other' / i.archive.name
    damaged.parent.mkdir()
    damaged.write_bytes(b'another archive')
    result = i.run(['--asterisk-archive', str(damaged)])
    assert result.returncode == 1 and 'Checksum mismatch' in result.stderr
    assert calls(i) == ['architecture', 'verify']  # nothing staged, Asterisk untouched
    assert_clean(i)


def test_install_update_missing_environment_refused_without_staging(installer):
    installer.update()
    installer.env_file.unlink()
    result = installer.run()
    assert result.returncode == 1 and '.env' in result.stderr
    assert calls(installer) == [] and releases(installer) == []


@pytest.mark.parametrize('value,expected', [('a' * 40, 'a' * 40), ('$Format:%H$', 'unknown'),
                                           ('unsafe value', 'unknown'), ('', 'unknown')])
@pytest.mark.parametrize('newline', ['', '\n'])
def test_install_archive_revision(installer, value, expected, newline):
    i = installer
    i.update()
    (i.tree / 'archive-revision').write_text(value + newline)
    result = i.run()
    assert result.returncode == 0, result.stderr
    assert (releases(i)[0] / 'REVISION').read_text() == expected + '\n'


@pytest.mark.parametrize('dirty', [False, True])
def test_install_checkout_revision(installer, dirty):
    i = installer
    i.update()

    def git(*args):
        return subprocess.run(['git', '-C', str(i.tree), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null',
        'commit', '-qm', 'Synthetic tree')
    revision = git('rev-parse', '--short=12', 'HEAD')
    if dirty:
        (i.tree / 'untracked').touch()
        revision += '-dirty'
    result = i.run()
    assert result.returncode == 0, result.stderr
    assert (releases(i)[0] / 'REVISION').read_text() == revision + '\n'


@pytest.mark.parametrize('answer,expected', [('', '192.0.2.10'), ('192.0.2.20', '192.0.2.20')])
def test_init_env_tty_accepts_or_replaces_proposed_sip_address(tmp_path, monkeypatch, answer, expected):
    module = deploy_module()
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    prompts = []
    answers = iter(['+1', '', answer])

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr('builtins.input', answer)
    path = tmp_path / 'generated.env'
    module.init_env(path, sip_domain_default='192.0.2.10')
    assert prompts == ['Country calling code, with + (e.g. +44): ',
                       'Domestic trunk prefix digit, dropped when dialling internationally '
                       '(e.g. 0; empty if your country has no trunk prefix): ',
                       'Address or DNS name the SIP phone uses to reach this host '
                       '(the VPN address for a phone over VPN or Tailscale) [192.0.2.10]: ']
    env, _ = module.configuration(path)
    assert env['PHONELET_SIP_DOMAIN'] == expected
    assert env['AST_TRUNK_PREFIX'] == ''  # an empty answer: no trunk prefix


def test_install_missing_curl_refuses_before_creating_work(installer):
    i = installer
    (i.commands / 'curl').unlink()
    for name in ('bash', 'dirname', 'flock'):
        (i.commands / name).symlink_to(shutil.which(name))
    result = i.run(OPTIONS, {'PATH': str(i.commands)})
    assert result.returncode == 1 and 'apt-get install curl' in result.stderr
    assert calls(i) == [] and list((i.host / 'root').iterdir()) == []


@pytest.mark.parametrize('damage', ['no-archive', 'no-archive-sum', 'no-sums', 'no-asterisk-sum', 'no-dongle-sum',
                                   'bad-dongle-sum', 'two-archives'])
def test_install_build_validation_stops_before_prepare(installer, damage):
    i = installer
    sums = i.build / 'artifacts.sha256'
    lost = {'no-archive-sum': i.archive.name, 'no-asterisk-sum': 'stage/usr/sbin/asterisk',
            'no-dongle-sum': 'stage/usr/lib/asterisk/modules/chan_dongle.so'}.get(damage)
    if damage == 'no-archive':
        i.archive.unlink()
    elif damage == 'no-sums':
        sums.unlink()
    elif lost:
        sums.write_text(''.join(line for line in sums.read_text().splitlines(keepends=True)
                                if not line.rstrip().endswith(lost)))
    elif damage == 'bad-dongle-sum':
        sums.write_text(sums.read_text().replace('1' * 64, 'bad'))
    else:
        # Sorts after the valid archive, so only the count check refuses it.
        (i.build / 'asterisk-20.22.1-arm64.tar.gz').write_bytes(b'synthetic second archive')
    result = i.run(['--asterisk-build', str(i.build)] + OPTIONS)
    assert result.returncode == 1
    if damage in ('no-asterisk-sum', 'no-dongle-sum'):
        assert f'Missing or malformed {lost} checksum' in result.stderr
    if damage in ('no-archive', 'two-archives'):
        assert 'Expected one asterisk-VERSION-ARCH.tar.gz' in result.stderr
    assert not any(line.startswith('prepare') for line in calls(i))
    assert_clean(i)


def test_install_generation_error_removes_file_and_stages(installer):
    i = installer
    result = i.run(['--country-prefix', 'bad', '--trunk-prefix', '0', '--sip-domain', '192.0.2.10'])
    assert result.returncode == 1 and 'AST_COUNTRY_PREFIX' in result.stderr
    assert not i.env_file.exists()
    assert not any(line.startswith(('render', 'install ', 'engine ')) for line in calls(i))
    assert_clean(i)


# Bit 0 = --country-prefix, 1 = --trunk-prefix (optional), 2 = --sip-domain.
@pytest.mark.parametrize('provided', [0, 1, 2, 3, 4, 6])
def test_install_unattended_generation_requires_all_answers_before_commands(installer, provided):
    arguments = [value for index in range(3) if provided & (1 << index)
                 for value in OPTIONS[2 * index:2 * index + 2]]
    executable(installer.commands / 'dirname', 'echo dirname >> "$COMMAND_LOG"; exit 99\n')
    result = installer.run(arguments)
    assert result.returncode == 2 and 'required when stdin is not a TTY' in result.stderr
    assert calls(installer) == [] and list((installer.host / 'root').iterdir()) == []
    assert not installer.env_file.exists()
    assert_clean(installer)


def test_install_unattended_generation_without_trunk_prefix(installer):
    i = installer
    result = i.run(OPTIONS[:2] + OPTIONS[4:])
    assert result.returncode == 0, result.stderr
    assert 'AST_TRUNK_PREFIX=""' in i.env_file.read_text().splitlines()
    dialplan = (i.snapshot / 'etc-asterisk/extensions_custom.conf').read_text()
    assert '_00XXX.' in dialplan and 'XXXX.' not in dialplan  # no local rule
    assert_clean(i)


@pytest.mark.parametrize('arguments', [OPTIONS[:4] + OPTIONS[:2], OPTIONS[2:4] + OPTIONS[2:4] + OPTIONS[4:]])
def test_install_repeated_answer_is_usage_error(installer, arguments):
    result = installer.run(arguments)
    assert result.returncode == 2 and calls(installer) == []
    assert not installer.env_file.exists()


def test_init_env_sip_default_does_not_replace_non_tty_option(tmp_path, monkeypatch):
    module = deploy_module()
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    path = tmp_path / 'unattended.env'
    with pytest.raises(ValueError, match='--sip-domain is required'):
        module.init_env(path, '+1', '0', sip_domain_default='192.0.2.10')
    assert not path.exists()


def test_install_without_git_or_archive_revision_is_unknown(installer):
    i = installer
    i.update()
    (i.tree / 'archive-revision').unlink()
    result = i.run()
    assert result.returncode == 0, result.stderr
    assert (releases(i)[0] / 'REVISION').read_text() == 'unknown\n'


def test_install_git_archive_substitutes_revision(tmp_path):
    tree = tmp_path / 'tree'
    tree.mkdir()
    for name in ('.gitattributes', 'archive-revision'):
        shutil.copyfile(ROOT / name, tree / name)

    def git(*args):
        return subprocess.run(['git', '-C', str(tree), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null',
        'commit', '-qm', 'Synthetic archive revision')
    archive = tmp_path / 'source.tar'
    git('archive', '--output=' + str(archive), 'HEAD')
    with tarfile.open(archive) as source:
        assert source.extractfile('archive-revision').read().decode() == git('rev-parse', 'HEAD') + '\n'


@pytest.mark.parametrize('arguments', [OPTIONS[:2], OPTIONS[:4], OPTIONS])
def test_install_update_answers_with_missing_environment_are_usage_error(installer, arguments):
    i = installer
    i.update()
    i.env_file.unlink()
    executable(i.commands / 'dirname', 'echo dirname >> "$COMMAND_LOG"; exit 99\n')
    result = i.run(arguments)
    assert result.returncode == 2 and calls(i) == [] and releases(i) == []


def test_install_update_ignores_all_bootstrap_settings(installer):
    i = installer
    i.update()
    result = i.run(extra_env={
        'PHONELET_BOOTSTRAP_TARGET': '../invalid-target',
        'PHONELET_BOOTSTRAP_WIFI': 'invalid',
        'PHONELET_BOOTSTRAP_MODEM_USB_ID': 'invalid',
    })
    assert result.returncode == 0, result.stderr
    log = calls(i)
    assert log[:2] == ['venv', 'pip'] and len(log) == 3
    assert log[2].startswith('engine scripts/deploy-release.py --stage ')


@pytest.mark.parametrize('archive_hash', ['a' * 40, None])
def test_install_nested_tree_does_not_use_outer_repository_revision(installer, archive_hash):
    i = installer
    i.update()
    if archive_hash:
        (i.tree / 'archive-revision').write_text(archive_hash + '\n')
    else:
        (i.tree / 'archive-revision').unlink()
    outer = i.tree.parent
    (outer / 'outer-marker').write_text('Synthetic outer repository\n')

    def git(*args):
        return subprocess.run(['git', '-C', str(outer), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    git('init', '-q')
    git('add', 'outer-marker')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'core.hooksPath=/dev/null',
        'commit', '-qm', 'Synthetic outer repository')
    outer_hash = git('rev-parse', '--short=12', 'HEAD')
    result = i.run()
    assert result.returncode == 0, result.stderr
    revision = (releases(i)[0] / 'REVISION').read_text().strip()
    assert revision == (archive_hash or 'unknown')
    assert not revision.startswith(outer_hash)


@pytest.mark.parametrize('archive_hash', ['a' * 40, None])
def test_install_dubious_git_ownership_falls_back_to_archive_revision(installer, archive_hash):
    i = installer
    i.update()
    if archive_hash:
        (i.tree / 'archive-revision').write_text(archive_hash + '\n')
    else:
        (i.tree / 'archive-revision').unlink()
    executable(i.commands / 'git', '''[[ "$*" == 'rev-parse --show-toplevel' ]] || exit 99
 echo 'fatal: detected dubious ownership in repository' >&2
 exit 128
''')
    result = i.run()
    assert result.returncode == 0, result.stderr
    assert (releases(i)[0] / 'REVISION').read_text().strip() == (archive_hash or 'unknown')


def test_install_removes_stale_work_directories_but_preserves_symlinks_and_files(installer):
    prefix = 'phonelet-install'
    i = installer
    root = i.host / 'root'
    stale = root / (prefix + '.STALE123')
    stale.mkdir(mode=0o700)
    (stale / 'env').write_text('# synthetic interrupted configuration\n')
    target = root / 'preserved'
    target.mkdir()
    (target / 'marker').write_text('Synthetic retained data\n')
    (stale / 'link').symlink_to(target, target_is_directory=True)
    link = root / (prefix + '.LINK1234')
    link.symlink_to(target, target_is_directory=True)
    file = root / (prefix + '.FILE1234')
    file.write_text('Synthetic retained file\n')
    foreign = root / (prefix + '.FOREIGN1')
    foreign.mkdir()
    other = [root / (prefix + suffix) for suffix in ('.short', '.TOOLONG12', '.BAD-NAME', '.BAD_NAME')]
    for directory in other:
        directory.mkdir()
    result = i.run(OPTIONS, {'PREPARE_STATUS': '3', 'FOREIGN_DIRECTORY': str(foreign)})
    assert result.returncode == 3, result.stderr
    assert not stale.exists()
    assert link.is_symlink() and (target / 'marker').read_text() == 'Synthetic retained data\n'
    assert file.read_text() == 'Synthetic retained file\n'
    assert foreign.is_dir() and all(directory.is_dir() for directory in other)
    assert not any(line.startswith(('apt-get', 'venv')) for line in calls(i))


def test_install_second_run_refuses_while_another_holds_the_installer_lock(installer):
    i = installer
    stale = i.host / 'root/phonelet-install.OTHERRUN'
    stale.mkdir(mode=0o700)
    (stale / 'env').write_text('# synthetic work of the running install\n')
    with open(i.host / 'run/phonelet-installer.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the first install.sh
        result = i.run(OPTIONS)
    assert result.returncode == 1
    assert 'another install.sh is running' in result.stderr
    assert calls(i) == []
    assert (stale / 'env').read_text() == '# synthetic work of the running install\n'
    assert sorted(p.name for p in (i.host / 'root').iterdir()) == ['phonelet-install.OTHERRUN']
    assert i.run(OPTIONS, {'PREPARE_STATUS': '3'}).returncode == 3  # free again: the sweep runs
    assert not stale.exists()


def test_install_update_sweeps_interrupted_install_work(installer):
    i = installer
    i.update()
    stale = i.host / 'root/phonelet-install.STALE123'
    stale.mkdir(mode=0o700)
    result = i.run()
    assert result.returncode == 0, result.stderr
    assert not stale.exists()


@pytest.mark.parametrize('scenario', ['check', 'update-answers', 'first-mode', 'missing-answers', 'target'])
def test_install_check_and_usage_errors_preserve_interrupted_work(installer, scenario):
    i = installer
    settings = {}
    if scenario in ('check', 'update-answers'):
        i.update()
        arguments = ['--check'] if scenario == 'check' else OPTIONS
    elif scenario == 'first-mode':
        arguments = ['--allow-asterisk-restart']
    elif scenario == 'missing-answers':
        arguments = []
    else:
        arguments = OPTIONS
        settings = {'PHONELET_BOOTSTRAP_TARGET': 'unknown-target'}
    directory = i.host / 'root/phonelet-install.STALE123'
    directory.mkdir()
    (directory / 'marker').write_text('synthetic retained work\n')
    result = i.run(arguments, settings)
    assert result.returncode == (0 if scenario == 'check' else 2), result.stderr
    assert (directory / 'marker').read_text() == 'synthetic retained work\n'


@pytest.mark.parametrize('arch', ['arm64', 'amd64'])
def test_install_without_build_fetches_the_host_architecture(installer, arch):
    i = installer
    result = i.run(OPTIONS, {'HOST_ARCH': arch})
    assert result.returncode == 0, result.stderr
    name = f'asterisk-20.22.0-{arch}.tar.gz'
    log = calls(i)
    assert log.index('architecture') < log.index('download') < log.index(f'prepare-args {name}')
    assert (i.snapshot / 'archive').read_bytes() == i.archive.read_bytes()
    assert_clean(i)


@pytest.mark.parametrize('arch', ['armhf', 'riscv64', ''])
def test_install_without_build_refuses_unexpected_architecture(installer, arch):
    i = installer
    result = i.run(OPTIONS, {'HOST_ARCH': arch})
    assert result.returncode == 1 and f'Unsupported host architecture: {arch}' in result.stderr
    assert calls(i) == ['architecture']
    assert_clean(i)


@pytest.mark.parametrize('arch', ['armhf', 'i386', 'riscv64'])
def test_install_refuses_unsupported_archive_architecture_before_prepare(installer, arch):
    i = installer
    i.archive.rename(i.build / f'asterisk-20.22.0-{arch}.tar.gz')
    result = i.run(['--asterisk-build', str(i.build)] + OPTIONS)
    assert result.returncode == 1 and 'Unsupported Asterisk archive' in result.stderr
    assert calls(i) == []
    assert_clean(i)


@pytest.mark.parametrize('source', ['download', 'build'])
def test_install_invalid_environment_stops_before_install_without_printing_values(installer, source):
    i = installer
    i.existing_env()
    with i.env_file.open('a') as stream:
        stream.write('AST_AMI_SECRET=other-secret\n')
    values = [line.split('=', 1)[1].strip('"') for line in i.env_file.read_text().splitlines()
              if line.startswith(('PHONELET_AMI_SECRET=', 'AST_SIP_PASSWORD=', 'AST_AMI_SECRET='))]
    result = i.run(['--asterisk-build', str(i.build)] if source == 'build' else [])
    assert result.returncode == 1
    assert 'AST_AMI_SECRET is derived from PHONELET_AMI_SECRET' in result.stderr
    assert len(values) == 3 and not any(value in result.stdout + result.stderr for value in values)
    assert not any(line.startswith(('install ', 'engine ')) for line in calls(i))
    assert not i.snapshot.exists()
    assert_clean(i)


def test_install_stages_rendered_configuration_and_payload(installer):
    i = installer
    # PHONELET_* of the caller's shell stays out of the validation.
    result = i.run(OPTIONS, {'PHONELET_AMI_SECRET': 'changeme', 'PHONELET_API_KEY': 'changeme'})
    assert result.returncode == 0, result.stderr
    rendered = i.snapshot / 'etc-asterisk'
    assert sorted(path.name for path in rendered.iterdir()) == sorted(deploy_module().CONFIGS + ('pjsip.conf',))
    assert (rendered / 'pjsip.conf').read_text() == '#include "pjsip_custom.conf"\n'
    password = next(line.split('=', 1)[1].strip('"') for line in i.env_file.read_text().splitlines()
                    if line.startswith('AST_SIP_PASSWORD='))
    assert password in (rendered / 'pjsip_custom.conf').read_text()
    assert password not in result.stdout + result.stderr
    assert (i.snapshot / 'artifacts.sha256').is_file()
    assert not (i.snapshot / 'target').exists()


@pytest.mark.parametrize('option', ['--asterisk-build', '--asterisk-archive'])
def test_install_resolves_relative_paths_from_the_callers_directory(installer, option):
    i = installer
    path = 'build' if option == '--asterisk-build' else 'build/' + i.archive.name
    result = i.run([option, path] + OPTIONS, cwd=i.root)
    assert result.returncode == 0, result.stderr
    assert (i.snapshot / 'archive').read_bytes() == i.archive.read_bytes()


@pytest.mark.parametrize('name', ['../other', '/absolute', 'nested/name', 'a.b', 'a_b', 'Upper', '-prefix',
                                 'has space', "x'; echo unsafe", 'unknown-target', 'docs-only',
                                 # Existing directories with a profile: only the name check refuses them.
                                 '../targets/synthetic', 'synthetic/', 'orangepi-zero2w/.'])
def test_install_rejects_invalid_or_unselectable_targets_before_commands(installer, name):
    if name == 'docs-only':
        target = installer.tree / 'targets/docs-only'
        target.mkdir()
        (target / 'README.md').write_text('# Docs-only target\n')
    result = installer.run(OPTIONS, {'PHONELET_BOOTSTRAP_TARGET': name})
    assert result.returncode == 2 and calls(installer) == []
    assert list((installer.host / 'root').iterdir()) == []
    if name == 'docs-only':
        assert 'run without PHONELET_BOOTSTRAP_TARGET' in result.stderr
        assert 'targets/docs-only/README.md' in result.stderr


@pytest.mark.parametrize('profile', ['', '# Only documentation', 'target_setup() { test -f "$TARGET_DIR/asset"; }'])
def test_install_discovers_a_new_target_in_its_tree_from_another_directory(installer, profile):
    i = installer
    target = i.tree / 'targets/7-synthetic'
    target.mkdir()
    (target / 'profile.sh').write_text(profile)
    (target / 'asset').write_text('synthetic asset\n')
    # A caller-local folder with the same name must never be selected.
    caller = i.root / 'caller'
    (caller / 'targets/7-synthetic').mkdir(parents=True)
    (caller / 'targets/7-synthetic/profile.sh').write_text('exit 99\n')
    result = i.run(OPTIONS, {'PHONELET_BOOTSTRAP_TARGET': '7-synthetic'}, cwd=caller)
    assert result.returncode == 0, result.stderr
    assert (target / 'profile.sh').read_text() == profile
    assert not (i.snapshot / 'target').exists()


@pytest.mark.parametrize('wifi', ['0', '1'])
def test_install_warns_about_sms_commands_without_wifi_packages(installer, wifi):
    i = installer
    i.existing_env()
    key = base64.urlsafe_b64encode(b'\xab' * 32).decode().rstrip('=')
    with i.env_file.open('a') as stream:
        stream.write('PHONELET_SMS_COMMAND_KEY=' + key + '\n')
    result = i.run(extra_env={'PHONELET_BOOTSTRAP_WIFI': wifi})
    assert result.returncode == 0, result.stderr
    assert ('Warning: SMS commands enabled but Wi-Fi packages not selected' in result.stderr) == (wifi == '0')
    assert key not in result.stdout + result.stderr + '\n'.join(calls(i))


@pytest.mark.parametrize('error', ['EOFError', 'KeyboardInterrupt'])
@pytest.mark.parametrize('answered', [0, 2])
def test_init_env_cancelled_prompt_writes_nothing_and_prints_one_line(tmp_path, error, answered):
    path = tmp_path / 'cancelled.env'
    # A TTY whose user answers some prompts, then presses Ctrl-D or Ctrl-C.
    driver = f"""import builtins, runpy, sys
answers = iter(['+1', '0'][:{answered}])
def answer(prompt):
    for value in answers:
        return value
    raise {error}
sys.stdin.isatty = lambda: True
builtins.input = answer
sys.argv = ['deploy-release.py', '--init-env', {str(path)!r}]
runpy.run_path({str(ROOT / 'scripts/deploy-release.py')!r}, run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-c', driver], capture_output=True, text=True,
                            env=os.environ | {'PYTHONPATH': str(ROOT / 'src')})
    assert result.returncode == 1
    assert result.stderr == '\nCancelled: no .env written.\n'
    assert result.stdout == ''
    assert not path.exists()


@pytest.mark.parametrize('option, nested, alias', [
    ('--asterisk-build', False, False), ('--asterisk-build', True, False),
    ('--asterisk-archive', True, False), ('--asterisk-build', True, True),
    ('--asterisk-archive', True, True),
])
@pytest.mark.parametrize('upgrade', [False, True])
@pytest.mark.parametrize('root_alias', [False, True])
def test_install_sweep_preserves_caller_inputs_inside_leftover_work(installer, option, nested, alias, upgrade,
                                                                 root_alias):
    i = installer
    if upgrade:
        i.update()
    if root_alias:
        (i.host / 'root').rename(i.host / 'private-root')
        (i.host / 'root').symlink_to('private-root', target_is_directory=True)
    leftover = i.host / 'root/phonelet-install.INPUT123'
    build = leftover / 'build' if nested else leftover
    shutil.copytree(i.build, build)
    archive = build / i.archive.name
    source = build if option == '--asterisk-build' else archive
    if alias:
        link = i.root / 'input-link'
        link.symlink_to(source, target_is_directory=source.is_dir())
        source = link
    before = {p: p.read_bytes() for p in build.iterdir()}
    arguments = [option, str(source)] + ([] if upgrade else OPTIONS)
    result = i.run(arguments)
    assert result.returncode == 0, result.stderr
    assert all(p.read_bytes() == content for p, content in before.items())
    assert list((i.host / 'root').glob('phonelet-install.*')) == [leftover]
    assert 'check-env' in calls(i) if upgrade else any(c.startswith('prepare ') for c in calls(i))


def test_install_renders_modem_paths_with_quoted_dotenv_keys(installer):
    i = installer
    i.existing_env()
    with i.env_file.open('a') as stream:
        stream.write("'AST_DONGLE_DATA'='/dev/ttyCustomData'\n'AST_DONGLE_AUDIO'='/dev/ttyCustomAudio'\n")
    result = i.run(['--asterisk-build', str(i.build)])
    assert result.returncode == 0, result.stderr
    dongle = (i.snapshot / 'etc-asterisk/dongle.conf').read_text()
    assert 'data=/dev/ttyCustomData\n' in dongle and 'audio=/dev/ttyCustomAudio\n' in dongle
    assert not (i.snapshot / 'env').exists()


@pytest.mark.parametrize('upgrade', [False, True])
def test_install_uses_verified_private_build_after_source_replacement(installer, upgrade):
    i = installer
    if upgrade:
        i.update()
    content = i.root / 'content'
    content.write_text('verified build\n')
    with tarfile.open(i.archive, 'w:gz') as archive:
        archive.add(content, arcname='content')
    original_archive = i.archive.read_bytes()
    original_sums = (hashlib.sha256(original_archive).hexdigest() + '  ' + i.archive.name + '\n'
                     + '0' * 64 + '  stage/usr/sbin/asterisk\n'
                     + '1' * 64 + '  stage/usr/lib/asterisk/modules/chan_dongle.so\n')
    (i.build / 'artifacts.sha256').write_text(original_sums)
    replacement = i.root / 'replacement.tar.gz'
    content.write_text('concurrent rebuild\n')
    with tarfile.open(replacement, 'w:gz') as archive:
        archive.add(content, arcname='content')
    replacement_sums = i.root / 'replacement.sha256'
    replacement_sums.write_text(original_sums.replace(hashlib.sha256(original_archive).hexdigest(),
                                                     hashlib.sha256(replacement.read_bytes()).hexdigest())
                               .replace('0' * 64, '2' * 64).replace('1' * 64, '3' * 64))
    python = i.commands / 'python3'
    python.write_text(python.read_text().replace('echo venv >> "$COMMAND_LOG"', '''echo replace-source >> "$COMMAND_LOG"
cp "$REPLACEMENT" "$ARCHIVE_SOURCE"
cp "$REPLACEMENT_SUMS" "${ARCHIVE_SOURCE%/*}/artifacts.sha256"
echo venv >> "$COMMAND_LOG"'''))
    bootstrap = i.tree / 'scripts/bootstrap.sh'
    bootstrap.write_text(bootstrap.read_text().replace('cp "$3" "$SNAPSHOT/archive"',
                                                       'cp "$3" "$SNAPSHOT/archive"\ntar -xzf "$3" -C "$UNPACKED"'))
    unpacked = i.root / 'unpacked'
    unpacked.mkdir()
    result = i.run(['--asterisk-build', str(i.build)] + ([] if upgrade else OPTIONS), {
        'REPLACEMENT': str(replacement), 'REPLACEMENT_SUMS': str(replacement_sums), 'UNPACKED': str(unpacked),
    })
    assert result.returncode == 0, result.stderr
    assert calls(i).index('verify') < calls(i).index('replace-source')
    assert i.archive.read_bytes() == replacement.read_bytes()  # The source really changed after verification.
    assert (i.snapshot / 'archive').read_bytes() == original_archive
    assert (i.snapshot / 'artifacts.sha256').read_text() == original_sums
    assert (unpacked / 'content').read_text() == 'verified build\n'
    assert_clean(i)


def test_install_profile_needs_only_exported_target_directory(installer):
    i = installer
    script = i.tree / 'install.sh'
    script.write_text(script.read_text().replace('set -euo pipefail',
                                                 'set -euo pipefail\nexport -n PHONELET_BOOTSTRAP_TARGET', 1))
    bootstrap = i.tree / 'scripts/bootstrap.sh'
    bootstrap.write_text(bootstrap.read_text().replace('set -euo pipefail',
                                                       'set -euo pipefail\n[[ ! -v PHONELET_BOOTSTRAP_TARGET ]]', 1))
    result = i.run(OPTIONS, {'PHONELET_BOOTSTRAP_TARGET': 'synthetic'})
    assert result.returncode == 0, result.stderr
    assert 'profile prepare synthetic' in calls(i) and 'profile install synthetic' in calls(i)
