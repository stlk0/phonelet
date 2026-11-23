"""Release deployment, configuration, preflight, smoke checks and rollback."""
import builtins
import fcntl
import inspect
import io
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import runpy
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error

import pytest

from phonelet.db import Database

from tests.helpers import ROOT, executable, script


# `asterisk -rx 'manager show user phonelet'` from Asterisk 20.18.2 with the rendered template
# (isolated podman check, 2026-10-21); Asterisk prints permissions in its own order.
MANAGER_USER = '''
          username: phonelet
            secret: <Set>
               ACL: yes
         read perm: <none>
        write perm: reporting,message
   displayconnects: yes
allowmultiplelogin: yes
         Variables:
ACL: (unnamed)
---------------------------------------------
  0:  deny - 0.0.0.0/0.0.0.0
  1: allow - 127.0.0.1/255.255.255.255
'''


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    module = script('deploy-release')
    stage, root, etc = [tmp_path / name for name in ('stage', 'root', 'etc')]
    for directory in (stage, root, etc, tmp_path / 'recordings', tmp_path / 'netplan'):
        directory.mkdir()
    shutil.copytree(ROOT / 'asterisk', stage / 'asterisk')
    shutil.copytree(ROOT / 'scripts', stage / 'scripts')
    shutil.copytree(ROOT / 'src', stage / 'src')
    env = {'PHONELET_AMI_SECRET': 'real-secret', 'PHONELET_API_KEY': 'real-key',
           'AST_SIP_PASSWORD': 'real-password', 'PHONELET_SIP_DOMAIN': '127.0.0.1',
           'AST_COUNTRY_PREFIX': '+1', 'AST_TRUNK_PREFIX': '1',
           'PHONELET_DB_PATH': str(tmp_path / 'phonelet.db'),
           'PHONELET_RECORDINGS_DIR': str(tmp_path / 'recordings'),
           'PHONELET_WIFI_STATE_DIR': str(tmp_path / 'wifi'),
           'PHONELET_WIFI_NETPLAN_FILE': str(tmp_path / 'netplan/30-wifis-dhcp.yaml'),
           'AST_DONGLE_DATA': '/dev/null', 'AST_DONGLE_AUDIO': '/dev/null'}
    (root / '.env').write_text('\n'.join(k + '=' + v for k, v in env.items()))
    for name in ('extensions', 'manager', 'pjsip'):
        (etc / (name + '.conf')).write_text('#include "' + name + '_custom.conf"\n')
    rendered = module.render(stage, module.configuration(root / '.env')[0])
    for name in module.CONFIGS:
        if name != 'cdr_custom.conf':  # absence must be restored on failure
            shutil.copyfile(rendered / name, etc / name)
    (etc / 'extensions_custom.conf').write_text('; old dialplan\n')
    systemd = tmp_path / 'systemd'
    dropin = systemd / 'phonelet.service.d/20-phonelet-release.conf'
    dropin.parent.mkdir(parents=True)
    dropin.write_text('[Service]\nExecStart=/old/.venv/bin/python old-app\n')
    helper = tmp_path / 'helper'
    helper.write_text('old helper')
    with sqlite3.connect(tmp_path / 'phonelet.db') as db:
        db.execute('create table preserved (value text)')
        db.execute("insert into preserved values ('original')")
        db.execute(f'PRAGMA user_version={Database.SCHEMA_VERSION}')
    commands = tmp_path / 'commands'
    log = tmp_path / 'commands.log'
    monkeypatch.setenv('COMMAND_LOG', str(log))
    monkeypatch.setenv('FAIL_ONCE', str(tmp_path / 'failed-once'))
    monkeypatch.setenv('STOP_COUNT', str(tmp_path / 'stop-count'))
    manager_user = tmp_path / 'manager-user.txt'
    manager_user.write_text(MANAGER_USER)
    monkeypatch.setenv('MANAGER_USER', str(manager_user))
    executable(stage / '.venv/bin/python', 'echo "python $*" >> "$COMMAND_LOG"\n')
    executable(commands / 'systemctl', '''echo "systemctl $*" >> "$COMMAND_LOG"
if [[ "${FAIL_PHASE:-}" == restart && "$*" == 'restart phonelet' && ! -e "$FAIL_ONCE" ]]; then
 touch "$FAIL_ONCE"
 exit 1
fi
case "$*" in
 'show --property=User --value phonelet') echo "${PHONELET_SERVICE_USER:-root}" ;;
 'show --property=LoadState --value phonelet') echo "${PHONELET_LOAD_STATE:-loaded}" ;;
 # PHONELET_INACTIVE: the service is down until the deploy starts it.
 'is-active --quiet phonelet')
  [[ -z "${PHONELET_INACTIVE:-}" ]] || grep -q '^systemctl restart phonelet$' "$COMMAND_LOG" || exit 3 ;;
 # FAIL_STOPS: the numbers of the 'stop phonelet' calls that fail, e.g. "2" for the rollback's first.
 'stop phonelet')
  count=$(( $(cat "$STOP_COUNT" 2>/dev/null || echo 0) + 1 ))
  echo "$count" > "$STOP_COUNT"
  [[ " ${FAIL_STOPS:-} " != *" $count "* ]] || { echo 'Job for phonelet.service timed out.' >&2; exit 1; } ;;
 'reset-failed asterisk') [[ -z "${RESET_FAILED_FAILS:-}" ]] || exit 1 ;;
esac
''')
    executable(commands / 'asterisk', '''echo "asterisk $*" >> "$COMMAND_LOG"
case "$*" in
 '-rx core show application DongleSendSMSBase64') echo DongleSendSMSBase64 ;;
 '-rx core show application TryExec'|'-rx core show application Originate')
  # Asterisk 20.18.2 (isolated check, 2026-10-23); a missing application's reply names it too.
  if [[ "${MISSING_APP:-}" == "${2##* }" ]]; then printf "Your application(s) is (are) not registered\\nCommand '%s' failed.\\n" "$2"
  else printf "\\e[1;35m  -= Info about Application '%s' =- \\e[0m\\n" "${2##* }"; fi ;;
 '-rx core show function REGEX')
  if [[ "${MISSING_APP:-}" == REGEX ]]; then printf "No function by that name registered.\\nCommand '%s' failed.\\n" "$2"
  else printf "\\e[1;35m  -= Info about Function '%s' =- \\e[0m\\n" "${2##* }"; fi ;;
 '-rx module show like cdr_custom.so') echo cdr_custom.so ;;
 '-rx module show like cdr_csv.so')
  [[ -z "${CDR_CSV:-}" ]] || printf 'cdr_csv.so  Comma Separated Values CDR Backend  0  %s  extended\n' "$CDR_CSV" ;;
 '-rx module show like '*)
  if [[ " ${RUNNING_MODULES:-} " == *" ${2##* } "* ]]; then printf '%s  Module  0  Running  core\n' "${2##* }"
  elif [[ " ${STOPPED_MODULES:-} " == *" ${2##* } "* ]]; then printf '%s  Module  0  Not Running  core\n' "${2##* }"; fi ;;
 '-rx module unload '*)
  if [[ "${UNLOAD_FAILS:-}" == "${2##* }" ]]; then printf 'Unable to unload resource %s\n' "${2##* }"
  else printf 'Unloaded %s\n' "${2##* }"; fi ;;
 '-rx core show channels count') echo '0 active calls' ;;
 '-rx cdr show active')
  printf 'Channels with Call Detail Record (CDR) Information\n--------------------------------------------------\n'
  if [[ -n "${CDR_ACTIVE:-}" ]]; then printf 'Message/ast_msg_queue     <none>     Hangup  11:05:35 11:05:35 09:45:09 1 1\n'; fi ;;
 '-rx cdr show status')
  printf '  * Registered Backends\n    -------------------\n      cdr-custom\n'
  if [[ -n "${CDR_STILL_LOGS:-}" ]]; then printf '      %s\n' "$CDR_STILL_LOGS"; fi
  [[ "${CDR_STILL_LOGS:-}" == cdr_manager ]] || printf '      cdr_manager (suspended) \n' ;;
 '-rx pjsip show endpoint phone') [[ "${FAIL_PHASE:-}" == endpoint ]] || echo 'Endpoint: phone/phone' ;;
 '-rx manager show user phonelet') cat "$MANAGER_USER" ;;
 '-rx dongle show devices') echo 'dongle0 Free' ;;
esac
if [[ "${FAIL_PHASE:-}" == reload && "$*" == '-rx dialplan reload' && ! -e "$FAIL_ONCE" ]]; then
 touch "$FAIL_ONCE"
 echo 'Unable to reload dialplan'
fi
exit 0
''')
    executable(commands / 'lame', 'exit 0\n')
    monkeypatch.setenv('PATH', str(commands) + ':' + os.environ['PATH'])
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda path: True if str(path) in ('/var/spool/asterisk/phonelet-sms', '/var/log/asterisk/cdr-custom') else original_is_dir(path))
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10**12))
    monkeypatch.setattr(module, 'require_durable_sms_driver', lambda _: None)
    # A host with a real MDM link must not change the outcome: hide it from os.stat (and so
    # os.path.exists); only fake_devices below decides whether the link exists and what it is.
    real_stat = os.stat
    def stat(path, *args, **kwargs):
        if not isinstance(path, int) and os.fspath(path) == module.MDM_LINK:
            raise FileNotFoundError(2, 'Hidden host device', os.fspath(path))
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', stat)
    class Response:
        status = 200
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.body).encode()
    def urlopen(request, timeout):
        if request.full_url.endswith('/ready'):
            return Response({'status': 'ready', 'software_ready': True, 'modem_ready': True})
        assert request.full_url.endswith('/api/modem')
        return Response({'network_registered': True, 'operator': 'Synthetic Operator', 'signal_dbm': -89})
    monkeypatch.setattr(module.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    return SimpleNamespace(module=module, stage=stage, root=root, etc=etc, systemd=systemd,
                           helper=helper, dropin=dropin, log=log, db=tmp_path / 'phonelet.db',
                           wifi_state=tmp_path / 'wifi',
                           manager_user=manager_user,
                           deploy=lambda **kw: module.deploy(stage, root, etc, systemd, helper, **kw))


@pytest.mark.parametrize('phase', ['reload', 'restart', 'endpoint'])
def test_shell_failures_restore_code_dependencies_and_configs_preserving_database(deployment, monkeypatch, phase):
    d = deployment
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    old_unit = d.dropin.read_bytes()
    monkeypatch.setenv('FAIL_PHASE', phase)
    if phase == 'endpoint':
        # Simulate new data written by the candidate before readiness failure.
        original_smoke = d.module.smoke
        def smoke(*args):
            with sqlite3.connect(d.db) as db:
                db.execute("insert into preserved values ('candidate')")
            original_smoke(*args)
        monkeypatch.setattr(d.module, 'smoke', smoke)
    with pytest.raises(Exception):
        d.deploy()
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.dropin.read_bytes() == old_unit  # includes old interpreter/dependencies
    assert d.helper.read_text() == 'old helper'
    with sqlite3.connect(d.db) as db:
        assert db.execute('pragma user_version').fetchone()[0] == Database.SCHEMA_VERSION
        assert db.execute('select value from preserved').fetchall()[0] == ('original',)
        if phase == 'endpoint':
            assert db.execute("select count(*) from preserved where value='candidate'").fetchone()[0] == 12
    assert (d.stage / 'rollback/failed-release.db').exists()
    assert d.log.read_text().rstrip().endswith('systemctl restart phonelet')


SANDBOX_OPTIONS = ('NoNewPrivileges=yes', 'ProtectSystem=full', 'ProtectHome=read-only',
                   'ReadWritePaths=-/etc/netplan', 'ProtectKernelModules=yes', 'ProtectKernelLogs=yes',
                   'ProtectControlGroups=yes', 'ProtectClock=yes', 'LockPersonality=yes', 'RestrictRealtime=yes',
                   'RestrictSUIDSGID=yes', 'RestrictNamespaces=yes',
                   'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK', 'LimitCORE=0')


def test_release_dropin_sandboxes_the_service(deployment):
    d = deployment
    d.deploy()
    lines = d.dropin.read_text().splitlines()
    for option in SANDBOX_OPTIONS:
        assert option in lines
    # wpa_cli's reply socket in /tmp and netplan's udevadm trigger on /sys need the host's view.
    assert not any(line.startswith(('PrivateTmp', 'ProtectKernelTunables')) for line in lines)


@pytest.mark.skipif(not shutil.which('systemd-analyze'), reason='systemd-analyze not installed')
def test_release_dropin_passes_systemd_analyze_verify(deployment, tmp_path):
    d = deployment
    d.deploy()
    unit = tmp_path / 'verify/phonelet.service'
    unit.parent.mkdir()
    unit.write_text('[Unit]\nDescription=Phonelet\n\n[Service]\nType=simple\nUser=root\n'
                    'ExecStart=/bin/sleep infinity\n\n' + d.dropin.read_text())
    done = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                          capture_output=True, text=True, check=False)
    output = done.stdout + done.stderr
    assert done.returncode == 0 and 'ignoring' not in output, output  # an unknown key is only a warning


@pytest.mark.parametrize('check', [False, True])
@pytest.mark.parametrize('key, value', [('PHONELET_DB_PATH', '/root/phonelet.db'),
                                        ('PHONELET_RECORDINGS_DIR', '/boot/recordings'),
                                        ('PHONELET_WIFI_STATE_DIR', '/etc/phonelet-wifi'),
                                        ('PHONELET_WIFI_NETPLAN_FILE', '/usr/lib/netplan/30-wifi.yaml')])
def test_path_the_sandbox_makes_read_only_is_refused_before_any_change(deployment, check, key, value):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write(f'\n{key}={value}\n')
    # The recordings directory is refused earlier, by the asterisk.service check: both sandboxes protect it.
    with pytest.raises(ValueError, match=key + ' (is read-only for phonelet.service|must be outside)'):
        d.deploy(check=check)
    assert not (d.stage / 'rollback').exists() and not d.log.exists()


@pytest.mark.parametrize('failure', [None, 'sandbox', 'wifi', 'bind', 'placeholder', 'driver', 'active-cdr',
                                     'inactive', 'unpacked', 'pip', 'compile', 'io', 'schema', 'include', 'lame',
                                     'service-user', 'service-unloaded'])
def test_check_env_cli_runs_static_preflight_without_host_changes(deployment, monkeypatch, failure, capsys):
    d = deployment
    # Use an explicit FILE rather than root/.env, as promised by the flag.
    env_file = d.root / 'candidate.env'
    (d.root / '.env').rename(env_file)
    expected = {'sandbox': 'PHONELET_DB_PATH', 'wifi': 'Unfinished Wi-Fi change',
                'bind': 'AST_SIP_BIND', 'placeholder': 'PHONELET_AMI_SECRET',
                'pip': 'pip', 'compile': 'compileall', 'io': 'synthetic read error',
                'schema': 'Cannot check schema', 'include': 'Missing Asterisk include',
                'lame': 'Required executable missing: lame', 'service-user': 'existing root Phonelet service',
                'service-unloaded': 'Phonelet service is not loaded'}
    if failure == 'sandbox':
        with env_file.open('a') as stream:
            stream.write('\nPHONELET_DB_PATH=/root/phonelet.db\n')
    elif failure == 'wifi':
        d.wifi_state.mkdir()
        (d.wifi_state / 'pending.json').write_text('{"existed": true}')
    elif failure == 'bind':
        with env_file.open('a') as stream:
            stream.write('\nAST_SIP_BIND=192.0.2.10:5060\n')
        def missing_address(bind):
            assert bind == '192.0.2.10:5060'
            raise RuntimeError('AST_SIP_BIND is not on this host')
        monkeypatch.setattr(d.module, 'check_sip_bind_address', missing_address)
    elif failure == 'placeholder':
        with env_file.open('a') as stream:
            stream.write('\nPHONELET_AMI_SECRET=GENERATE_PRIVATE_SECRET\n')
    elif failure == 'driver':
        def missing_driver(settings):
            raise RuntimeError('Durable SMS driver capability absent')
        monkeypatch.setattr(d.module, 'require_durable_sms_driver', missing_driver)
    elif failure == 'active-cdr':
        monkeypatch.setenv('CDR_STILL_LOGS', 'csv')
        monkeypatch.setenv('CDR_ACTIVE', '1')
    elif failure == 'inactive':
        executable(Path(shutil.which('systemctl')), '''[[ "$1" == show ]] || exit 3
[[ "$*" != 'show --property=LoadState --value phonelet' ]] || echo loaded
''')
    elif failure == 'unpacked':
        Path(shutil.which('asterisk')).unlink()
    elif failure == 'io':
        def unreadable(stage, env):
            raise OSError('synthetic read error')
        monkeypatch.setattr(d.module, 'render', unreadable)
    elif failure == 'schema':
        d.db.write_bytes(b'synthetic invalid database')
    elif failure == 'include':
        (d.etc / 'manager.conf').write_text('; missing custom include\n')
    elif failure == 'lame':
        original_which = d.module.shutil.which
        monkeypatch.setattr(d.module.shutil, 'which', lambda name: None if name == 'lame' else original_which(name))
    elif failure == 'service-user':
        monkeypatch.setenv('PHONELET_SERVICE_USER', 'nobody')
    elif failure == 'service-unloaded':
        monkeypatch.setenv('PHONELET_LOAD_STATE', 'not-found')
    original_run = d.module.run
    def static_run(*arguments, **kwargs):
        assert arguments[0] != 'asterisk'
        if arguments[0] == 'systemctl':
            assert arguments[1] == 'show' and arguments[-1] == 'phonelet'
        if failure == 'pip' and arguments[1:4] == ('-m', 'pip', 'check'):
            raise subprocess.CalledProcessError(1, arguments)
        if failure == 'compile' and arguments[1:3] == ('-m', 'compileall'):
            raise subprocess.TimeoutExpired(arguments, 60)
        return original_run(*arguments, **kwargs)
    monkeypatch.setattr(d.module, 'run', static_run)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for directory in (d.root, d.etc, d.systemd) for path in directory.rglob('*') if path.is_file()}
    before.update({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (d.helper, d.db)})
    arguments = ['--check-env', str(env_file), '--stage', str(d.stage), '--root', str(d.root),
                 '--etc', str(d.etc), '--systemd', str(d.systemd), '--helper', str(d.helper)]
    if failure in expected:
        with pytest.raises(SystemExit, match=expected[failure]) as error:
            d.module.main(arguments)
        assert str(error.value).endswith('Asterisk not changed')
        assert 'GENERATE_PRIVATE_SECRET' not in str(error.value)
    else:
        d.module.main(arguments)
        assert capsys.readouterr().out == 'Pre-upgrade check passed\n'
        assert (d.stage / 'runtime.env').is_file()
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == saved for path, saved in before.items())
    assert not (d.root / '.deploy.lock').exists() and not (d.stage / 'rollback').exists()
    calls = d.log.read_text() if d.log.exists() else ''
    assert not re.search(r'systemctl (?:stop|start|restart|reload|daemon-reload)|module unload|dialplan reload', calls)


@pytest.mark.parametrize('allowed', [False, True])
def test_pre_upgrade_transport_change_requires_permission_from_files_only(deployment, monkeypatch, capsys, allowed):
    d = deployment
    current = d.etc / 'pjsip_custom.conf'
    current.write_text(current.read_text().replace(':5060', ':5090'))
    original_run = d.module.run
    def static_run(*arguments, **kwargs):
        assert arguments[0] != 'asterisk'
        if arguments[0] == 'systemctl':
            assert arguments[1] == 'show' and arguments[-1] == 'phonelet'
        return original_run(*arguments, **kwargs)
    monkeypatch.setattr(d.module, 'run', static_run)
    arguments = ['--check-env', str(d.root / '.env'), '--stage', str(d.stage), '--etc', str(d.etc)]
    if allowed:
        d.module.main(arguments + ['--allow-transport-change'])
        assert capsys.readouterr().out == 'Pre-upgrade check passed\n'
    else:
        with pytest.raises(SystemExit, match='SIP transport changed') as error:
            d.module.main(arguments)
        assert '--allow-asterisk-restart' in str(error.value) and '--allow-transport-change' in str(error.value)
        assert str(error.value).endswith('Asterisk not changed')
    assert not (d.stage / 'rollback').exists() and not (d.stage / 'phonelet-release.conf').exists()


@pytest.mark.parametrize('mode', ['--check-env', '--render-configs'])
@pytest.mark.parametrize('flag', ['--check', '--allow-asterisk-restart', '--restart-asterisk'])
def test_config_cli_refuses_deploy_flags_before_reading_files(tmp_path, capsys, mode, flag):
    module = script('deploy-release')
    arguments = [mode, str(tmp_path / 'absent.env')]
    if mode == '--render-configs':
        arguments.append(str(tmp_path / 'configs'))
    with pytest.raises(SystemExit) as error:
        module.main(arguments + [flag])
    assert error.value.code == 2 and 'deployment-only options' in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('failure', ['environment', 'template'])
def test_render_configs_cli_distinguishes_environment_and_template_errors(deployment, monkeypatch, failure):
    d = deployment
    monkeypatch.setattr(d.module, 'ASTERISK_DIR', d.stage / 'asterisk')
    with (d.root / '.env').open('a') as stream:
        stream.write('\nAST_TRUNK_PREFIX=\n')
        if failure == 'environment':
            stream.write('AST_COUNTRY_PREFIX=private-invalid-prefix\n')
    if failure == 'template':
        template = d.stage / 'asterisk/pjsip_custom.conf.tpl'
        template.write_text(template.read_text() + '\n; ${AST_TRUNK_PREFIX}\n')
    with pytest.raises(SystemExit) as error:
        d.module.main(['--render-configs', str(d.root / '.env'), str(d.stage / 'configs')])
    message = str(error.value)
    assert message.startswith('Invalid environment file: ' if failure == 'environment' else 'Invalid Asterisk template: ')
    assert 'private-invalid-prefix' not in message
    if failure == 'template':
        assert 'pjsip_custom.conf.tpl' in message and 'AST_TRUNK_PREFIX' in message


def test_relative_database_path_is_refused_as_not_absolute(deployment, tmp_path, monkeypatch):
    d = deployment
    # A release in a read-only tree, as /root/phonelet-releases is: the clearer error still wins.
    monkeypatch.setattr(d.module, 'SANDBOX_READ_ONLY', (str(tmp_path),))
    with (d.root / '.env').open('a') as stream:
        stream.write('\nPHONELET_DB_PATH=phonelet.db\n')
    with pytest.raises(ValueError, match='Database path must be absolute'):
        d.deploy(check=True)


@pytest.mark.parametrize('missing', [False, True])
def test_missing_netplan_directory_is_only_a_warning(deployment, tmp_path, capsys, missing):
    d = deployment
    if missing:
        with (d.root / '.env').open('a') as stream:
            stream.write(f'\nPHONELET_WIFI_NETPLAN_FILE={tmp_path / "absent/30-wifis-dhcp.yaml"}\n')
    d.deploy(check=True)
    warned = 'Warning: ' + str(tmp_path / 'absent') + ' does not exist' in capsys.readouterr().out
    assert warned == missing


def test_symlink_into_a_read_only_tree_is_refused(deployment, tmp_path):
    d = deployment
    link = tmp_path / 'recordings-link'
    link.symlink_to('/usr/share')
    with (d.root / '.env').open('a') as stream:
        stream.write(f'\nPHONELET_RECORDINGS_DIR={link}\n')
    with pytest.raises(ValueError, match='PHONELET_RECORDINGS_DIR (is read-only for phonelet.service|must be outside)'):
        d.deploy()


@pytest.mark.parametrize('pre_upgrade', [False, True])
def test_caller_phonelet_environment_does_not_reach_runtime_env(deployment, monkeypatch, pre_upgrade):
    # pydantic reads os.environ for fields the .env leaves out; deployment reads the .env only.
    d = deployment
    monkeypatch.setenv('PHONELET_LOG_LEVEL', 'DEBUG')
    d.deploy(pre_upgrade=pre_upgrade)
    assert 'PHONELET_LOG_LEVEL="INFO"\n' in (d.stage / 'runtime.env').read_text()
    assert os.environ['PHONELET_LOG_LEVEL'] == 'DEBUG'


def test_success_requires_complete_smoke_and_preserves_private_bind(deployment):
    d = deployment
    d.deploy()
    assert '--host 127.0.0.1 --port 8080' in d.dropin.read_text()
    assert 'UMask=0077' in d.dropin.read_text()
    assert 'TimeoutStopSec=30\n' in d.dropin.read_text()  # systemd kills a candidate that ignores SIGTERM
    assert f'EnvironmentFile=\nEnvironmentFile={d.stage / "runtime.env"}' in d.dropin.read_text()
    assert 'PHONELET_API_KEY="real-key"' in (d.stage / 'runtime.env').read_text()
    assert str(d.stage / '.venv/bin/python') in d.dropin.read_text()
    assert 'remove_existing=yes' in (d.etc / 'pjsip_custom.conf').read_text()
    assert 'rtp_symmetric=yes' in (d.etc / 'pjsip_custom.conf').read_text()
    assert 'pjsip show endpoint phone' in d.log.read_text()
    assert 'core show application TryExec' in d.log.read_text()
    assert 'core show application Originate' in d.log.read_text()
    assert 'core show function REGEX' in d.log.read_text()


@pytest.mark.parametrize('check', [False, True])
@pytest.mark.parametrize('state', ['same', 'empty', 'missing', 'closed-wal'])
def test_deploy_schema_preflight_accepts_compatible_or_absent_database(deployment, monkeypatch, state, check):
    d = deployment
    if state in ('empty', 'missing'):
        d.db.unlink()
        if state == 'empty':
            sqlite3.connect(d.db).close()
    elif state == 'closed-wal':
        with sqlite3.connect(d.db) as connection:
            connection.execute('PRAGMA journal_mode=WAL')
        connection.close()
    before = (d.db.read_bytes(), d.db.stat().st_mtime_ns) if d.db.exists() else None
    original_stop = d.module.stop_application
    checked = []

    def assert_unchanged():
        after = (d.db.read_bytes(), d.db.stat().st_mtime_ns) if d.db.exists() else None
        assert after == before
        assert not d.db.with_name(d.db.name + '-wal').exists()
        assert not d.db.with_name(d.db.name + '-shm').exists()
        checked.append(True)

    def stop():
        assert_unchanged()  # Verify preflight separately from the existing deploy snapshot.
        original_stop()

    monkeypatch.setattr(d.module, 'stop_application', stop)
    d.deploy(check=check)
    if check:
        assert_unchanged()
    assert checked == [True]


@pytest.mark.parametrize('check', [False, True])
@pytest.mark.parametrize('version', [0, Database.SCHEMA_VERSION - 1, Database.SCHEMA_VERSION + 1])
def test_deploy_schema_preflight_refuses_foreign_schema_without_mutation(deployment, version, check, capsys):
    d = deployment
    with sqlite3.connect(d.db) as connection:
        connection.execute(f'PRAGMA user_version={version}')
    before = outside_release(d)
    with pytest.raises(RuntimeError) as error:
        d.deploy(check=check)
    message = str(error.value)
    assert f'has schema version {version}, this release needs {Database.SCHEMA_VERSION}' in message
    assert 'docs/operations.md, Recreating the database' in message
    assert outside_release(d) == before
    assert 'systemctl stop' not in d.log.read_text()
    assert not (d.stage / 'rollback').exists()
    assert not d.db.with_name(d.db.name + '-wal').exists()
    assert not d.db.with_name(d.db.name + '-shm').exists()
    assert 'Summary:' not in capsys.readouterr().out


@pytest.mark.parametrize('check', [False, True])
@pytest.mark.parametrize('failure', ['open', 'read'])
def test_deploy_schema_preflight_sqlite_error_refuses_without_mutation(deployment, monkeypatch, capsys,
                                                                     check, failure):
    d = deployment
    if failure == 'open':
        def connect(*args, **kwargs):
            raise sqlite3.OperationalError('synthetic-private-error')
        monkeypatch.setattr(d.module.sqlite3, 'connect', connect)
    else:
        d.db.write_bytes(b'synthetic corrupt database')
    before = outside_release(d)
    with pytest.raises(SystemExit) as error:
        d.deploy(check=check)
    message = str(error.value)
    assert str(d.db) in message
    assert 'docs/operations.md, Recreating the database' in message
    assert 'synthetic-private-error' not in message
    assert outside_release(d) == before
    assert 'systemctl stop' not in d.log.read_text()
    assert not (d.stage / 'rollback').exists()
    output = capsys.readouterr()
    assert 'Traceback' not in output.out + output.err


def test_schema_read_error_exits_without_traceback(tmp_path):
    database = tmp_path / 'corrupt.db'
    database.write_bytes(b'synthetic corrupt database')
    code = ('import runpy, sys; from pathlib import Path; '
            "runpy.run_path(sys.argv[1])['check_database_schema'](Path(sys.argv[2]))")
    result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts/deploy-release.py'), str(database)],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 1
    assert str(database) in result.stderr
    assert 'docs/operations.md, Recreating the database' in result.stderr
    assert 'Traceback' not in result.stdout + result.stderr
    assert database.read_bytes() == b'synthetic corrupt database'


@pytest.mark.parametrize('version', [Database.SCHEMA_VERSION, Database.SCHEMA_VERSION + 1])
def test_deploy_schema_preflight_reads_live_wal_version(deployment, version):
    d = deployment
    connection = sqlite3.connect(d.db)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute(f'PRAGMA user_version={version}')
        before = (d.db.read_bytes(), d.db.stat().st_mtime_ns)
        if version == Database.SCHEMA_VERSION:
            d.deploy(check=True)
        else:
            with pytest.raises(RuntimeError, match=f'has schema version {version}'):
                d.deploy(check=True)
        assert (d.db.read_bytes(), d.db.stat().st_mtime_ns) == before
        assert 'systemctl stop' not in d.log.read_text()
    finally:
        connection.close()


@pytest.mark.parametrize('check', [False, True])
def test_deploy_schema_preflight_skips_missing_wal_shared_memory(deployment, monkeypatch, capsys, check):
    d = deployment
    # A WAL without its SHM can be left after an interrupted database session.
    d.db.with_name(d.db.name + '-wal').write_bytes(b'')
    before = outside_release(d)
    original_stop = d.module.stop_application
    checked = []

    def assert_unchanged():
        assert outside_release(d) == before
        assert not d.db.with_name(d.db.name + '-shm').exists()
        checked.append(True)

    def stop():
        assert_unchanged()
        original_stop()

    monkeypatch.setattr(d.module, 'stop_application', stop)
    d.deploy(check=check)
    if check:
        assert_unchanged()
        assert 'systemctl stop' not in d.log.read_text()
    else:
        assert 'systemctl restart phonelet' in d.log.read_text()
    assert checked == [True]
    output = capsys.readouterr().out
    assert output.count('Warning: schema not checked: ') == 1
    assert ('Deployment verified' in output) == (not check)


@pytest.mark.parametrize('registered', [True, False])
@pytest.mark.parametrize('details', [None, 'dbm', 'rssi', 'unknown'])
@pytest.mark.parametrize('bind,port', [('127.0.0.1:5077', '5077'), ('127.0.0.1', '5060')])
def test_deploy_summary_uses_smoke_body_and_rendered_sip_without_secrets(deployment, monkeypatch, capsys,
                                                                     registered, details, bind, port):
    d = deployment
    revision = '0123456789ab'
    (d.stage / 'REVISION').write_text(revision)
    with (d.root / '.env').open('a') as stream:
        stream.write('\nAST_SIP_USERNAME=synthetic-phone\nPHONELET_SIP_DOMAIN=192.0.2.10\n'
                     'AST_SIP_BIND=' + bind + '\n')
    template = d.stage / 'asterisk/pjsip_custom.conf.tpl'
    template.write_text(template.read_text().replace('protocol=udp', 'protocol=tcp'))
    readiness = {'software_ready': True, 'modem_ready': registered}
    modem = {'network_registered': registered, 'ignored_secret': 'synthetic-hidden-value'}
    if details:
        modem.update(operator='Synthetic\x00\t\r\n Operator\x1b\u200b',
                     signal_dbm=-89 if details == 'dbm' else None,
                     signal_rssi=99 if details == 'unknown' else 12)
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        assert 0 < timeout <= 5
        assert request.get_header('X-api-key') == 'real-key'
        if request.full_url == 'http://127.0.0.1:8080/api/modem':
            assert 'systemctl restart phonelet' in d.log.read_text()
            return Response(modem)
        assert request.full_url == 'http://127.0.0.1:8080/ready'
        if not registered:
            raise urllib.error.HTTPError(request.full_url, 503, 'unavailable', {},
                                         io.BytesIO(json.dumps(readiness).encode()))
        return Response(readiness)

    class Response:
        status = 200
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.body).encode()

    monkeypatch.setattr(d.module.urllib.request, 'urlopen', urlopen)
    d.deploy(allow_restart=True)
    output = capsys.readouterr()
    summary = output.out.split('Summary:\n')[1].split('Deployment verified')[0]
    assert 'Release revision: ' + revision in summary
    assert 'Software ready: yes' in summary
    state = 'yes' if registered else 'no'
    assert f'Modem ready: {state}; registered: {state}' in summary
    assert ('Operator: Synthetic Operator' in summary) == bool(details)
    assert ('Signal: -89 dBm' in summary) == (details == 'dbm')
    assert ('Signal RSSI: 12' in summary) == (details == 'rssi')
    assert ('Signal' in summary) == (details in ('dbm', 'rssi'))
    assert all(char.isprintable() or char == '\n' for char in summary)
    assert 'SIP server: 192.0.2.10' in summary
    assert f'SIP port: {port}; transport: tcp' in summary
    assert 'SIP user: synthetic-phone' in summary
    assert 'grep AST_SIP_PASSWORD ' + str(d.root / '.env') in summary
    assert 6 <= len(summary.splitlines()) + 1 <= 10
    assert [request.full_url for request in requests] == ['http://127.0.0.1:8080/ready',
                                                         'http://127.0.0.1:8080/api/modem']
    assert all(secret not in output.out + output.err for secret in
               ('real-password', 'real-key', 'real-secret', 'synthetic-hidden-value'))


@pytest.mark.parametrize('failure', ['http', 'timeout', 'invalid-json', 'invalid-body'])
def test_deploy_summary_modem_failure_is_best_effort_without_secrets(deployment, monkeypatch, capsys, failure):
    d = deployment
    original_urlopen = d.module.urllib.request.urlopen
    requests = []

    def urlopen(request, timeout):
        requests.append(request.full_url)
        if request.full_url.endswith('/ready'):
            return original_urlopen(request, timeout)
        assert request.full_url == 'http://127.0.0.1:8080/api/modem'
        assert request.get_header('X-api-key') == 'real-key'
        assert 0 < timeout <= 5
        private = 'real-password real-key real-secret'
        if failure == 'http':
            raise urllib.error.HTTPError(request.full_url, 503, private, {}, io.BytesIO(private.encode()))
        if failure == 'timeout':
            raise TimeoutError(private)
        return io.BytesIO(private.encode() if failure == 'invalid-json' else b'[]')

    monkeypatch.setattr(d.module.urllib.request, 'urlopen', urlopen)
    d.deploy()
    output = capsys.readouterr()
    reason = {'http': 'HTTP 503', 'timeout': 'TimeoutError', 'invalid-json': 'JSONDecodeError',
              'invalid-body': 'ValueError'}[failure]
    assert output.out.count(f'Modem status: unavailable ({reason})') == 1
    assert 'Summary:' in output.out and 'Deployment verified' in output.out
    assert len(requests) == 2
    assert all(secret not in output.out + output.err for secret in ('real-password', 'real-key', 'real-secret'))
    assert d.log.read_text().count('systemctl restart phonelet') == 1


@pytest.mark.parametrize('error_type', [ValueError, OSError])
def test_deploy_summary_exception_does_not_fail_verified_deploy(deployment, monkeypatch, capsys, error_type):
    d = deployment

    def summary(*args):
        raise error_type('real-password real-key real-secret')

    monkeypatch.setattr(d.module, 'print_summary', summary)
    d.deploy()
    output = capsys.readouterr()
    assert output.out.count(f'Warning: summary unavailable ({error_type.__name__})') == 1
    assert output.out.rstrip().splitlines()[-1].startswith('Deployment verified')
    assert all(secret not in output.out + output.err for secret in ('real-password', 'real-key', 'real-secret'))
    assert d.log.read_text().count('systemctl restart phonelet') == 1


@pytest.mark.parametrize('outcome', ['check', 'rollback', 'after-smoke-failure'])
def test_deploy_summary_is_absent_without_verified_deploy(deployment, monkeypatch, capsys, outcome):
    d = deployment
    original_urlopen = d.module.urllib.request.urlopen
    requests = []

    def urlopen(request, timeout):
        requests.append(request.full_url)
        return original_urlopen(request, timeout)

    monkeypatch.setattr(d.module.urllib.request, 'urlopen', urlopen)
    if outcome == 'check':
        d.deploy(check=True)
    else:
        if outcome == 'rollback':
            monkeypatch.setenv('FAIL_PHASE', 'endpoint')
        else:
            monkeypatch.setenv('CDR_STILL_LOGS', 'csv')
        with pytest.raises(RuntimeError):
            d.deploy()
        assert d.log.read_text().rstrip().endswith('systemctl restart phonelet')
    output = capsys.readouterr()
    assert 'Summary:' not in output.out + output.err
    assert not any(url.endswith('/api/modem') for url in requests)
    assert all(secret not in output.out + output.err for secret in ('real-password', 'real-key', 'real-secret'))


def test_success_installs_asterisk_configs_with_private_modes(deployment):
    d = deployment
    d.deploy()
    for name in d.module.CONFIGS:
        assert (d.etc / name).stat().st_mode & 0o777 == 0o640, name
    assert d.helper.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('leftover', ['file', 'symlink'])
def test_deploy_replaces_stale_temporary_config_without_following_symlinks(deployment, tmp_path, leftover):
    d = deployment
    target = d.etc / 'extensions_custom.conf'
    temporary = target.with_name(target.name + '.phonelet-new')
    outside = tmp_path / 'outside-config-directory'
    outside.write_text('preserve synthetic external content\n')
    outside.chmod(0o600)
    if leftover == 'symlink':
        temporary.symlink_to(outside)
    else:
        temporary.write_text('stale partial configuration\n')

    d.deploy()

    assert target.read_bytes() == (d.stage / 'rendered' / target.name).read_bytes()
    assert target.read_text() != '; old dialplan\n'
    assert target.stat().st_mode & 0o777 == 0o640
    assert not target.is_symlink()
    assert not temporary.exists() and not temporary.is_symlink()
    assert outside.read_text() == 'preserve synthetic external content\n'
    assert outside.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('umask', [0, 0o077])
def test_temporary_configs_have_final_modes_before_content_is_written(deployment, monkeypatch, umask):
    d = deployment
    seen = set()
    open_file, copyfileobj = builtins.open, shutil.copyfileobj

    def check(path, mode):
        path = Path(path)
        if path.name.endswith('.phonelet-new'):
            expected = 0o755 if path == d.helper.with_name(d.helper.name + '.phonelet-new') else 0o640
            assert mode == expected
            seen.add(path)

    def open_path(path, *args, **kwargs):
        stream = open_file(path, *args, **kwargs)
        if str(path).endswith('.phonelet-new'):
            try:
                check(path, os.fstat(stream.fileno()).st_mode & 0o777)
            except BaseException:
                stream.close()
                raise
        return stream

    def copy_stream(source, target, *args, **kwargs):
        path = Path(os.readlink(f'/proc/self/fd/{target.fileno()}'))
        check(path, os.fstat(target.fileno()).st_mode & 0o777)
        return copyfileobj(source, target, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', open_path)
    monkeypatch.setattr(shutil, 'copyfileobj', copy_stream)
    previous = os.umask(umask)
    try:
        d.deploy()
    finally:
        os.umask(previous)
    assert len(seen) == len(d.module.CONFIGS) + 2  # configs, systemd drop-in and helper


@pytest.mark.parametrize('failure', ['owner', 'replace'])
def test_rollback_removes_uninstalled_temporary_config(deployment, monkeypatch, failure):
    d = deployment
    target = d.etc / 'extensions_custom.conf'
    temporary = target.with_name(target.name + '.phonelet-new')
    before = {p: p.read_bytes() for p in [*d.etc.iterdir(), d.dropin, d.helper]}
    operation = d.module.os.chown if failure == 'owner' else d.module.os.replace

    def fail(path, *args, **kwargs):
        if Path(path) == temporary:
            assert temporary.exists()
            raise OSError('synthetic install failure')
        return operation(path, *args, **kwargs)

    monkeypatch.setattr(d.module.os, 'chown' if failure == 'owner' else 'replace', fail)
    with pytest.raises(OSError, match='synthetic install failure'):
        d.deploy()
    assert {p: p.read_bytes() for p in before} == before
    assert not list(d.root.parent.rglob('*.phonelet-new'))


def test_rollback_restores_configs_atomically_with_saved_owner_and_mode(deployment, monkeypatch):
    d = deployment
    target = d.etc / 'extensions_custom.conf'
    target.chmod(0o604)
    d.helper.chmod(0o700)
    before = {p: p.read_bytes() for p in [*d.etc.iterdir(), d.dropin, d.helper]}
    owner = target.stat().st_uid, target.stat().st_gid
    chown = d.module.os.chown
    restored = {}

    def record_chown(path, uid, gid, *args, **kwargs):
        if Path(path).name.endswith('.phonelet-restore'):
            restored[Path(path).name.removesuffix('.phonelet-restore')] = (uid, gid)
        return chown(path, uid, gid, *args, **kwargs)

    monkeypatch.setattr(d.module.os, 'chown', record_chown)
    monkeypatch.setenv('FAIL_PHASE', 'endpoint')
    with pytest.raises(RuntimeError):
        d.deploy()
    assert {p: p.read_bytes() for p in before} == before
    assert target.stat().st_mode & 0o777 == 0o604
    assert d.helper.stat().st_mode & 0o777 == 0o700
    assert restored[target.name] == owner and restored[d.helper.name] == owner
    assert not (d.etc / 'cdr_custom.conf').exists()  # absent before: removed, not restored
    assert not [p for p in d.root.parent.rglob('*') if p.name.endswith(('.phonelet-new', '.phonelet-restore'))]
    assert d.log.read_text().rstrip().endswith('systemctl restart phonelet')


def test_saved_rollback_copies_keep_the_owner_of_their_targets(deployment, monkeypatch):
    d = deployment
    chown = d.module.os.chown
    saved = {}

    def record_chown(path, uid, gid, *args, **kwargs):
        if Path(path).parent == d.stage / 'rollback':
            saved[Path(path).name] = (uid, gid)
        return chown(path, uid, gid, *args, **kwargs)

    monkeypatch.setattr(d.module.os, 'chown', record_chown)
    targets = [d.etc / name for name in d.module.CONFIGS] + [d.dropin, d.helper]
    expected = {str(i): (t.stat().st_uid, t.stat().st_gid) for i, t in enumerate(targets) if t.exists()}
    d.deploy()
    assert saved == expected and len(expected) == len(targets) - 1  # cdr_custom.conf was absent


def test_failed_atomic_restore_reports_rollback_incomplete_without_temporary(deployment, monkeypatch):
    d = deployment
    target = d.etc / 'extensions_custom.conf'
    replace = d.module.os.replace

    def fail_restore(source, destination):
        if Path(source) == target.with_name(target.name + '.phonelet-restore'):
            raise OSError('synthetic restore failure')
        return replace(source, destination)

    monkeypatch.setattr(d.module.os, 'replace', fail_restore)
    monkeypatch.setenv('FAIL_PHASE', 'endpoint')
    with pytest.raises(RuntimeError, match='ROLLBACK INCOMPLETE: restore ' + re.escape(str(target))):
        d.deploy()
    assert not [p for p in d.root.parent.rglob('*') if p.name.endswith(('.phonelet-new', '.phonelet-restore'))]
    assert 'systemctl restart phonelet' not in d.log.read_text().split('systemctl stop phonelet')[-1]


def test_failed_temporary_cleanup_still_starts_restored_application(deployment, monkeypatch, capsys):
    d = deployment
    target = d.etc / 'extensions_custom.conf'
    temporary = target.with_name(target.name + '.phonelet-new')
    before = {p: p.read_bytes() for p in [*d.etc.iterdir(), d.dropin, d.helper]}
    replace, unlink = d.module.os.replace, Path.unlink

    def fail_replace(source, destination):
        if Path(source) == temporary:
            raise OSError('synthetic install failure')
        return replace(source, destination)

    def fail_cleanup(path, *args, **kwargs):
        if path == temporary and path.exists():
            raise PermissionError('synthetic cleanup failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(d.module.os, 'replace', fail_replace)
    monkeypatch.setattr(Path, 'unlink', fail_cleanup)
    with pytest.raises(RuntimeError, match='ROLLBACK INCOMPLETE: remove ' + re.escape(str(temporary))):
        d.deploy()
    assert {p: p.read_bytes() for p in before} == before
    assert d.log.read_text().rstrip().endswith('systemctl restart phonelet')
    assert temporary.exists()
    output = capsys.readouterr().out
    assert 'temporary file remains' in output and str(temporary) in output


def test_busy_deploy_lock_reports_another_deployment(deployment, monkeypatch):
    d = deployment
    lock_path = d.root / '.deploy.lock'
    monkeypatch.setattr(sys, 'argv', ['deploy-release.py', '--stage', str(d.stage), '--root', str(d.root)])
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match='another deployment') as error:
            runpy.run_path(str(ROOT / 'scripts/deploy-release.py'), run_name='__main__')
    assert str(lock_path) in str(error.value)
    assert not d.log.exists() and not (d.stage / 'rollback').exists()


@pytest.mark.parametrize('descriptor', ['inherited', 'other-file', 'not-a-number'])
def test_deploy_locks_the_descriptor_install_sh_holds(deployment, monkeypatch, descriptor):
    """install.sh keeps the lock from the Asterisk upgrade on: deploy locks its inherited descriptor."""
    d = deployment
    lock_path = d.root / '.deploy.lock'
    monkeypatch.setattr(sys, 'argv', ['deploy-release.py', '--stage', str(d.stage), '--root', str(d.root)])
    seen = []
    monkeypatch.setattr(d.module, 'deploy', lambda *args, **kwargs: seen.append(args))
    with lock_path.open('a') as held, (d.root / 'other').open('a') as other:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # install.sh's lock
        source = held if descriptor == 'inherited' else other
        monkeypatch.setenv('PHONELET_DEPLOY_LOCK_FD', 'x' if descriptor == 'not-a-number' else str(os.dup(source.fileno())))
        # The script's main block, run in the loaded module so that deploy() is the stub above.
        code = (ROOT / 'scripts/deploy-release.py').read_text()
        main = code[code.index("if __name__ == '__main__':"):].replace("if __name__ == '__main__':", 'if True:', 1)
        if descriptor == 'inherited':
            exec(compile(main, 'deploy-release.py', 'exec'), vars(d.module))
            assert len(seen) == 1
            with open(lock_path, 'a') as intruder:  # still held by install.sh
                with pytest.raises(BlockingIOError):
                    fcntl.flock(intruder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            with pytest.raises(SystemExit, match='PHONELET_DEPLOY_LOCK_FD'):
                exec(compile(main, 'deploy-release.py', 'exec'), vars(d.module))
            assert seen == []


@pytest.mark.parametrize('check', [False, True])
def test_unknown_asterisk_names_are_rejected_without_values(deployment, capsys, check):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nAST_SIP_PASWORD=synthetic-secret-1\nAST_ZTYPO=synthetic-secret-2\n')
    with pytest.raises(ValueError, match='Unknown AST_') as error:
        d.deploy(check=check)
    assert 'AST_SIP_PASWORD' in str(error.value) and 'AST_ZTYPO' in str(error.value)
    output = str(error.value) + capsys.readouterr().out
    assert 'synthetic-secret' not in output and 'real-secret' not in output and 'real-password' not in output
    assert not d.log.exists() and not (d.stage / 'rollback').exists()


def test_known_asterisk_names_match_templates_and_environment_example():
    module = script('deploy-release')
    # Include every template, even one not yet added to CONFIGS; targets must introduce no private names.
    placeholders = {name for path in (ROOT / 'asterisk').glob('*.tpl')
                    for name in re.findall(r'\$\{(AST_[A-Z0-9_]+)\}', path.read_text())}
    target_placeholders = {name for path in (ROOT / 'targets').rglob('*') if path.is_file()
                           for name in re.findall(r'\$\{(AST_[A-Z0-9_]+)\}', path.read_text())}
    example = set(re.findall(r'^#?(AST_[A-Z0-9_]+)=', (ROOT / '.env.example').read_text(), re.M))
    assert module.known_asterisk_variables() == placeholders == example | module.DERIVED_KEYS.keys()
    assert not (example & module.DERIVED_KEYS.keys())
    assert target_placeholders <= placeholders


@pytest.mark.parametrize('check', [False, True])
def test_ami_port_mismatch_is_rejected_before_preflight(deployment, check):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nPHONELET_AMI_PORT=15038\n')
    with pytest.raises(ValueError, match='PHONELET_AMI_PORT=5038'):
        d.deploy(check=check)
    assert not d.log.exists() and not (d.stage / 'rollback').exists()


ENV_SECRET = 'synthetic-secret-7f3a9c'


@pytest.mark.parametrize('line', ['AST_SIP_PASSWORD="' + ENV_SECRET, "AST_SIP_PASSWORD='" + ENV_SECRET,
                                  '=' + ENV_SECRET, 'FOO BAR=' + ENV_SECRET])
def test_unparsable_env_line_stops_with_its_number_only(deployment, capsys, caplog, line):
    d = deployment
    env_file = d.root / '.env'
    count = len(env_file.read_text().splitlines())
    with env_file.open('a') as stream:
        stream.write('\n# synthetic comment\n\n\n' + line + '\nAST_RECORD_CALLS=no\n')
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError, match=r'^\.env line ' + str(count + 4) + ' cannot be parsed$') as caught:
        d.module.configuration(env_file)
    output = capsys.readouterr()
    for text in (str(caught.value), output.out, output.err, caplog.text):
        assert ENV_SECRET not in text and 'FOO' not in text and 'AST_SIP_PASSWORD' not in text


def test_env_key_without_value_is_still_rejected(deployment):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nFOO\n')
    with pytest.raises(ValueError, match='entry without a value: FOO'):
        d.module.configuration(d.root / '.env')


@pytest.mark.parametrize('port_line', ['port = 5038 ; synthetic comment\n', ''])
def test_manager_port_accepts_comments_and_reports_missing_key(deployment, monkeypatch, port_line):
    d = deployment
    monkeypatch.setattr(d.module, 'ASTERISK_DIR', d.stage / 'asterisk')
    (d.stage / 'asterisk/manager.conf').write_text('[general]\nenabled = yes\n' + port_line)
    if port_line:
        _, settings = d.module.configuration(d.root / '.env')
        assert settings.ami_port == 5038
    else:
        with pytest.raises(ValueError, match='AMI port.*asterisk/manager.conf'):
            d.module.configuration(d.root / '.env')


@pytest.mark.parametrize('check', [False, True])
@pytest.mark.parametrize('revision', ['0123456789ab', '0123456789ab-dirty', None])
def test_deploy_and_check_print_release_revision_first(deployment, capsys, check, revision):
    d = deployment
    if revision is not None:
        (d.stage / 'REVISION').write_text(revision + '\n')
    d.deploy(check=check)
    assert capsys.readouterr().out.startswith('Release revision: ' + (revision or 'unknown') + '\n')


@pytest.mark.parametrize('check', [False, True])
def test_short_sip_password_is_rejected_without_disclosing_it(deployment, capsys, check):
    d = deployment
    password = 'synthetic11'
    env_file = d.root / '.env'
    env_file.write_text(env_file.read_text().replace('AST_SIP_PASSWORD=real-password',
                                                   'AST_SIP_PASSWORD=' + password))
    with pytest.raises(ValueError, match='AST_SIP_PASSWORD.*at least 12') as error:
        d.deploy(check=check)
    assert password not in str(error.value) + capsys.readouterr().out
    assert not d.log.exists() and not (d.stage / 'rollback').exists()


def test_twelve_character_sip_password_is_accepted(deployment):
    env_file = deployment.root / '.env'
    env_file.write_text(env_file.read_text().replace('AST_SIP_PASSWORD=real-password',
                                                   'AST_SIP_PASSWORD=synthetic123'))
    deployment.deploy(check=True)


@pytest.mark.parametrize('failure', ['placeholder', 'mismatch', 'transport', 'disk'])
def test_preflight_failure_changes_no_running_files(deployment, monkeypatch, failure):
    d = deployment
    if failure == 'placeholder':
        with (d.root / '.env').open('a') as stream: stream.write('\nPHONELET_API_KEY=GENERATE_STRONG_KEY_HERE\n')
    if failure == 'mismatch':
        with (d.root / '.env').open('a') as stream: stream.write('\nAST_AMI_SECRET=different\n')
    if failure == 'transport':
        target = d.etc / 'pjsip_custom.conf'
        target.write_text(target.read_text().replace(':5060', ':5090'))
    if failure == 'disk':
        monkeypatch.setattr(d.module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises((ValueError, RuntimeError)):
        d.deploy()
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert not (d.stage / 'rollback').exists()
    assert not d.log.exists() or 'systemctl stop' not in d.log.read_text()


@pytest.mark.parametrize('check', [False, True])
def test_unfinished_wifi_change_refuses_deploy_before_any_change(deployment, check):
    d = deployment
    d.wifi_state.mkdir()
    marker = d.wifi_state / 'pending.json'
    marker.write_text('{"existed": true}')
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(RuntimeError, match='Unfinished Wi-Fi change') as error:
        d.deploy(check=check)
    assert str(marker) in str(error.value) and 'Manual Wi-Fi rollback' in str(error.value)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.dropin.read_text() == '[Service]\nExecStart=/old/.venv/bin/python old-app\n'
    assert marker.read_text() == '{"existed": true}'
    assert not (d.stage / 'rollback').exists() and not (d.stage / 'runtime.env').exists()
    assert not d.log.exists()
    marker.unlink()
    d.deploy(check=check)


def test_wifi_change_started_after_preflight_rolls_back_and_restarts_previous_app(deployment, monkeypatch):
    d = deployment
    marker = d.wifi_state / 'pending.json'
    stop = d.module.stop_application

    def stop_during_a_change():
        stop()
        d.wifi_state.mkdir(exist_ok=True)
        marker.write_text('{"existed": true}')

    monkeypatch.setattr(d.module, 'stop_application', stop_during_a_change)
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(RuntimeError, match='Unfinished Wi-Fi change') as error:
        d.deploy()
    assert 'ROLLBACK INCOMPLETE' not in str(error.value)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.dropin.read_text() == '[Service]\nExecStart=/old/.venv/bin/python old-app\n'
    assert marker.read_text() == '{"existed": true}'  # the previous app finishes the recovery
    log = d.log.read_text()
    assert 'dialplan reload' not in log.split('systemctl stop phonelet')[1]  # no config was installed
    assert log.rstrip().endswith('systemctl restart phonelet')


@pytest.mark.parametrize('check', [False, True])
def test_relative_wifi_state_dir_is_refused_before_any_change(deployment, check):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nPHONELET_WIFI_STATE_DIR=wifi\n')
    with pytest.raises(ValueError, match='PHONELET_WIFI_STATE_DIR must be an absolute path'):
        d.deploy(check=check)
    assert not (d.stage / 'rollback').exists() and not d.log.exists()


@pytest.mark.parametrize('mode', [{}, {'check': True}, {'pre_upgrade': True}])
def test_symlink_target_is_refused_before_any_snapshot(deployment, tmp_path, mode):
    # The pre-upgrade check refuses it too, before install.sh replaces Asterisk.
    d = deployment
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.write_text('not a helper')
    d.helper.unlink()
    d.helper.symlink_to(elsewhere)
    with pytest.raises(RuntimeError, match='Refusing symlink deployment target: ' + re.escape(str(d.helper))):
        d.deploy(**mode)
    assert elsewhere.read_text() == 'not a helper'
    assert not (d.stage / 'rollback').exists()
    assert not d.log.exists() or 'systemctl stop' not in d.log.read_text()


@pytest.mark.parametrize('check', [False, True])
@pytest.mark.parametrize('kind,application,module_name', [
    ('application', 'TryExec', 'app_exec'), ('application', 'Originate', 'app_originate'),
    ('function', 'REGEX', 'func_strings')])
def test_missing_sms_application_stops_deploy_before_any_change(deployment, monkeypatch, check, kind, application,
                                                                module_name):
    # Without TryExec or REGEX no SMS is sent; without Originate a rejected SMS raises no alert.
    d = deployment
    monkeypatch.setenv('MISSING_APP', application)
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    old_unit = d.dropin.read_bytes()
    with pytest.raises(RuntimeError, match=f'^Required {module_name} module not loaded: no {application} {kind}$'):
        d.deploy(check=check)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.dropin.read_bytes() == old_unit and d.helper.read_text() == 'old helper'
    assert not (d.stage / 'rollback').exists()
    log = d.log.read_text()
    assert f'core show {kind} {application}' in log
    assert 'systemctl stop' not in log and 'reload' not in log and 'restart' not in log


@pytest.mark.parametrize('check', [False, True])
@pytest.mark.parametrize('line,message', [
    ('AST_DONGLE_URC=/dev/dongle0-at', 'Unknown AST_ variables: AST_DONGLE_URC'),
    ('AST_DONGLE_URC=/dev/null', 'Unknown AST_ variables: AST_DONGLE_URC'),
    ('AST_DONGLE_DATA=/dev/dongle0-modem', r'is the modem \(MDM\) port.*set AST_DONGLE_DATA=/dev/dongle0-at'),
    ('AST_DONGLE_DATA=/dev//dongle0-modem', r'AST_DONGLE_DATA is the modem \(MDM\) port.*set AST_DONGLE_DATA=/dev/dongle0-at'),
    ('AST_DONGLE_DATA=/dev/./dongle0-modem', r'AST_DONGLE_DATA is the modem \(MDM\) port.*set AST_DONGLE_DATA=/dev/dongle0-at'),
    ('AST_DONGLE_DATA=//dev/dongle0-modem', r'is the modem \(MDM\) port.*set AST_DONGLE_DATA=/dev/dongle0-at')])
def test_invalid_port_environment_stops_deploy_before_changes(deployment, capsys, check, line, message):
    # Reject unsupported keys and the modem (MDM) port before rendering data=.
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\n' + line + '\n')
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(ValueError, match=message):
        d.deploy(check=check)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert not d.log.exists() and not (d.stage / 'rollback').exists()
    assert 'Check passed' not in capsys.readouterr().out


def test_default_data_port_is_the_pcui_link(deployment):
    d = deployment
    env = (d.root / '.env').read_text()
    (d.root / '.env').write_text(env.replace('AST_DONGLE_DATA=/dev/null\n', ''))
    original = Path.is_char_device
    seen = []
    def is_char_device(path):
        seen.append(str(path))
        return True if str(path) == '/dev/dongle0-at' else original(path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, 'is_char_device', is_char_device)
        d.deploy()
    assert seen == ['/dev/dongle0-at', '/dev/null']  # data, audio; no third port
    conf = (d.etc / 'dongle.conf').read_text()
    assert 'data=/dev/dongle0-at\n' in conf and not re.search(r'^\s*urc\s*=', conf, re.M)


def fake_devices(monkeypatch, devices):
    """os.stat of each path in devices returns the stat of the real device it maps to."""
    real = os.stat
    monkeypatch.setattr(os, 'stat', lambda path, *a, **kw: real(devices.get(os.fspath(path), path), *a, **kw))


@pytest.mark.parametrize('check', [False, True])
def test_modem_mdm_device_number_is_refused_before_changes(deployment, monkeypatch, capsys, check):
    # Another name for the modem (MDM) device (a second link, its ttyUSB path) passes the string checks.
    d = deployment
    (d.root / '.env').write_text((d.root / '.env').read_text().replace(
        'AST_DONGLE_DATA=/dev/null', 'AST_DONGLE_DATA=/dev/ttyUSB7'))
    fake_devices(monkeypatch, {'/dev/ttyUSB7': '/dev/null', '/dev/dongle0-modem': '/dev/null'})
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(RuntimeError, match=r'is the modem \(MDM\) port /dev/dongle0-modem.*set AST_DONGLE_DATA=/dev/dongle0-at'):
        d.deploy(check=check)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert not (d.stage / 'rollback').exists()
    assert not d.log.exists() or 'systemctl stop' not in d.log.read_text()
    assert 'Check passed' not in capsys.readouterr().out


def test_pcui_data_port_beside_an_existing_mdm_link_is_accepted(deployment, monkeypatch):
    d = deployment
    (d.root / '.env').write_text((d.root / '.env').read_text().replace(
        'AST_DONGLE_DATA=/dev/null', 'AST_DONGLE_DATA=/dev/dongle0-at'))
    fake_devices(monkeypatch, {'/dev/dongle0-at': '/dev/zero', '/dev/dongle0-modem': '/dev/null'})
    d.deploy()
    assert 'data=/dev/dongle0-at\n' in (d.etc / 'dongle.conf').read_text()


OTHER_DONGLE_CONF = '[general]\n\n[dongle0]\ndata=/dev/dongle0-modem\naudio=/dev/dongle0-audio\ncontext=dongle-incoming\n'


@pytest.mark.parametrize('fail', [False, True])
def test_deploy_over_a_different_dongle_conf_and_its_rollback(deployment, monkeypatch, fail):
    # A deploy replaces a live dongle.conf that differs; a failed deploy restores it unchanged.
    d = deployment
    (d.etc / 'dongle.conf').write_text(OTHER_DONGLE_CONF)
    if fail:
        monkeypatch.setenv('FAIL_PHASE', 'endpoint')
        with pytest.raises(RuntimeError):
            d.deploy()
        assert (d.etc / 'dongle.conf').read_text() == OTHER_DONGLE_CONF
        return
    d.deploy()
    conf = (d.etc / 'dongle.conf').read_text()
    assert 'data=/dev/null\n' in conf and 'data=/dev/dongle0-modem' not in conf


def test_transport_change_restarts_asterisk_with_explicit_option(deployment):
    target = deployment.etc / 'pjsip_custom.conf'
    target.write_text(target.read_text().replace(':5060', ':5090'))
    deployment.deploy(allow_restart=True)
    assert 'systemctl restart asterisk' in deployment.log.read_text()


def test_changed_sip_bind_is_a_transport_change(deployment):
    # pjsip cannot rebind a running transport: a new AST_SIP_BIND needs a restart, never a reload.
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nAST_SIP_BIND=127.0.0.1:5060\n')
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(RuntimeError, match='--allow-asterisk-restart'):
        d.deploy()
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    d.deploy(allow_restart=True)
    assert 'systemctl restart asterisk' in d.log.read_text()
    assert 'bind=127.0.0.1:5060\n' in (d.etc / 'pjsip_custom.conf').read_text()


@pytest.mark.parametrize('check', [False, True])
def test_sip_bind_to_an_address_not_on_the_host_is_refused_before_any_change(deployment, check):
    # pjsip would leave the transport unbound and Asterisk running: no SIP until a manual restart.
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nAST_SIP_BIND=192.0.2.10:5060\n')  # TEST-NET-1, never a local address
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(RuntimeError, match='AST_SIP_BIND address 192.0.2.10 is not on this host'):
        d.deploy(allow_restart=True, check=check)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert not d.log.exists()  # no command ran


@pytest.mark.parametrize('check', [False, True])
def test_csv_held_by_an_active_cdr_asks_for_a_restart_before_any_change(deployment, monkeypatch, check):
    # After an outgoing SMS Message/ast_msg_queue keeps a CDR active until Asterisk stops.
    d = deployment
    monkeypatch.setenv('CDR_ACTIVE', '1')
    monkeypatch.setenv('CDR_STILL_LOGS', 'csv')
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(RuntimeError, match='deploy with --restart-asterisk'):
        d.deploy(check=check)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert 'systemctl stop' not in d.log.read_text()


def test_forced_restart_restarts_asterisk_without_a_transport_change(deployment, monkeypatch):
    # cdr_csv stays registered while Message/ast_msg_queue holds a CDR: only a restart drops it.
    monkeypatch.setenv('CDR_ACTIVE', '1')
    monkeypatch.setenv('CDR_CSV', 'Running')
    monkeypatch.setenv('RUNNING_MODULES', 'chan_unistim.so')
    deployment.deploy(force_restart=True)
    log = deployment.log.read_text()
    assert 'systemctl restart asterisk' in log
    # A restart applies cdr.conf and modules.conf: no reloads or unloads; the CDR check still runs.
    assert 'module reload' not in log and 'module unload' not in log
    assert log.index('systemctl restart asterisk') < log.rindex('cdr show status')
    assert_start_limit_reset_before_each_restart(log)
    # Once more after the verified deploy, so the next one has the full start limit.
    assert log.rindex('systemctl reset-failed asterisk') > log.rindex('cdr show status')


def assert_start_limit_reset_before_each_restart(log):
    lines = log.splitlines()
    restarts = [i for i, line in enumerate(lines) if line == 'systemctl restart asterisk']
    assert restarts and all(lines[i - 1] == 'systemctl reset-failed asterisk' for i in restarts)


def test_csv_left_after_a_forced_restart_rolls_back_with_another_restart(deployment, monkeypatch):
    d = deployment
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    monkeypatch.setenv('CDR_STILL_LOGS', 'csv')
    with pytest.raises(RuntimeError, match='still logs CDR'):
        d.deploy(force_restart=True)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    log = d.log.read_text()
    assert log.count('systemctl restart asterisk') == 2 and 'module unload' not in log
    assert log.rstrip().endswith('systemctl restart phonelet')
    assert_start_limit_reset_before_each_restart(log)


def test_ssh_failure_is_nonzero_and_archive_contains_no_secrets(tmp_path):
    commands = tmp_path / 'bin'
    log = tmp_path / 'archive-list'
    executable(commands / 'ssh', '''if [[ "$*" == *mktemp* ]]; then
 echo /root/phonelet-releases/release.TEST123
else
 exit 73
fi
''')
    executable(commands / 'scp', 'tar -tzf "$2" > "$ARCHIVE_LIST"\n')
    result = subprocess.run(['bash', str(ROOT / 'deploy.sh'), 'fake-host'],
                            env=os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'], 'ARCHIVE_LIST': str(log)}, capture_output=True)
    assert result.returncode == 73
    entries = log.read_text().splitlines()
    assert 'requirements.lock' in entries
    assert 'scripts/deploy-release.py' in entries
    assert '.env' not in entries
    assert not any(name.startswith('chan_dongle_patch/') for name in entries)


def test_deploy_requires_a_host_before_ssh(tmp_path):
    commands, log = tmp_path / 'bin', tmp_path / 'ssh.log'
    executable(commands / 'ssh', 'touch "$SSH_LOG"; exit 99\n')
    result = subprocess.run(['bash', str(ROOT / 'deploy.sh')], capture_output=True, text=True,
                            env=os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'], 'SSH_LOG': str(log)})
    assert result.returncode == 2
    assert 'Usage: deploy.sh HOST' in result.stderr
    assert not log.exists()


@pytest.fixture
def staged_deploy_script(tmp_path):
    """Execute remote shell commands locally, with every host path mapped into tmp_path."""
    checkout, commands, stage = [tmp_path / name for name in ('checkout', 'bin', 'release.TEST123')]
    checkout.mkdir()
    stage.mkdir()
    shutil.copyfile(ROOT / 'deploy.sh', checkout / 'deploy.sh')
    for name in ('src', 'asterisk', 'scripts'):
        (checkout / name).mkdir()
    shutil.copyfile(ROOT / 'scripts/release-common.sh', checkout / 'scripts/release-common.sh')
    for name in ('pyproject.toml', 'requirements.lock'):
        (checkout / name).touch()
    env_file = tmp_path / 'host.env'
    env_file.write_text('# synthetic host configuration\n')
    ssh_log, tar_log = tmp_path / 'ssh.log', tmp_path / 'tar.log'
    executable(commands / 'ssh', r'''echo "$*" >> "$SSH_LOG"
if [[ "$2" == *mktemp* ]]; then
 echo /root/phonelet-releases/release.TEST123
else
 remote_command=${2//\/root\/phonelet-releases\/release.TEST123/$FAKE_STAGE}
 remote_command=${remote_command//\/root\/phonelet\/.env/$FAKE_ENV}
 bash -c "$remote_command"
fi
''')
    executable(commands / 'scp', 'cp "$2" "$FAKE_STAGE/release.tar.gz"\n')
    executable(commands / 'tar', 'echo "$*" >> "$TAR_LOG"\nexec ' + shutil.which('tar') + ' "$@"\n')
    executable(commands / 'python3', '''[[ "$*" == '-m venv .venv' ]]
mkdir -p .venv/bin
printf '#!/bin/bash\\nexit 0\\n' > .venv/bin/python
chmod 755 .venv/bin/python
''')

    def run():
        return subprocess.run(['bash', str(checkout / 'deploy.sh'), 'fake-host'], capture_output=True, text=True,
                              env=os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'],
                                                'SSH_LOG': str(ssh_log), 'TAR_LOG': str(tar_log),
                                                'FAKE_STAGE': str(stage), 'FAKE_ENV': str(env_file)})

    return SimpleNamespace(run=run, checkout=checkout, stage=stage, env_file=env_file,
                           commands=commands, ssh_log=ssh_log, tar_log=tar_log)


def test_deploy_missing_host_environment_points_to_install(staged_deploy_script):
    d = staged_deploy_script
    d.env_file.unlink()
    result = d.run()
    assert result.returncode == 1
    assert '.env' in result.stderr and 'run install.sh first' in result.stderr and 'docs/install.md' in result.stderr
    assert not (d.stage / '.venv').exists()


def test_deploy_extracts_release_without_archive_ownership(staged_deploy_script):
    d = staged_deploy_script
    result = d.run()
    assert result.returncode == 0, result.stderr
    extraction = next(line for line in d.tar_log.read_text().splitlines() if 'release.tar.gz' in line)
    assert '--no-same-owner' in extraction.split()


@pytest.mark.parametrize('state', ['clean', 'tracked', 'untracked', 'no-git'])
def test_deploy_writes_revision_for_clean_dirty_and_non_git_checkout(staged_deploy_script, state):
    d = staged_deploy_script
    expected = 'unknown'
    if state != 'no-git':
        def git(*args):
            return subprocess.run(['git', '-C', str(d.checkout), *args], check=True, capture_output=True,
                                  text=True).stdout.strip()
        git('init', '-q')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Synthetic release')
        expected = git('rev-parse', '--short=12', 'HEAD')
        if state == 'tracked':
            with (d.checkout / 'deploy.sh').open('a') as stream:
                stream.write('\n# synthetic uncommitted change\n')
        elif state == 'untracked':
            (d.checkout / 'untracked').touch()
        if state != 'clean':
            expected += '-dirty'
    result = d.run()
    assert result.returncode == 0, result.stderr
    assert (d.stage / 'REVISION').read_text() == expected + '\n'
    assert not (d.checkout / 'REVISION').exists()


def test_deploy_marks_revision_dirty_when_git_status_fails(staged_deploy_script):
    d = staged_deploy_script
    executable(d.commands / 'git', '''case "$*" in
 'rev-parse --show-toplevel') pwd -P ;;
 'rev-parse --short=12 HEAD') echo 0123456789ab ;;
 'status --porcelain --untracked-files=normal') exit 128 ;;
 *) exit 2 ;;
esac
''')
    result = d.run()
    assert result.returncode == 0, result.stderr
    assert (d.stage / 'REVISION').read_text() == '0123456789ab-dirty\n'


def test_deploy_rejects_unsafe_revision_before_ssh(staged_deploy_script):
    d = staged_deploy_script
    executable(d.commands / 'git', '''if [[ "$*" == 'rev-parse --show-toplevel' ]]; then
 pwd -P
elif [[ "$1" == rev-parse ]]; then
 printf '%s\\n' "bad'; echo injected; #"
fi
''')
    result = d.run()
    assert result.returncode != 0
    assert 'revision' in result.stderr.lower()
    assert not d.ssh_log.exists()


class AmiSocket:
    """A blocking AMI peer that hands its answers out in small chunks."""
    def __init__(self, data, chunk=1):
        self.data, self.chunk, self.sent = data, chunk, []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, _): pass
    def sendall(self, data): self.sent.append(data)
    def recv(self, _):
        chunk, self.data = self.data[:self.chunk], self.data[self.chunk:]
        return chunk


def device_list(device='dongle0', version='1', journal='ok'):
    return ('Response: Success\r\nEventList: start\r\n\r\n'
            f'Event: DongleDeviceEntry\r\nDevice: {device}\r\n' +
            (f'SMSJournalVersion: {version}\r\n' if version else '') +
            (f'SMSJournalStatus: {journal}\r\n' if journal else '') +
            '\r\nEvent: DongleShowDevicesComplete\r\nEventList: Complete\r\n\r\n')


@pytest.mark.parametrize('login,journal,device,version', [('Success', 'ok', 'dongle0', '1'), ('Error', 'ok', 'dongle0', '1'),
                                                ('Success', None, 'dongle0', '1'), ('Success', 'error', 'dongle0', '1'),
                                                ('Success', 'ok', 'other', '1'), ('Success', 'ok', 'dongle0', None),
                                                ('Success', 'ok', 'dongle0', '2')])
def test_ami_smoke_requires_authentication_and_durable_driver(monkeypatch, login, journal, device, version):
    module = script('deploy-release')
    peer = AmiSocket((f'Asterisk Call Manager/9.0\r\nResponse: {login}\r\n\r\n' +
                      device_list(device, version, journal)).encode())
    monkeypatch.setattr(module.socket, 'create_connection', lambda *a, **kw: peer)
    settings = module.Settings(ami_secret='real-secret', api_key='real-key')
    if login == 'Success' and journal == 'ok' and device == 'dongle0' and version == '1':
        module.require_durable_sms_driver(settings)
        assert peer.sent[0].startswith(b'Action: Login\r\n') and b'Events: off\r\n' in peer.sent[0]
    else:
        with pytest.raises(RuntimeError):
            module.require_durable_sms_driver(settings)


@pytest.mark.parametrize('answer,chunk', [
    ('Response: Error\r\nMessage: Device not found\r\n\r\n' + device_list(), 1),  # the list is not read
    ('Response: Success\r\nMessage: ' + 'x' * 70000, 4096),  # oversized, never ends
])
def test_ami_smoke_refuses_an_error_answer_and_an_oversized_message(monkeypatch, answer, chunk):
    module = script('deploy-release')
    peer = AmiSocket(('Asterisk Call Manager/9.0\r\nResponse: Success\r\n\r\n' + answer).encode(), chunk)
    monkeypatch.setattr(module.socket, 'create_connection', lambda *a, **kw: peer)
    with pytest.raises(RuntimeError) as caught:
        module.require_durable_sms_driver(module.Settings(ami_secret='real-secret', api_key='real-key'))
    assert 'real-secret' not in str(caught.value)
    if chunk > 1:
        assert 'too large' in str(caught.value)


def test_same_version_rollback_keeps_history_and_outbox_acknowledgements(tmp_path):
    module = script('deploy-release')
    database, original = tmp_path / 'live.db', tmp_path / 'before.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE sms (id INTEGER PRIMARY KEY, text TEXT NOT NULL)')
        db.execute('CREATE TABLE outbox (event_key TEXT, backend TEXT, status TEXT, payload TEXT)')
        db.execute('PRAGMA user_version=2')
    module.snapshot_database(database, original)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO sms VALUES (1, 'accepted during candidate')")
        jobs = [('sms:1', 'homeassistant', 'delivered', '{}'), ('sms:1', 'telegram', 'pending', '{"next_chunk": 1}')]
        db.executemany('INSERT INTO outbox VALUES (?,?,?,?)', jobs)
    module.prepare_database_for_rollback(database, original)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT text FROM sms').fetchall() == [('accepted during candidate',)]
        assert db.execute('SELECT * FROM outbox').fetchall() == jobs


@pytest.mark.parametrize('old_version, new_version', [(0, 1), (1, 2), (2, 1)])
def test_rollback_across_schema_versions_is_refused(tmp_path, old_version, new_version):
    # No backward compatibility: not even the former pre-outbox (0) to outbox (1) upgrade.
    module = script('deploy-release')
    database, original = tmp_path / 'live.db', tmp_path / 'before.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE sms (id INTEGER PRIMARY KEY, text TEXT NOT NULL, notified INTEGER DEFAULT 0)')
        db.execute('CREATE TABLE calls (id INTEGER PRIMARY KEY, caller TEXT NOT NULL, notified INTEGER DEFAULT 0)')
        db.execute(f'PRAGMA user_version={old_version}')
    module.snapshot_database(database, original)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE outbox (event_key TEXT, backend TEXT, status TEXT, payload TEXT)')
        db.execute("INSERT INTO sms VALUES (1, 'accepted during candidate', 0)")
        db.execute(f'PRAGMA user_version={new_version}')
    with pytest.raises(RuntimeError, match='requires a compatible application'):
        module.prepare_database_for_rollback(database, original)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT id, notified FROM sms').fetchall() == [(1, 0)]


def test_incompatible_schema_keeps_current_data_and_stops_old_app(deployment, monkeypatch):
    d = deployment
    def smoke(*args):
        with sqlite3.connect(d.db) as db:
            db.execute('PRAGMA user_version=99')
        raise RuntimeError('unready')
    monkeypatch.setattr(d.module, 'smoke', smoke)
    with pytest.raises(RuntimeError, match='ROLLBACK INCOMPLETE'):
        d.deploy()
    with sqlite3.connect(d.db) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (99,)
    assert d.log.read_text().count('systemctl restart phonelet') == 1


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_interrupted_mutation_rolls_back_and_restores_signal_handlers(deployment, signum):
    d = deployment
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    old_unit = d.dropin.read_bytes()
    context = multiprocessing.get_context('fork')
    results = context.Queue()

    def worker():
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        replace = d.module.os.replace
        interrupted = False

        def replace_then_interrupt(source, target):
            nonlocal interrupted
            replace(source, target)
            if Path(target).parent == d.etc and not interrupted:
                interrupted = True
                os.kill(os.getpid(), signum)

        d.module.os.replace = replace_then_interrupt
        original_copy = d.module.shutil.copy2

        def copy_and_repeat_cancel(source, target):
            if interrupted:
                # Repeated cancellation must not interrupt rollback itself.
                os.kill(os.getpid(), signum)
            return original_copy(source, target)

        d.module.shutil.copy2 = copy_and_repeat_cancel
        try:
            d.deploy()
        except d.module.DeploymentInterrupted:
            results.put({s: signal.getsignal(s) for s in previous} == previous)
        else:
            results.put(False)

    process = context.Process(target=worker)
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail('Interrupted deployment did not finish recovery')
    assert process.exitcode == 0
    assert results.get(timeout=1) is True
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.dropin.read_bytes() == old_unit
    assert d.helper.read_text() == 'old helper'
    assert d.log.read_text().rstrip().endswith('systemctl restart phonelet')


def test_deployment_preserves_empty_sip_destination(deployment):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nPHONELET_SIP_MESSAGE_DESTINATION=\n')
    d.deploy()
    assert 'PHONELET_SIP_MESSAGE_DESTINATION=""\n' in (d.stage / 'runtime.env').read_text()
    assert 'MessageSend' not in d.log.read_text()


def test_validated_environment_is_canonical_for_systemd(deployment):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        stream.write('\nPHONELET_API_KEY=checked-key # human comment\n')
    d.deploy()
    content = (d.stage / 'runtime.env').read_text()
    assert 'PHONELET_API_KEY="checked-key"\n' in content
    assert '# human comment' not in content
    assert (d.stage / 'runtime.env').stat().st_mode & 0o777 == 0o600
    settings = d.module.Settings(ami_secret='abc\\def"ghi', api_key='secret$literal')
    result = d.module.write_runtime_environment(d.stage, settings).read_text()
    assert 'PHONELET_AMI_SECRET="abc\\\\def\\"ghi"\n' in result
    assert 'PHONELET_API_KEY="secret$literal"\n' in result


def test_root_release_rejects_nonroot_service_before_mutation(deployment, monkeypatch):
    d = deployment
    original_run = d.module.run
    def run(*args):
        if args == ('systemctl', 'show', '--property=User', '--value', 'phonelet'):
            return 'asterisk\n'
        return original_run(*args)
    monkeypatch.setattr(d.module, 'run', run)
    with pytest.raises(RuntimeError, match='root Phonelet service'):
        d.deploy()
    assert not (d.stage / 'rollback').exists()


def test_rendered_ami_user_writes_only_reporting_and_message(deployment):
    d = deployment
    rendered = d.module.render(d.stage, d.module.configuration(d.root / '.env')[0])
    lines = (rendered / 'manager_custom.conf').read_text().splitlines()
    assert [line for line in lines if line.startswith('[')] == ['[phonelet]']
    assert [line for line in lines if line.startswith('write')] == ['write=reporting,message']
    assert [line for line in lines if line.startswith('read')] == ['read=']


def manager_user_writes(write):
    line = '        write perm: reporting,message\n'
    assert line in MANAGER_USER
    return MANAGER_USER.replace(line, '' if write is None else f'        write perm: {write}\n')


@pytest.mark.parametrize('write, missing', [
    ('reporting,message', None),
    ('message,reporting', None),
    ('system,call,command,originate,message', 'reporting'),  # previous template, as Asterisk prints it
    ('reporting', 'message'),
    ('system,call,command,originate', 'reporting,message'),
    (None, 'reporting,message')])
def test_smoke_requires_reporting_and_message_write_permissions(deployment, write, missing):
    d = deployment
    d.manager_user.write_text(manager_user_writes(write))
    settings = d.module.Settings(ami_secret='real-secret', api_key='real-key')
    if missing is None:
        d.module.smoke(settings, 'http://127.0.0.1:8080/ready')
    else:
        with pytest.raises(RuntimeError, match=f'lacks write permission: {missing}$'):
            d.module.smoke(settings, 'http://127.0.0.1:8080/ready')


def test_deploy_refuses_ami_user_without_reporting(deployment):
    d = deployment
    d.manager_user.write_text(manager_user_writes('system,call,command,originate,message'))
    with pytest.raises(RuntimeError, match='lacks write permission: reporting$'):
        d.deploy()


def test_smoke_reports_absent_ami_user(deployment):
    d = deployment
    d.manager_user.write_text('There is no manager called phonelet\n')  # Asterisk 20 output for an unknown user
    settings = d.module.Settings(ami_secret='real-secret', api_key='real-key')
    with pytest.raises(RuntimeError, match='Required AMI user is absent'):
        d.module.smoke(settings, 'http://127.0.0.1:8080/ready')


@pytest.mark.parametrize('code,body,accepted', [
    (200, b'{"status":"ready","software_ready":true}', True),
    (503, b'{"status":"unavailable","software_ready":true,"modem_ready":false}', True),
    (503, b'{"status":"unavailable","software_ready":false}', False),
    (200, b'{"status":"ready","software_ready":false}', False),
    (200, b'{"status":"ready"}', False),
    (503, b'Service Unavailable', False),
    (200, b'[]', False),
    (401, b'{"software_ready":true}', False)])
def test_smoke_requires_software_readiness_without_network_or_ami_socket(deployment, monkeypatch, code, body, accepted):
    d = deployment
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return body
    def urlopen(request, timeout):
        if code != 200:  # urllib raises non-2xx responses; their body is still the readiness report
            raise urllib.error.HTTPError(request.full_url, code, 'error', {}, io.BytesIO(body))
        return Response()
    monkeypatch.setattr(d.module.urllib.request, 'urlopen', urlopen)
    def forbidden(*args, **kwargs):
        raise AssertionError('smoke must not open an AMI session')
    monkeypatch.setattr(d.module, 'require_durable_sms_driver', forbidden)
    monkeypatch.setattr(d.module.socket, 'create_connection', forbidden)
    settings = d.module.Settings(ami_secret='real-secret', api_key='real-key')
    if accepted:
        d.module.smoke(settings, 'http://127.0.0.1:8080/ready')
    else:
        with pytest.raises(RuntimeError, match='software is not ready'):
            d.module.smoke(settings, 'http://127.0.0.1:8080/ready')


def fake_ready_sequence(d, monkeypatch, reports):
    """/ready answers 503 with each report in turn (200 once software_ready), then the last one.

    A None report is a poll without one (the request times out). Each smoke sleep advances
    a fake clock. Returns the reports served, one per /ready poll.
    """
    reports = iter(reports)
    last = {}
    served = []
    clock = [1000]
    monkeypatch.setattr(d.module, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        # time.sleep is global: subprocess's own sub-second waits for the fake commands would make
        # the poll count depend on machine load.
        if seconds >= 1:
            clock[0] += seconds
    monkeypatch.setattr(d.module.time, 'sleep', sleep)

    class Response(io.BytesIO):
        status = 200

    def urlopen(request, timeout):
        nonlocal last
        if request.full_url.endswith('/api/modem'):
            return Response(json.dumps({'network_registered': True}).encode())
        report = next(reports, last)
        served.append(report)
        if report is None:
            raise urllib.error.URLError(TimeoutError('timed out'))
        last = report
        body = json.dumps(report).encode()
        if report.get('software_ready'):
            return Response(body)
        raise urllib.error.HTTPError(request.full_url, 503, 'unavailable', {}, io.BytesIO(body))
    monkeypatch.setattr(d.module.urllib.request, 'urlopen', urlopen)
    return served


def first_pass(records, ready=False):
    return {'software_ready': ready, 'modem_ready': ready, 'ingress_first_pass': not ready,
            'ingress_records': records}


def test_deploy_waits_for_a_first_journal_pass_that_makes_progress(deployment, monkeypatch, capsys):
    d = deployment
    # Ten minutes of first pass, growing every poll (5 s), with a poll that gets no report, then ready.
    polls = 600 // d.module.SMOKE_RETRY_SECONDS
    reports = [first_pass(10 * i) for i in range(polls)] + [first_pass(10 * polls, ready=True)]
    reports[50] = None
    polled = fake_ready_sequence(d, monkeypatch, reports)
    d.deploy()
    assert len(polled) == polls + 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith('First journal pass')]
    assert len(lines) == 19  # at 30, 60 ... 570 s
    assert lines[0] == 'First journal pass: 60 records'  # the poll at 30 s
    assert not (d.stage / 'rollback/failed-release.db').exists()


@pytest.mark.parametrize('grown_polls', [0, 30])
def test_stalled_first_journal_pass_rolls_back(deployment, monkeypatch, grown_polls):
    d = deployment
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    # Progress for grown_polls polls (past the first minute when 30), then the count stays.
    polled = fake_ready_sequence(d, monkeypatch, [first_pass(i) for i in range(grown_polls + 1)])
    with pytest.raises(RuntimeError, match='software is not ready'):
        d.deploy()
    # The last growth is seen on poll grown_polls + 1 (or the wait starts); 60 s later it fails.
    assert len(polled) == grown_polls + 1 + d.module.FIRST_PASS_STALL_SECONDS // d.module.SMOKE_RETRY_SECONDS
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert (d.stage / 'rollback/failed-release.db').exists()


def test_first_sighting_of_a_first_pass_count_is_not_growth(deployment, monkeypatch):
    d = deployment
    # Five polls without a report, then a first pass whose count never moves.
    polled = fake_ready_sequence(d, monkeypatch, [None] * 5 + [first_pass(100)])
    with pytest.raises(RuntimeError, match='software is not ready'):
        d.deploy()
    assert len(polled) == d.module.FIRST_PASS_STALL_SECONDS // d.module.SMOKE_RETRY_SECONDS + 1


def test_no_report_after_a_stall_does_not_extend_the_wait(deployment, monkeypatch):
    d = deployment
    # Growth until poll 20 (95 s), then polls without a report: the stall window still runs out.
    polled = fake_ready_sequence(d, monkeypatch, [first_pass(i) for i in range(20)] + [None] * 100)
    with pytest.raises(urllib.error.URLError):
        d.deploy()
    assert len(polled) == 20 + d.module.FIRST_PASS_STALL_SECONDS // d.module.SMOKE_RETRY_SECONDS


def test_inactive_phonelet_ends_a_growing_first_pass_at_once(deployment, monkeypatch):
    d = deployment
    polled = fake_ready_sequence(d, monkeypatch, [first_pass(i) for i in range(100)])
    run, checks = d.module.run, []
    def crashing(*args, **kwargs):
        if args == ('systemctl', 'is-active', '--quiet', 'phonelet'):
            checks.append(args)
            if len(checks) == 16:
                raise subprocess.CalledProcessError(3, args)
        return run(*args, **kwargs)
    monkeypatch.setattr(d.module, 'run', crashing)
    with pytest.raises(subprocess.CalledProcessError):
        d.deploy()
    assert len(polled) == 15
    assert (d.stage / 'rollback/failed-release.db').exists()


def test_first_journal_pass_past_the_cap_rolls_back(deployment, monkeypatch):
    d = deployment
    polls = d.module.FIRST_PASS_MAX_SECONDS // d.module.SMOKE_RETRY_SECONDS
    polled = fake_ready_sequence(d, monkeypatch, [first_pass(i) for i in range(polls + 10)])
    with pytest.raises(RuntimeError, match='First journal pass still running after 1800 s'):
        d.deploy()
    assert len(polled) == polls + 1  # the poll at 1800 s
    assert (d.stage / 'rollback/failed-release.db').exists()


@pytest.mark.parametrize('report', [
    {'software_ready': False, 'ingress_first_pass': False, 'ingress_records': 1},
    {'software_ready': False},  # an application without the first-pass fields
    {'software_ready': False, 'ingress_first_pass': True, 'ingress_records': '1'},
    None])  # no report at all
def test_unready_outside_a_first_journal_pass_rolls_back_after_a_minute(deployment, monkeypatch, report):
    d = deployment
    # A growing count, where the report has a numeric one, extends nothing outside a first pass.
    numeric = isinstance((report or {}).get('ingress_records'), int)
    polled = fake_ready_sequence(d, monkeypatch, [{**report, 'ingress_records': n} if numeric else report
                                                  for n in range(100)])
    with pytest.raises(Exception):
        d.deploy()
    assert len(polled) == d.module.SMOKE_ATTEMPTS
    assert (d.stage / 'rollback/failed-release.db').exists()


def outside_release(d):
    """Every file and directory outside the release; directory mtimes reveal created or removed entries."""
    return {p: (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns) for p in d.stage.parent.rglob('*')
            if not p.is_relative_to(d.stage) and p != d.log}


def test_check_reports_changes_without_secrets_or_mutation(deployment, monkeypatch, capsys):
    d = deployment
    with (d.root / '.env').open('a') as stream:
        # A token shorter than four characters stays unmasked so the diff text is not mangled.
        stream.write('\nPHONELET_HA_URL=http://ha.invalid\nPHONELET_HA_TOKEN=Set\n')
    manager = d.etc / 'manager_custom.conf'
    manager.write_text(manager.read_text().replace('real-secret', 'old-live-secret'))  # rotated in .env only
    pjsip = d.etc / 'pjsip_custom.conf'
    pjsip.write_text(pjsip.read_text().replace('real-password', 'old-password-1').replace('type=auth', 'type=auth\n; legacy')
                     + ';password=old-commented-pw ; disabled\nmd5_cred=old-md5-cred\n')
    d.helper.unlink()
    (d.root / '.deploy.lock').touch()
    class Socket:
        responses = [b'Response: Success\r\n\r\n',
                     b'Event: DongleDeviceEntry\r\nDevice: dongle0\r\nSMSJournalVersion: 1\r\n'
                     b'SMSJournalStatus: ok\r\n\r\nEvent: DongleShowDevicesComplete\r\n\r\n']
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, _): pass
        def sendall(self, _): pass
        def recv(self, _): return self.responses.pop(0) if self.responses else b''
    connections = []
    monkeypatch.setattr(d.module.socket, 'create_connection', lambda *a, **kw: connections.append(a) or Socket())
    before = outside_release(d)
    monkeypatch.setattr(sys, 'argv', ['deploy-release.py', '--stage', str(d.stage), '--root', str(d.root),
                                      '--etc', str(d.etc), '--systemd', str(d.systemd),
                                      '--helper', str(d.helper), '--check'])
    # The script as a program, with the recordings refusal list the fixture's temp dir allows (see script()).
    program = d.stage / 'scripts/deploy-release-check.py'
    program.write_text(re.sub(r'^ASTERISK_UNWRITABLE = .*$', 'ASTERISK_UNWRITABLE = ' + repr(d.module.ASTERISK_UNWRITABLE),
                              (ROOT / 'scripts/deploy-release.py').read_text(), count=1, flags=re.M))
    runpy.run_path(str(program), run_name='__main__')  # returns: exit code 0
    program.unlink()
    output = capsys.readouterr().out
    assert output.rstrip().endswith('Check passed; no changes made')
    assert connections  # AMI and driver journal are still checked before the report
    for secret in ('real-secret', 'old-live-secret', 'real-password', 'old-password-1', 'real-key',
                   'old-commented-pw', 'old-md5-cred'):
        assert secret not in output
    assert f'{manager}: changed\n(only masked secret values differ)\n' in output
    assert '-; legacy\n' in output and ' password=[REDACTED]\n' in output
    assert '-; old dialplan' in output and 'n,Dial(PJSIP/phone,30,b(sub-prepare-incoming-call^s^1)U(sub-start-recording^${RECORDING_PATH}))' in output
    assert 'GotoIf($["no" != "yes"]?done)' in output  # recording is disabled at run time
    assert f'{d.etc / "dongle.conf"}: unchanged' in output
    assert f'{d.etc / "pjsip_custom.conf"}: changed' in output
    assert f'{d.etc / "cdr_custom.conf"}: new' in output
    assert f'{d.dropin}: changed' in output and 'old-app' not in output
    assert f'{d.helper}: new' in output
    assert outside_release(d) == before
    assert not (d.stage / 'rollback').exists()
    log = d.log.read_text()
    for command in ('systemctl stop', 'systemctl restart', 'systemctl daemon-reload', 'reload'):
        assert command not in log


def test_check_masks_secret_fields_shorter_than_four_characters(deployment, capsys):
    d = deployment
    env = d.root / '.env'
    env.write_text(env.read_text().replace('AST_SIP_PASSWORD=real-password', 'AST_SIP_PASSWORD=synthetic123'))
    pjsip = d.etc / 'pjsip_custom.conf'
    pjsip.write_text(pjsip.read_text().replace('password=real-password', 'password=Qx7 ; old'))
    d.deploy(check=True)
    output = capsys.readouterr().out
    assert 'synthetic123' not in output and 'Qx7' not in output
    assert '-password=[REDACTED] ; old\n' in output and '+password=[REDACTED]\n' in output
    # A longer secret inside a field name must not unmask the field.
    assert 'xy' not in d.module.redact('password=xy\n', {'pass'})
    assert d.module.redact('secret=ab\\;cd ; note\n', set()) == 'secret=[REDACTED] ; note\n'
    assert d.module.redact('secret=ab\\\\;cd\n', set()) == 'secret=[REDACTED]\n'


def test_failed_snapshot_of_a_new_database_still_restores_configs(deployment, monkeypatch):
    d = deployment
    d.db.unlink()  # first deployment: no prior schema to roll back to
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    old_unit = d.dropin.read_bytes()
    def smoke(*args):
        with sqlite3.connect(d.db) as db:
            db.execute('create table if not exists candidate (value text)')
        raise RuntimeError('unready')
    def snapshot(*args):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(d.module, 'smoke', smoke)
    monkeypatch.setattr(d.module, 'snapshot_database', snapshot)
    with pytest.raises(RuntimeError, match=r'ROLLBACK INCOMPLETE: new database snapshot failed: '
                       r'OSError: \[Errno 28\] No space left on device, new database preserved'):
        d.deploy()
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.dropin.read_bytes() == old_unit
    assert d.helper.read_text() == 'old helper'
    with sqlite3.connect(d.db) as db:
        assert db.execute("select name from sqlite_master where name='candidate'").fetchone()
    log = d.log.read_text()
    assert log.count('systemctl daemon-reload') == 2
    assert log.count('systemctl restart phonelet') == 1  # the old app never starts on the new schema


def test_check_reports_transport_change_and_refuses_failed_preflight(deployment, capsys):
    d = deployment
    target = d.etc / 'pjsip_custom.conf'
    target.write_text(target.read_text().replace(':5060', ':5090'))
    d.deploy(check=True)
    output = capsys.readouterr().out
    assert 'SIP transport changes: deployment needs --allow-asterisk-restart' in output
    assert output.rstrip().endswith('Check passed; no changes made')
    assert 'restart' not in d.log.read_text()
    with (d.root / '.env').open('a') as stream:
        stream.write('\nPHONELET_API_KEY=GENERATE_STRONG_KEY_HERE\n')
    with pytest.raises(ValueError):
        d.deploy(check=True)
    assert 'Check passed' not in capsys.readouterr().out
    assert not (d.stage / 'rollback').exists()


@pytest.mark.parametrize('mode,status', [('--check', 0), ('--check', 1), ('', 0), ('--bogus', None),
                                         ('--allow-asterisk-restart --check', None), ('--restart-asterisk', 0)])
def test_deploy_script_check_mode_removes_its_release(tmp_path, mode, status):
    commands = tmp_path / 'bin'
    log = tmp_path / 'ssh.log'
    executable(commands / 'ssh', '''echo "$*" >> "$SSH_LOG"
if [[ "$*" == *mktemp* ]]; then
 echo /root/phonelet-releases/release.TEST123
elif [[ "$*" == *deploy-release.py* ]]; then
 exit "$REMOTE_STATUS"
fi
''')
    executable(commands / 'scp', 'exit 0\n')
    result = subprocess.run(['bash', str(ROOT / 'deploy.sh'), 'fake-host'] + mode.split(),
                            env=os.environ | {'PATH': str(commands) + ':' + os.environ['PATH'], 'SSH_LOG': str(log),
                                              'REMOTE_STATUS': str(status)}, capture_output=True)
    if status is None:
        assert result.returncode == 2 and not log.exists()
        return
    assert result.returncode == status
    calls = log.read_text()
    assert f"deploy-release.py --stage '/root/phonelet-releases/release.TEST123' {mode}".rstrip() in calls
    removal = "rm -rf '/root/phonelet-releases/release.TEST123'"
    assert calls.endswith(removal + '\n') if mode == '--check' else removal not in calls


@pytest.mark.parametrize('status, reloaded', [('Running', True), ('Not Running', False), ('', False)])
def test_deploy_reloads_cdr_csv_only_while_it_runs(deployment, monkeypatch, status, reloaded):
    # The reload unregisters it (cdr.conf has no [csv]); one that declined to load stays untouched.
    d = deployment
    # A host from before: csv on, cdr_manager enabled. The file must be replaced, not left behind.
    (d.etc / 'cdr.conf').write_text('[general]\nunanswered=yes\n\n[csv]\nusegmtime=yes\n')
    (d.etc / 'cdr_manager.conf').write_text('[general]\nenabled=yes\n')
    monkeypatch.setenv('CDR_CSV', status)
    d.deploy()
    assert ('module reload cdr_csv.so' in d.log.read_text()) is reloaded
    assert 'module reload cdr_manager' in d.log.read_text()
    assert not re.search(r'^\[csv\]', (d.etc / 'cdr.conf').read_text(), re.M)
    assert 'enabled=no' in (d.etc / 'cdr_manager.conf').read_text()


@pytest.mark.parametrize('backend', ['csv', 'cdr_manager'])
def test_cdr_backend_left_logging_rolls_the_deploy_back(deployment, monkeypatch, backend):
    # cdr_csv cannot unregister while a CDR is active, but its reload still says it succeeded.
    d = deployment
    (d.etc / 'cdr.conf').write_text('[general]\nunanswered=yes\n\n[csv]\nusegmtime=yes\n')
    (d.etc / 'cdr_manager.conf').write_text('[general]\nenabled=yes\n')
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    monkeypatch.setenv('CDR_CSV', 'Running')
    monkeypatch.setenv('CDR_STILL_LOGS', backend)
    with pytest.raises(RuntimeError, match='still logs CDR'):
        d.deploy()
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.log.read_text().rstrip().endswith('systemctl restart phonelet')


def test_deploy_installs_primary_configs_and_unloads_running_noload_modules(deployment, monkeypatch):
    # A host from the samples: demo in the dialplan, AMI on every address, IAX2 and UNISTIM running.
    d = deployment
    (d.etc / 'extensions.conf').write_text('[default]\ninclude => demo\n#include "extensions_custom.conf"\n')
    (d.etc / 'manager.conf').write_text('[general]\nbindaddr = 0.0.0.0\n#include "manager_custom.conf"\n')
    (d.etc / 'modules.conf').write_text('[modules]\nautoload=yes\n')
    monkeypatch.setenv('RUNNING_MODULES', 'res_xmpp.so chan_unistim.so chan_motif.so chan_iax2.so')
    monkeypatch.setenv('STOPPED_MODULES', 'pbx_ael.so')
    d.deploy()
    for name in ('extensions.conf', 'manager.conf', 'modules.conf'):
        assert (d.etc / name).read_bytes() == (ROOT / 'asterisk' / name).read_bytes()
    log = d.log.read_text().splitlines()
    unloads = [line.split()[-1] for line in log if line.startswith('asterisk -rx module unload ')]
    # Only running ones, in the file's order: chan_motif before res_xmpp, which it needs.
    assert unloads == ['chan_iax2.so', 'chan_unistim.so', 'chan_motif.so', 'res_xmpp.so']
    # After the dialplan and AMI reloads: the new dialplan is live before anything goes away.
    first_unload = log.index('asterisk -rx module unload chan_iax2.so')
    assert log.index('asterisk -rx dialplan reload') < first_unload and log.index('asterisk -rx manager reload') < first_unload


def test_check_names_the_modules_a_deploy_would_unload(deployment, monkeypatch, capsys):
    monkeypatch.setenv('RUNNING_MODULES', 'chan_unistim.so')
    deployment.deploy(check=True)
    assert 'Deploy unloads running module chan_unistim.so' in capsys.readouterr().out
    assert 'module unload' not in deployment.log.read_text()


def test_failed_module_unload_rolls_the_deploy_back(deployment, monkeypatch):
    d = deployment
    (d.etc / 'modules.conf').write_text('[modules]\nautoload=yes\n')
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    monkeypatch.setenv('RUNNING_MODULES', 'chan_iax2.so')
    monkeypatch.setenv('UNLOAD_FAILS', 'chan_iax2.so')
    with pytest.raises(RuntimeError, match='module unload chan_iax2.so'):
        d.deploy()
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    log = d.log.read_text()
    assert log.rstrip().endswith('systemctl restart phonelet')
    # The rollback unloads nothing, whatever the restored file noloads.
    assert log.count('module unload') == 1


@pytest.mark.parametrize('check', [False, True])
def test_remote_ami_host_stops_deploy_before_any_change(deployment, check):
    # manager.conf binds AMI to 127.0.0.1: Phonelet on another host could no longer log in.
    d = deployment
    with (d.root / '.env').open('a') as stream: stream.write('\nPHONELET_AMI_HOST=192.0.2.5\n')
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    with pytest.raises(ValueError, match='AMI listens on 127.0.0.1 only'):
        d.deploy(check=check)
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert not d.log.exists() or 'systemctl stop' not in d.log.read_text()


def test_deploy_reads_every_noload_line_of_our_modules_conf(deployment, monkeypatch):
    # A line the regex skipped (an inline comment, another spelling) would stay loaded unnoticed.
    path = ROOT / 'asterisk' / 'modules.conf'
    lines = [line for line in path.read_text().splitlines() if line.strip().lower().startswith('noload')]
    monkeypatch.setattr(deployment.module, 'cli', lambda command: command.split()[-1] + '  Module  0  Running  core\n')
    assert len(deployment.module.running_noload_modules(path)) == len(lines) > 10


def test_rollback_kills_a_candidate_that_does_not_stop_and_starts_the_old_app(deployment, monkeypatch):
    # Stop 1 is the forward one; stop 2, the rollback's, times out on a candidate that ignores SIGTERM.
    d = deployment
    before = {p.name: p.read_bytes() for p in d.etc.iterdir()}
    old_unit = d.dropin.read_bytes()
    monkeypatch.setenv('FAIL_PHASE', 'endpoint')
    monkeypatch.setenv('FAIL_STOPS', '2')
    with pytest.raises(RuntimeError, match='^Required SIP endpoint is absent$'):  # rollback complete
        d.deploy()
    assert {p.name: p.read_bytes() for p in d.etc.iterdir()} == before
    assert d.dropin.read_bytes() == old_unit and d.helper.read_text() == 'old helper'
    lines = d.log.read_text().splitlines()
    kill = lines.index('systemctl kill --signal=SIGKILL phonelet')
    stops = [i for i, line in enumerate(lines) if line == 'systemctl stop phonelet']
    assert len(stops) == 3 and stops[1] < kill < stops[2]
    assert lines[-1] == 'systemctl restart phonelet'


def test_rollback_that_cannot_stop_the_candidate_names_the_error_and_keeps_the_database(deployment, monkeypatch):
    d = deployment
    monkeypatch.setenv('FAIL_PHASE', 'endpoint')
    monkeypatch.setenv('FAIL_STOPS', '2 3')
    with pytest.raises(RuntimeError, match='ROLLBACK INCOMPLETE: could not stop application; database untouched: '
                                           'CalledProcessError: '):
        d.deploy()
    assert not (d.stage / 'rollback/failed-release.db').exists()
    assert d.log.read_text().count('systemctl restart phonelet') == 1  # the forward start only


def test_phonelet_commands_outlast_the_systemd_stop_timeout(deployment, monkeypatch):
    # A candidate under a previous drop-in without TimeoutStopSec stops within systemd's default 90 s.
    d = deployment
    original_run = d.module.run
    default = inspect.signature(original_run).parameters['timeout'].default
    timeouts = []
    def run(*args, timeout=default):
        if args[1:] in (('stop', 'phonelet'), ('restart', 'phonelet')):
            timeouts.append(timeout)
        return original_run(*args, timeout=timeout)
    monkeypatch.setattr(d.module, 'run', run)
    monkeypatch.setenv('FAIL_PHASE', 'endpoint')
    with pytest.raises(RuntimeError):
        d.deploy()
    assert len(timeouts) == 4 and min(timeouts) > 90  # forward stop and start, rollback stop and start


def test_preflight_accepts_a_stopped_phonelet_and_the_deploy_starts_it(deployment, monkeypatch):
    # An inactive or failed service (a crash, a rollback that could not start it) is repaired by a deploy.
    monkeypatch.setenv('PHONELET_INACTIVE', '1')
    deployment.deploy()
    assert 'systemctl restart phonelet' in deployment.log.read_text()


@pytest.mark.parametrize('check', [False, True])
def test_preflight_refuses_a_phonelet_unit_that_is_not_loaded(deployment, monkeypatch, check):
    d = deployment
    monkeypatch.setenv('PHONELET_LOAD_STATE', 'not-found')
    old_unit = d.dropin.read_bytes()
    with pytest.raises(RuntimeError, match='Phonelet service is not loaded'):
        d.deploy(check=check)
    assert d.dropin.read_bytes() == old_unit
    assert not (d.stage / 'rollback').exists()
    assert 'systemctl stop' not in d.log.read_text()


def test_failed_release_snapshot_is_reported_as_itself_not_as_incompatibility(deployment, monkeypatch):
    # A full disk while copying the candidate's database is not a schema problem.
    d = deployment
    original = d.module.snapshot_database
    def snapshot(source, destination):
        if destination.name == 'failed-release.db':
            raise OSError(28, 'No space left on device')
        return original(source, destination)
    monkeypatch.setattr(d.module, 'snapshot_database', snapshot)
    monkeypatch.setenv('FAIL_PHASE', 'endpoint')
    with pytest.raises(RuntimeError, match=r'ROLLBACK INCOMPLETE: failed release database snapshot: '
                                           r'OSError: \[Errno 28\] No space left on device$') as error:
        d.deploy()
    assert 'backward compatible' not in str(error.value)
    assert d.log.read_text().count('systemctl restart phonelet') == 1  # the old app does not start


def test_sync_before_the_application_stops_and_before_verified(deployment, monkeypatch, capsys):
    d = deployment
    def sync():
        with d.log.open('a') as log:
            log.write('os.sync\n')
    monkeypatch.setattr(d.module.os, 'sync', sync)
    d.deploy()
    lines = d.log.read_text().splitlines()
    first_stop = lines.index('systemctl stop phonelet')
    assert lines[first_stop - 1] == 'os.sync'
    assert lines[-2:] == ['systemctl reset-failed asterisk', 'os.sync']
    assert capsys.readouterr().out.rstrip().splitlines()[-1].startswith('Deployment verified')


def test_failed_start_limit_reset_after_a_verified_deploy_is_a_warning(deployment, monkeypatch, capsys):
    d = deployment
    monkeypatch.setenv('RESET_FAILED_FAILS', '1')
    d.deploy()
    output = capsys.readouterr().out
    assert 'Warning: systemctl reset-failed asterisk: CalledProcessError' in output
    assert output.rstrip().splitlines()[-1].startswith('Deployment verified')


HOURS_AGO = 2 * 3600


@pytest.fixture
def releases(deployment, tmp_path):
    """The stage as the current release among older ones; the old drop-in runs release.PREVIOUS."""
    d = deployment
    parent = tmp_path / 'releases'
    parent.mkdir()
    current = parent / 'release.CURRENT'
    d.stage.rename(current)
    old = time.time() - HOURS_AGO
    paths = {}
    for name in ('release.PREVIOUS', 'release.OLD1', 'release.OLD2', 'release.FRESH', 'release.bad-name', 'other'):
        paths[name] = parent / name
        (paths[name] / '.venv').mkdir(parents=True)
        (paths[name] / 'runtime.env').write_text('PHONELET_API_KEY="old-key"\n')
        if name != 'release.FRESH':  # FRESH is being staged by another deploy.sh right now
            os.utime(paths[name], (old, old))
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    os.utime(elsewhere, (old, old))
    (parent / 'release.LINK').symlink_to(elsewhere, target_is_directory=True)
    d.dropin.write_text(f'[Service]\nWorkingDirectory={paths["release.PREVIOUS"]}\n'
                        f'ExecStart={paths["release.PREVIOUS"]}/.venv/bin/python old-app\n')
    paths.update(current=current, elsewhere=elsewhere, link=parent / 'release.LINK')
    deploy = lambda **kw: d.module.deploy(current, d.root, d.etc, d.systemd, d.helper, **kw)
    return SimpleNamespace(paths=paths, deploy=deploy, parent=parent)


def test_verified_deploy_keeps_current_previous_and_fresh_releases_only(releases, deployment):
    r = releases
    r.deploy()
    assert sorted(p.name for p in r.parent.iterdir()) == [
        'other', 'release.CURRENT', 'release.FRESH', 'release.LINK', 'release.PREVIOUS', 'release.bad-name']
    assert r.paths['elsewhere'].is_dir()
    assert f'WorkingDirectory={r.paths["current"]}\n' in deployment.dropin.read_text()


def test_release_that_cannot_be_removed_is_a_warning(releases, monkeypatch, capsys, deployment):
    r = releases
    def rmtree(path):
        raise PermissionError(13, 'Permission denied', str(path))
    monkeypatch.setattr(deployment.module.shutil, 'rmtree', rmtree)
    r.deploy()
    output = capsys.readouterr().out
    assert 'Warning: could not remove old release ' + str(r.paths['release.OLD1']) in output
    assert 'PermissionError' in output and output.rstrip().splitlines()[-1].startswith('Deployment verified')
    assert r.paths['release.OLD1'].is_dir() and r.paths['release.OLD2'].is_dir()


@pytest.mark.parametrize('outcome', ['check', 'failed'])
def test_check_or_failed_deploy_removes_no_release(releases, monkeypatch, outcome):
    r = releases
    if outcome == 'check':
        r.deploy(check=True)
    else:
        monkeypatch.setenv('FAIL_PHASE', 'endpoint')
        with pytest.raises(RuntimeError):
            r.deploy()
    for name in ('release.PREVIOUS', 'release.OLD1', 'release.OLD2', 'release.FRESH'):
        assert r.paths[name].is_dir()


@pytest.mark.parametrize('phase', ['reload', 'restart'])
def test_rollback_without_a_database_before_the_deploy_leaves_the_old_app_stopped(deployment, monkeypatch, phase):
    # The database was moved aside to be recreated (docs/operations.md): the previous release must
    # not start on none, or it would notify the whole retained journal and CDR again.
    d = deployment
    d.db.unlink()
    monkeypatch.setenv('PHONELET_INACTIVE', '1')
    monkeypatch.setenv('FAIL_PHASE', phase)
    old_unit = d.dropin.read_bytes()
    with pytest.raises(RuntimeError, match='ROLLBACK INCOMPLETE: no database before the deploy'):
        d.deploy()
    assert d.dropin.read_bytes() == old_unit
    log = d.log.read_text().splitlines()
    assert log[-1] != 'systemctl restart phonelet'
    assert log.count('systemctl restart phonelet') == (1 if phase == 'restart' else 0)


def test_deploy_cli_still_requires_stage():
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/deploy-release.py')],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode != 0 and '--stage is required' in result.stderr
