#!/usr/bin/env python3
"""Generate configuration or deploy a staged release after complete preflight."""
import argparse
from contextlib import closing, contextmanager, suppress
import difflib
import fcntl
import io
import ipaddress
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request

from dotenv import dotenv_values
from dotenv.parser import parse_stream
from pydantic import ValidationError
from phonelet.ami_client import MAX_LIST_MESSAGES, MAX_MESSAGE_BYTES, encode_action, parse_message
from phonelet.constants import DEFAULT_DONGLE_DEVICE, RSSI_UNKNOWN, SIP_DOMAIN_PATTERN
from phonelet.contracts import DEVICE_LIST_ACTION, sms_journal_ok
from phonelet.db import Database
from phonelet.netplan_wifi import MARKER_NAME as WIFI_MARKER_NAME


@contextmanager
def without_phonelet_environment():
    saved = {key: value for key, value in os.environ.items() if key.upper().startswith('PHONELET_')}
    try:
        for key in saved:
            del os.environ[key]
        yield
    finally:
        os.environ.update(saved)


# config.py creates runtime Settings at import; deployment validates its own settings below.
with without_phonelet_environment():
    from phonelet.config import CDR_LOG_PATH, SMS_INGRESS_DIR, Settings, is_placeholder


CONFIGS = ('extensions_custom.conf', 'pjsip_custom.conf', 'manager_custom.conf',
           'dongle.conf', 'cdr.conf', 'cdr_manager.conf', 'cdr_custom.conf',
           'extensions.conf', 'manager.conf', 'modules.conf')
ASTERISK_DIR = Path(__file__).resolve().parents[1] / 'asterisk'
AST_PLACEHOLDER = re.compile(r'\$\{(AST_[A-Z0-9_]+)\}')
# The local number rule: its exten line and the same => lines after it. With no trunk prefix render()
# leaves it out, as _XXXX. it would convert every number of six or more digits.
LOCAL_NUMBER_RULE = re.compile(r'^exten => _\$\{AST_TRUNK_PREFIX\}.*\n(?:[ \t]+same => .*\n)*', re.M)
DEFAULT_SIP_BIND = '0.0.0.0:5060'
DEFAULT_SIP_PORT = '5060'
# Host udev links: interface 00 is the modem (MDM) port, never data=; PCUI (interface 02)
# carries data=, interface 01 audio=.
MDM_LINK = '/dev/dongle0-modem'
PCUI_LINK = '/dev/dongle0-at'
AUDIO_LINK = '/dev/dongle0-audio'
DATA_PORT_FIX = 'set AST_DONGLE_DATA=' + PCUI_LINK + ' (PCUI, interface 02)'
# Fixed names in the templates, checked by the smoke test.
AMI_USER = 'phonelet'  # [phonelet] in asterisk/manager_custom.conf.tpl
SIP_ENDPOINT = 'phone'  # endpoint/AOR in asterisk/pjsip_custom.conf.tpl; not AST_SIP_USERNAME
SECRET_NAME = re.compile(r'SECRET|PASSWORD|TOKEN|KEY|CRED', re.I)
# One Asterisk config line: indent and name, value, trailing comment. Like Asterisk, a ';' right
# after a backslash is part of the value.
CONFIG_FIELD = re.compile(r'^([ \t;]*([\w.-]+)[ \t]*=>?[ \t]*)((?:\\;|[^;\n])*?)([ \t]*(?:(?<!\\);.*)?)$', re.M)
REDACTED = '[REDACTED]'
# Shorter values would mask unrelated text; redact() still masks their fields by name.
MIN_MASKED_SECRET_LENGTH = 4
# The smoke check reaches a wildcard API listener through the loopback of the same family.
WILDCARD_LOOPBACK = {'0.0.0.0': '127.0.0.1', '::': '::1'}
# About a minute for the app and Asterisk to settle after the restart.
SMOKE_ATTEMPTS = 12
SMOKE_RETRY_SECONDS = 5
# A first journal pass on a new database may outlast that minute: smoke keeps waiting while
# /ready last reported it in progress and its record count grew within the stall window, up to the cap.
FIRST_PASS_MAX_SECONDS = 1800
FIRST_PASS_STALL_SECONDS = 60
FIRST_PASS_PROGRESS_SECONDS = 30  # one progress line about this often
monotonic = time.monotonic  # the smoke wait's clock; tests replace it
# Written into the release drop-in: systemd kills a candidate that ignores SIGTERM after this.
UNIT_STOP_TIMEOUT_SECONDS = 30
# Sandbox of the release drop-in; Phonelet stays root. Left out: PrivateTmp (wpa_cli binds its reply
# socket in /tmp, where the wpa_supplicant outside the unit cannot answer it) and ProtectKernelTunables
# (a read-only /sys makes the `udevadm trigger` of `netplan apply` fail, so udev rules are not re-applied).
SANDBOX_READ_WRITE = ('/etc/netplan',)  # the Wi-Fi command's netplan file
SANDBOX = (
    'NoNewPrivileges=yes',
    'ProtectSystem=full',  # /usr, /boot, /efi and /etc read-only; netplan itself writes only /run
    'ProtectHome=read-only',  # the release and .env in /root are only read at runtime
    *('ReadWritePaths=-' + path for path in SANDBOX_READ_WRITE),
    'ProtectKernelModules=yes',
    'ProtectKernelLogs=yes',
    'ProtectControlGroups=yes',
    'ProtectClock=yes',
    'LockPersonality=yes',
    'RestrictRealtime=yes',
    'RestrictSUIDSGID=yes',
    'RestrictNamespaces=yes',
    # HTTP API, AMI and channels; wpa_cli and systemctl (D-Bus); ip, udevadm and netplan (netlink).
    'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK',
    'LimitCORE=0',  # no core files: they would hold SMS texts and secrets
)
SANDBOX_READ_ONLY = ('/usr', '/boot', '/efi', '/etc', '/root', '/home', '/run/user')
# Every stop or restart of Phonelet waits longer than systemd's stop timeout, including the 90 s
# default under a previous drop-in without TimeoutStopSec.
SERVICE_COMMAND_TIMEOUT = 120
RELEASE_NAME = re.compile(r'release\.[A-Za-z0-9]+')  # deploy.sh: mktemp -d .../release.XXXXXXXX
# deploy.sh creates and fills a release before it takes the deploy lock: a release modified
# within this time may be a deployment being staged right now, so cleanup leaves it.
STAGING_GRACE_SECONDS = 3600
# The stage holds rollback/phonelet.db and, on rollback, rollback/failed-release.db; one more copy is headroom.
DATABASE_COPIES_RESERVED = 3
# These template inputs are always derived; users configure only the application key.
DERIVED_KEYS = {'AST_AMI_SECRET': 'PHONELET_AMI_SECRET',
                'AST_RECORDINGS_DIR': 'PHONELET_RECORDINGS_DIR',
                'AST_MIN_FREE_BYTES': 'PHONELET_MIN_FREE_BYTES'}
# Read-only or private (PrivateTmp) for Asterisk: asterisk/systemd/asterisk.service.d/10-phonelet.conf.
ASTERISK_UNWRITABLE = ('/usr', '/etc', '/boot', '/efi', '/home', '/root', '/run/user', '/tmp', '/var/tmp')
# App settings derived from Asterisk variables; written to runtime.env, not to .env.
DERIVED_SETTINGS = {'PHONELET_RECORD_CALLS': 'AST_RECORD_CALLS'}
REQUIRED_KEYS = ('PHONELET_AMI_SECRET', 'PHONELET_API_KEY', 'AST_SIP_PASSWORD',
                 'AST_COUNTRY_PREFIX', 'PHONELET_SIP_DOMAIN')


class SoftwareNotReady(RuntimeError):
    """Smoke failed on the readiness report, which the wait loop still reads."""

    def __init__(self, readiness):
        super().__init__('Phonelet software is not ready')
        self.readiness = readiness


class DeploymentInterrupted(BaseException):
    """Cancellation must bypass retry loops and enter transaction rollback."""


class DeploymentSignals:
    def __init__(self):
        self.rolling_back = False
        self.previous = {}

    def __enter__(self):
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            self.previous[signum] = signal.signal(signum, self.interrupt)
        return self

    def interrupt(self, signum, _frame):
        if not self.rolling_back:
            self.rolling_back = True
            raise DeploymentInterrupted(f'Deployment interrupted by {signal.Signals(signum).name}')
        # Let recovery finish even if the caller cancels repeatedly.

    def __exit__(self, *_):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)


def run(*args, timeout=60):
    result = subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout)
    return result.stdout


def describe(error):
    return f'{type(error).__name__}: {error}'


def stop_application():
    run('systemctl', 'stop', 'phonelet', timeout=SERVICE_COMMAND_TIMEOUT)


def restart_application():
    run('systemctl', 'restart', 'phonelet', timeout=SERVICE_COMMAND_TIMEOUT)


def cli(command):
    output = run('asterisk', '-rx', command)
    if re.search(r'No such|Unable to|Failed|Error loading|not found|could not be reloaded', output, re.I):
        raise RuntimeError('Asterisk command failed: ' + command)
    return output


def known_asterisk_variables():
    return {name for template in ASTERISK_DIR.glob('*.tpl')
            for name in AST_PLACEHOLDER.findall(template.read_text())}


def configuration(env_file):
    try:
        text = Path(env_file).read_text(encoding='utf-8')
    except FileNotFoundError:
        text = ''
    # dotenv_values skips unparsable lines with a warning; stop instead, naming the line but never its text.
    bad = next((binding.original for binding in parse_stream(io.StringIO(text)) if binding.error), None)
    if bad is not None:
        # The parser's line is where skipped blank lines start; count them to name the bad line itself.
        blank = bad.string[:len(bad.string) - len(bad.string.lstrip())].count('\n')
        raise ValueError(f'.env line {bad.line + blank} cannot be parsed')
    env = dict(dotenv_values(stream=io.StringIO(text), interpolate=False))
    for key, source in (*DERIVED_KEYS.items(), *DERIVED_SETTINGS.items()):
        if key in env:
            raise ValueError(key + ' is derived from ' + source + '; remove it from .env')
    multiline = [key for key, value in env.items() if value is not None and any(c in value for c in '\r\n')]
    if multiline:
        raise ValueError('Multiline environment values are not supported: ' + ', '.join(multiline))
    bare = [key for key, value in env.items() if value is None]
    if bare:
        raise ValueError('Environment contains an entry without a value: ' + ', '.join(bare))
    unknown_phonelet = sorted(key for key in env if key.startswith('PHONELET_')
                              and key.removeprefix('PHONELET_').lower() not in Settings.model_fields)
    if unknown_phonelet:
        raise ValueError('Unknown PHONELET_ variables: ' + ', '.join(unknown_phonelet))
    for key in REQUIRED_KEYS:
        if key not in env:
            raise ValueError(key + ' is required; set it in .env')
    if is_placeholder(env['PHONELET_SIP_DOMAIN']):
        raise ValueError('PHONELET_SIP_DOMAIN must not be empty or a placeholder')
    if not SIP_DOMAIN_PATTERN.fullmatch(env['PHONELET_SIP_DOMAIN']):
        raise ValueError('Unsafe configuration value: PHONELET_SIP_DOMAIN')
    kwargs = {k.removeprefix('PHONELET_').lower(): v for k, v in env.items() if k.startswith('PHONELET_')}
    try:
        # Settings fills fields the .env leaves out from os.environ: only the .env counts here.
        with without_phonelet_environment():
            settings = Settings(**kwargs)
    except ValidationError as error:
        # Field diagnostics may echo input. Our model validator uses value-free messages.
        errors = error.errors()
        problems = sorted({'PHONELET_' + str(item['loc'][0]).upper() for item in errors if item['loc']})
        problems.extend(str(item.get('ctx', {}).get('error', item['msg'])) for item in errors if not item['loc'])
        raise ValueError('Invalid configuration: ' + ', '.join(problems)) from None
    settings.check_secrets()
    unknown = sorted({key for key in env if key.startswith('AST_')} - known_asterisk_variables())
    if unknown:
        raise ValueError('Unknown AST_ variables: ' + ', '.join(unknown))
    # normpath keeps a leading //, so collapse it too: /dev//x, //dev/x and /dev/./x are the same path.
    data = env.get('AST_DONGLE_DATA') or '/'
    if '/' + os.path.normpath(data).lstrip('/') == MDM_LINK:
        raise ValueError('AST_DONGLE_DATA is the modem (MDM) port ' + MDM_LINK + ', which carries no calls '
                         'or SMS notifications: ' + DATA_PORT_FIX)
    defaults = dict(AST_SIP_USERNAME='phone', AST_RECORD_CALLS='no', AST_TRUNK_PREFIX='',
                    AST_SIP_BIND=DEFAULT_SIP_BIND,
                    AST_DONGLE_RXGAIN='0', AST_DONGLE_TXGAIN='0',
                    AST_DONGLE_DATA=PCUI_LINK, AST_DONGLE_AUDIO=AUDIO_LINK)
    env = defaults | env
    for key, source in DERIVED_KEYS.items():
        env[key] = str(getattr(settings, source.removeprefix('PHONELET_').lower()))
    if is_placeholder(env['AST_SIP_PASSWORD']):
        raise ValueError('AST_SIP_PASSWORD must not be empty or a placeholder')
    if len(env['AST_SIP_PASSWORD']) < 12:
        raise ValueError('AST_SIP_PASSWORD must be at least 12 characters long')
    if not env.get('AST_COUNTRY_PREFIX'):
        raise ValueError('AST_COUNTRY_PREFIX is required for local SMS and call number conversion; set it in .env')
    if settings.ami_username != AMI_USER:
        raise ValueError('PHONELET_AMI_USERNAME is fixed by the phonelet template')
    port_match = re.search(r'^[ \t]*port[ \t]*=[ \t]*(\d+)[ \t]*(?:;[^\n]*)?$',
                           (ASTERISK_DIR / 'manager.conf').read_text(), re.M)
    if port_match is None:
        raise ValueError('AMI port missing or invalid in asterisk/manager.conf')
    ami_port = int(port_match[1])
    if settings.ami_port != ami_port:
        raise ValueError(f'AMI port must match asterisk/manager.conf: set PHONELET_AMI_PORT={ami_port}')
    # Values enter Asterisk configuration and recording shell commands: no syntax injection.
    patterns = {'AST_SIP_USERNAME': r'[A-Za-z0-9_-]+', 'AST_COUNTRY_PREFIX': r'\+[0-9]+',
                'AST_TRUNK_PREFIX': r'[0-9]?', 'AST_SIP_BIND': r'[0-9.]+(?::[0-9]{1,5})?'}
    for key in ('AST_RECORDINGS_DIR', 'AST_DONGLE_DATA', 'AST_DONGLE_AUDIO'):
        patterns[key] = r'/[A-Za-z0-9_./-]+'
    for key, pattern in patterns.items():
        if not re.fullmatch(pattern, env[key]) or '/..' in env[key]:
            raise ValueError('Unsafe configuration value: ' + key)
    for key, value in env.items():
        if key.startswith('AST_') and any(c in value for c in '\r\n;#'):
            raise ValueError('Unsafe configuration value: ' + key)
    # asterisk.service sees these read-only (ProtectSystem=full, ProtectHome=read-only) or as its own
    # private /tmp: no recording there, also not through a symlink (/home may link to /var/home).
    recordings = '/' + os.path.normpath(env['AST_RECORDINGS_DIR']).lstrip('/')
    tops = set(ASTERISK_UNWRITABLE) | {os.path.realpath(top) for top in ASTERISK_UNWRITABLE}
    if any(path == top or path.startswith(top + '/')
           for path in (recordings, os.path.realpath(recordings)) for top in tops):
        raise ValueError('PHONELET_RECORDINGS_DIR must be outside ' + ', '.join(ASTERISK_UNWRITABLE)
                         + ': asterisk.service cannot write there')
    for key in ('AST_DONGLE_RXGAIN', 'AST_DONGLE_TXGAIN'):
        # chan_dongle passes int gains to ast_frame_adjust_volume(), whose absolute
        # multiplier/divisor is a signed short in the pinned Asterisk version.
        if not re.fullmatch(r'[+-]?[0-9]+', env[key]) or not -32767 <= int(env[key]) <= 32767:
            raise ValueError(key + ' must be an integer between -32767 and 32767')
    address, _, port = env['AST_SIP_BIND'].partition(':')
    try:
        ipaddress.IPv4Address(address)
        valid_bind = 1 <= int(port or DEFAULT_SIP_PORT) <= 65535
    except ValueError:
        valid_bind = False
    if not valid_bind:
        raise ValueError('AST_SIP_BIND must be an IPv4 address with an optional port, e.g. ' + DEFAULT_SIP_BIND)
    if env['AST_RECORD_CALLS'] not in ('yes', 'no'):
        raise ValueError('AST_RECORD_CALLS must be yes or no')
    settings.record_calls = env['AST_RECORD_CALLS'] == 'yes'
    return env, settings


def init_env(path, country_prefix=None, trunk_prefix=None, sip_domain=None, sip_domain_default=None):
    answers = {}
    for option, value in (('country-prefix', country_prefix), ('trunk-prefix', trunk_prefix),
                          ('sip-domain', sip_domain)):
        if value is None:
            if not sys.stdin.isatty():
                if option == 'trunk-prefix':
                    answers[option] = ''
                    continue
                raise ValueError('--' + option + ' is required when stdin is not a TTY')
            prompts = {
                'country-prefix': 'Country calling code, with + (e.g. +44): ',
                'trunk-prefix': 'Domestic trunk prefix digit, dropped when dialling internationally '
                                '(e.g. 0; empty if your country has no trunk prefix): ',
                'sip-domain': 'Address or DNS name the SIP phone uses to reach this host '
                              '(the VPN address for a phone over VPN or Tailscale)',
            }
            prompt = prompts[option]
            if option == 'sip-domain':
                prompt += (f' [{sip_domain_default}]' if sip_domain_default else '') + ': '
            value = input(prompt)
            if option == 'sip-domain' and not value and sip_domain_default:
                value = sip_domain_default
        answers[option] = value
    values = {'PHONELET_AMI_SECRET': secrets.token_urlsafe(32),
              'PHONELET_API_KEY': secrets.token_urlsafe(32),
              'AST_SIP_PASSWORD': secrets.token_urlsafe(32),
              'AST_COUNTRY_PREFIX': answers['country-prefix'],
              'AST_TRUNK_PREFIX': answers['trunk-prefix'],
              'PHONELET_SIP_DOMAIN': answers['sip-domain']}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write('# See docs/install.md; optional settings are listed in .env.example.\n')
            for key, value in values.items():
                # Quote answers as data so a malformed answer cannot introduce dotenv entries.
                escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('\r', r'\r').replace('\n', r'\n')
                stream.write(f'{key}="{escaped}"\n')
        configuration(path)
    except BaseException:
        path.unlink()
        raise
    print(f'Created {path}; read the SIP password for the phone with: grep AST_SIP_PASSWORD {shlex.quote(str(path))}')


def write_runtime_environment(stage, settings):
    """Render one canonical systemd file from the exact validated app values."""
    destination = stage / 'runtime.env'
    lines = []
    for key, value in settings.model_dump().items():
        text = str(value)
        if any(c in text for c in '\r\n\0'):
            raise ValueError('Runtime environment values must be single-line strings')
        escaped = text.replace('\\', '\\\\').replace('"', '\\"')
        lines.append('PHONELET_' + key.upper() + '="' + escaped + '"\n')
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.writelines(lines)
    return destination


def render(stage, env, *, destination=None):
    destination = stage / 'rendered' if destination is None else destination
    destination.mkdir(parents=True, exist_ok=True)
    for name in CONFIGS:
        template = stage / 'asterisk' / (name + '.tpl')
        if template.exists():
            content = template.read_text()
            if not env['AST_TRUNK_PREFIX']:
                content = LOCAL_NUMBER_RULE.sub('', content)
                if '${AST_TRUNK_PREFIX}' in content:
                    raise ValueError(name + '.tpl uses AST_TRUNK_PREFIX outside a local number rule')
            content = AST_PLACEHOLDER.sub(lambda m: env[m[1]], content)
        else:
            content = (stage / 'asterisk' / name).read_text()
        (destination / name).write_text(content)
    return destination


def check_sip_bind_address(bind):
    """Refuse a SIP bind address this host does not have now.

    pjsip leaves its UDP transport unbound when the address is missing at start, and Asterisk keeps running.
    """
    address = bind.partition(':')[0]
    if address == '0.0.0.0':
        return
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((address, 0))
        except OSError:
            raise RuntimeError('AST_SIP_BIND address ' + address + ' is not on this host; bind only to an '
                               'address present at boot, or keep 0.0.0.0 and use the firewall') from None


def udp_transport_section(pjsip_conf):
    match = re.search(r'(?ms)^\[transport-udp\]\n.*?(?=^\[|\Z)', pjsip_conf)
    return match.group(0).strip() if match else ''


def ami_messages(connection):
    """AMI messages read from a blocking socket; the banner line carries no header and is skipped."""
    buffer = b''
    while True:
        block, found, rest = buffer.partition(b'\r\n\r\n')
        if found:
            buffer = rest
            yield parse_message(block)
        elif len(buffer) > MAX_MESSAGE_BYTES:
            raise RuntimeError('AMI message too large')
        elif chunk := connection.recv(8192):
            buffer += chunk
        else:
            raise RuntimeError('AMI connection closed')


def require_durable_sms_driver(settings):
    """Require authenticated AMI and the loaded durable-ingress driver capability."""
    with socket.create_connection((settings.ami_host, settings.ami_port), timeout=5) as connection:
        connection.settimeout(5)
        messages = ami_messages(connection)
        connection.sendall(encode_action({'Action': 'Login', 'Username': settings.ami_username,
                                          'Secret': settings.ami_secret, 'Events': 'off'}))
        if next(messages).get('Response') != 'Success':
            raise RuntimeError('AMI authentication failed')
        connection.sendall(encode_action({'Action': DEVICE_LIST_ACTION, 'Device': DEFAULT_DONGLE_DEVICE}))
        for message in itertools.islice(messages, MAX_LIST_MESSAGES):
            if message.get('Response') == 'Error' or message.get('Event') == DEVICE_LIST_ACTION + 'Complete':
                break
            if (message.get('Device') == DEFAULT_DONGLE_DEVICE
                    and sms_journal_ok(message.get('SMSJournalVersion'), message.get('SMSJournalStatus'))):
                return
        raise RuntimeError('Durable SMS driver capability absent or journal failed')


def smoke(settings, url):
    endpoint = cli('pjsip show endpoint ' + SIP_ENDPOINT)
    if not re.search(r'Endpoint:\s+' + re.escape(SIP_ENDPOINT) + r'(?:[/\s]|$)', endpoint):
        raise RuntimeError('Required SIP endpoint is absent')
    manager = cli('manager show user ' + AMI_USER)
    # Not 'There is no manager called phonelet'.
    if not re.search(r'^\s*username:\s*' + re.escape(AMI_USER) + r'\s*$', manager, re.M):
        raise RuntimeError('Required AMI user is absent')
    permissions = re.search(r'^\s*write perm:\s*(.*)$', manager, re.M | re.I)
    granted = set(re.split(r'[,\s]+', permissions[1].strip())) if permissions else set()
    # reporting: DongleShowDevices (modem status, preflight); message: MessageSend (SIP delivery).
    missing = [name for name in ('reporting', 'message') if name not in granted]
    if missing:
        raise RuntimeError('AMI user lacks write permission: ' + ','.join(missing))
    if not re.search(r'^\s*' + re.escape(DEFAULT_DONGLE_DEVICE) + r'\s', cli('dongle show devices'), re.M):
        raise RuntimeError('Required dongle device is absent')
    # software_ready covers AMI login and the driver journal; network registration is
    # proven by the owner's test SMS and call, so an unregistered modem (503) passes.
    request = urllib.request.Request(url, headers={'X-API-Key': settings.api_key})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:  # 503 still carries the readiness report
        with error:
            status, body = error.code, error.read()
    try:
        readiness = json.loads(body)
    except ValueError:
        readiness = None
    if status not in (200, 503) or not isinstance(readiness, dict) or readiness.get('software_ready') is not True:
        raise SoftwareNotReady(readiness if status in (200, 503) and isinstance(readiness, dict) else None)
    return readiness


def wait_until_ready(settings, url):
    """Poll smoke for SMOKE_ATTEMPTS polls, longer while the first journal pass makes progress.

    A poll without a readiness report (a timeout, a failed Asterisk check) keeps the last
    report's verdict; an inactive Phonelet after the first SMOKE_ATTEMPTS polls fails at once.
    """
    started = printed = grown = monotonic()
    records = None  # ingress_records of the last report, while it was in the first pass
    attempt = 0
    while True:
        attempt += 1
        active = False
        try:
            run('systemctl', 'is-active', '--quiet', 'phonelet')
            active = True
            return smoke(settings, url)
        except Exception as error:
            now = monotonic()
            report = getattr(error, 'readiness', None)
            if report is not None:
                count = report.get('ingress_records')
                if report.get('ingress_first_pass') is not True or type(count) is not int:
                    records = None
                else:
                    if records is not None and count > records:  # a first sighting is not growth
                        grown = now
                    records = count
            if attempt >= SMOKE_ATTEMPTS and not (active and records is not None and
                                                  now - grown < FIRST_PASS_STALL_SECONDS):
                raise
            if now - started >= FIRST_PASS_MAX_SECONDS:
                raise RuntimeError(f'First journal pass still running after {FIRST_PASS_MAX_SECONDS} s') from error
            if records is not None and now - printed >= FIRST_PASS_PROGRESS_SECONDS:
                print(f'First journal pass: {records} records', flush=True)
                printed = now
            time.sleep(SMOKE_RETRY_SECONDS)


def check_database_schema(database):
    """Inspect the live schema without creating a database or WAL sidecars."""
    if not database.exists():
        return
    wal_exists = database.with_name(database.name + '-wal').exists()
    if wal_exists and not database.with_name(database.name + '-shm').exists():
        print('Warning: schema not checked: database WAL has no shared-memory file; leaving files untouched')
        return
    # With no journals, immutable avoids creating empty WAL/SHM files for a closed WAL
    # database. Existing journals must instead be read through normal SQLite locking.
    journals_exist = wal_exists or database.with_name(database.name + '-journal').exists()
    uri = database.as_uri() + '?mode=ro' + ('' if journals_exist else '&immutable=1')
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            has_objects = version == 0 and bool(connection.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0])
            Database.check_schema_version(database, version, has_objects)
    except sqlite3.Error:
        raise SystemExit(f'Cannot check schema of database {database}; '
                         'see docs/operations.md, Recreating the database') from None


def print_summary(revision, readiness, env, root, pjsip_conf, settings, ready_url):
    def yes_no(value):
        return 'yes' if value is True else 'no' if value is False else 'unknown'

    transport = udp_transport_section(pjsip_conf)
    protocol = re.search(r'^protocol\s*=\s*(\S+)', transport, re.M)[1]
    port = re.search(r'^bind\s*=\s*[^:\s]+(?::(\d+))?', transport, re.M)[1] or DEFAULT_SIP_PORT
    print('Summary:')
    print('  Release revision: ' + revision)
    print('  Software ready: ' + yes_no(readiness.get('software_ready')))
    modem_ready = '  Modem ready: ' + yes_no(readiness.get('modem_ready'))
    request = urllib.request.Request(ready_url.rsplit('/', 1)[0] + '/api/modem',
                                     headers={'X-API-Key': settings.api_key})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            modem = json.loads(response.read())
        if not isinstance(modem, dict):
            raise ValueError('Invalid modem status')
    except Exception as error:
        # Exception text can contain response data or credentials; name only the error kind/status.
        reason = f'HTTP {error.code}' if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        print(modem_ready)
        print(f'  Modem status: unavailable ({reason})')
    else:
        print(modem_ready + '; registered: ' + yes_no(modem.get('network_registered')))
        operator = ''.join(char for char in str(modem.get('operator') or '') if char.isprintable())
        if operator:
            print('  Operator: ' + operator)
        rssi = modem.get('signal_rssi')
        if rssi != RSSI_UNKNOWN and modem.get('signal_dbm') is not None:
            print(f'  Signal: {modem["signal_dbm"]} dBm')
        elif rssi not in (None, RSSI_UNKNOWN):
            print(f'  Signal RSSI: {rssi}')
    print('  SIP server: ' + env['PHONELET_SIP_DOMAIN'])
    print(f'  SIP port: {port}; transport: {protocol}')
    print('  SIP user: ' + env['AST_SIP_USERNAME'])
    print('  SIP password: grep AST_SIP_PASSWORD ' + shlex.quote(str(root / '.env')))


def module_running(name):
    """True when 'module show like' lists the module as Running (not merely loaded)."""
    return re.search(r'\d\s+Running\b', cli('module show like ' + name)) is not None


def reload_asterisk(restart):
    if restart:
        # asterisk.service allows 5 starts in 10 minutes; a deploy and its rollback restart twice.
        run('systemctl', 'reset-failed', 'asterisk')
        run('systemctl', 'restart', 'asterisk', timeout=120)  # its unit may take 45 s to stop
    else:
        for command in ('dialplan reload', 'module reload res_pjsip.so', 'manager reload',
                        'dongle reload now', 'module reload cdr', 'module reload cdr_manager',
                        'module reload cdr_custom.so'):
            cli(command)
        # cdr.conf has no [csv]: a reload unregisters a running cdr_csv. After a restart it is
        # Not Running, and a reload would only say it is not initialized.
        if module_running('cdr_csv.so'):
            cli('module reload cdr_csv.so')


def running_noload_modules(modules_conf):
    """Modules our modules.conf noloads that still run: a noload acts only at the next start."""
    names = re.findall(r'^noload\s*=>?\s*([\w.-]+\.so)\s*$', modules_conf.read_text(), re.M | re.I)
    return [name for name in names if module_running(name)]


def snapshot_database(source, destination):
    fd, temporary = tempfile.mkstemp(prefix='.db-snapshot-', dir=destination.parent)
    os.close(fd)
    try:
        with sqlite3.connect(source.absolute().as_uri() + '?mode=ro', uri=True) as source_db:
            with sqlite3.connect(temporary) as target:
                source_db.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Database rollback snapshot failed integrity check')
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_database_for_rollback(database, previous_snapshot):
    """Refuse the previous app unless the current database schema is proven compatible with it."""
    with sqlite3.connect(previous_snapshot) as before, sqlite3.connect(database) as after:
        old_version = before.execute('PRAGMA user_version').fetchone()[0]
        new_version = after.execute('PRAGMA user_version').fetchone()[0]
        if new_version != old_version:
            raise RuntimeError('Database schema requires a compatible application; current data preserved')
        if after.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Current database failed integrity check; preserved for recovery')


def remove_old_releases(stage, previous_unit):
    """Keep the running release and the previous one (the rollback target); remove other releases."""
    keep = {stage.resolve()}
    if previous_unit.exists():
        previous = re.findall(r'^WorkingDirectory=(.+)$', previous_unit.read_text(), re.M)
        if not previous:
            print('Warning: the previous drop-in names no release; old releases kept')
            return
        keep.add(Path(previous[-1].strip()).resolve())
    for release in stage.parent.iterdir():
        if (not RELEASE_NAME.fullmatch(release.name) or release.is_symlink() or not release.is_dir()
                or release.resolve() in keep):
            continue
        try:
            if time.time() - release.stat().st_mtime >= STAGING_GRACE_SECONDS:
                shutil.rmtree(release)
        except OSError as error:
            print(f'Warning: could not remove old release {release}: {describe(error)}')


def redact(text, secrets):
    # A short secret is not replaced elsewhere, so mask every secret field whatever its length,
    # before a replacement can change a field name.
    def mask_secret_field(field):
        if field[3] and SECRET_NAME.search(field[2]):
            return field[1] + REDACTED + field[4]
        return field[0]
    text = CONFIG_FIELD.sub(mask_secret_field, text)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def report_changes(targets, env, settings, etc):
    """Print what deployment would replace; Asterisk configs get a diff without secret values."""
    secrets = {v for k, v in env.items() if SECRET_NAME.search(k)}
    secrets |= {str(v) for k, v in settings.model_dump().items() if SECRET_NAME.search(k)}
    files = []
    for source, target in targets:
        old = target.read_bytes().decode(errors='replace') if target.exists() else None
        new = source.read_bytes().decode(errors='replace')
        files.append((source, target, old, new))
        if target.parent == etc:
            # Live configs may still hold credentials that .env has already rotated.
            for text in (old or '', new):
                secrets |= {m[3] for m in CONFIG_FIELD.finditer(text) if SECRET_NAME.search(m[2])}
    secrets = {secret for secret in secrets if len(secret) >= MIN_MASKED_SECRET_LENGTH}
    for source, target, old, new in files:
        if old is None:
            state = 'new'
        elif old == new:
            state = 'unchanged'
        else:
            state = 'changed'
        print(str(target) + ': ' + state)
        if target.parent == etc and old != new:
            old, new = redact(old or '', secrets), redact(new, secrets)
            if old == new:
                print('(only masked secret values differ)')
            for line in difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(target), str(source), lineterm=''):
                print(line.rstrip('\n'))


def refuse_unfinished_wifi_change(settings):
    """A running Wi-Fi change, or one Phonelet rolls back before it serves HTTP: either can outlast the smoke wait."""
    marker = Path(settings.wifi_state_dir) / WIFI_MARKER_NAME
    if marker.exists():
        raise RuntimeError('Unfinished Wi-Fi change (' + str(marker) + '): wait for the running change to finish '
                           '(the marker disappears); if Phonelet is not running, start it and wait for /health; '
                           'or follow "Manual Wi-Fi rollback" in docs/sms-commands.md. Then deploy again')


def refuse_sandbox_read_only_paths(settings, stage):
    """Every directory Phonelet writes must stay writable under the SANDBOX of the drop-in."""
    def within(path, prefix):
        return path == prefix or path.startswith(prefix + '/')

    written = {'PHONELET_DB_PATH': Path(settings.db_path).parent,
               'PHONELET_RECORDINGS_DIR': Path(settings.recordings_dir),
               'PHONELET_WIFI_STATE_DIR': Path(settings.wifi_state_dir),
               'PHONELET_WIFI_NETPLAN_FILE': Path(settings.wifi_netplan_file).parent}
    for key, directory in written.items():
        # Relative to the service's working directory, the release; symlinks as the kernel follows them.
        path = os.path.realpath(stage / directory)
        if (any(within(path, prefix) for prefix in SANDBOX_READ_ONLY)
                and not any(within(path, prefix) for prefix in SANDBOX_READ_WRITE)):
            raise ValueError(key + ' is read-only for phonelet.service (ProtectSystem/ProtectHome): ' + path)
    netplan_dir = Path(settings.wifi_netplan_file).parent
    if not netplan_dir.is_dir():
        # ReadWritePaths= skips a missing directory: one created later stays read-only until a restart.
        print('Warning: ' + str(netplan_dir) + ' does not exist: the Wi-Fi command cannot write its netplan file; '
              'create it (install netplan.io) and restart Phonelet')


def install_file(source, target, mode, owner, suffix='.phonelet-new'):
    """Replace target atomically with source's content, mode and owner (uid, gid) or None to keep ours.

    The temporary file next to target is created with its final mode and never follows a symlink;
    a failure removes it when it can.
    """
    temporary = target.with_name(target.name + suffix)
    try:
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as destination:
            os.fchmod(destination.fileno(), mode)
            with source.open('rb') as content:
                shutil.copyfileobj(content, destination)
        if owner is not None:
            os.chown(temporary, *owner)
        os.replace(temporary, target)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def deploy(stage, root, etc, systemd, helper, *, allow_restart=False, check=False, force_restart=False,
           env_file=None, pre_upgrade=False):
    """Pre-upgrade mode checks candidate files and host resources without querying Asterisk or changing services."""
    revision_file = stage / 'REVISION'
    revision = revision_file.read_text().strip() if revision_file.exists() else 'unknown'
    if not re.fullmatch(r'(?:[0-9a-f]{12,40}(?:-dirty)?|unknown)', revision):
        raise ValueError('Invalid release revision')
    if not pre_upgrade:
        print('Release revision: ' + revision)
    env, settings = configuration(env_file if env_file is not None else root / '.env')
    host = WILDCARD_LOOPBACK.get(settings.api_host, settings.api_host)
    if ':' in host:
        host = '[' + host + ']'
    ready_url = f'http://{host}:{settings.api_port}/ready'
    if settings.sms_ingress_dir != SMS_INGRESS_DIR or settings.cdr_log_path != CDR_LOG_PATH:
        raise ValueError('Ingress paths must match the pinned driver and CDR mapping')
    if settings.ami_host not in ('127.0.0.1', 'localhost'):
        raise ValueError('AMI listens on 127.0.0.1 only (asterisk/manager.conf): set PHONELET_AMI_HOST=127.0.0.1')
    # The service runs in the release directory: a relative path would name another directory.
    if not Path(settings.wifi_state_dir).is_absolute():
        raise ValueError('PHONELET_WIFI_STATE_DIR must be an absolute path')
    if not Path(settings.db_path).is_absolute():
        raise ValueError('Database path must be absolute and not a symlink')
    refuse_sandbox_read_only_paths(settings, stage)
    refuse_unfinished_wifi_change(settings)
    check_sip_bind_address(env['AST_SIP_BIND'])
    rendered = render(stage, env)
    runtime_env = write_runtime_environment(stage, settings)
    python = str(stage / '.venv/bin/python')
    run(python, '-m', 'pip', 'check')
    run(python, '-m', 'compileall', '-q', str(stage / 'src'))
    for command in ('asterisk', 'systemctl', 'lame'):
        if pre_upgrade and command == 'asterisk':
            continue
        if not shutil.which(command):
            raise RuntimeError('Required executable missing: ' + command)
    for name in ('extensions', 'pjsip', 'manager'):
        if not re.search(r'^\s*#include\s+["<]?' + name + r'_custom\.conf', (etc / (name + '.conf')).read_text(), re.M):
            raise RuntimeError('Missing Asterisk include: ' + name)
    service_user = run('systemctl', 'show', '--property=User', '--value', 'phonelet').strip()
    if service_user not in ('', 'root'):
        raise RuntimeError('Default /root releases require the existing root Phonelet service')
    # An inactive or failed Phonelet is fine: the deploy starts it.
    if run('systemctl', 'show', '--property=LoadState', '--value', 'phonelet').strip() != 'loaded':
        raise RuntimeError('Phonelet service is not loaded: bootstrap the base unit first')
    if not pre_upgrade:
        run('systemctl', 'is-active', '--quiet', 'asterisk')
        # Pinned patched driver must already be installed, never auto-build on deployment.
        if 'DongleSendSMSBase64' not in cli('core show application DongleSendSMSBase64'):
            raise RuntimeError('Install the pinned outgoing SMS driver patch first')
        # [sms-send] checks the number with REGEX(), sends every SMS with TryExec() and reports one the
        # driver rejects with Originate(). A missing one prints "Command 'core show ... NAME' failed.":
        # match the info header.
        for kind, name, module_name in (('Function', 'REGEX', 'func_strings'), ('Application', 'TryExec', 'app_exec'),
                                        ('Application', 'Originate', 'app_originate')):
            if f"Info about {kind} '{name}'" not in run('asterisk', '-rx', f'core show {kind.lower()} {name}'):
                raise RuntimeError(f'Required {module_name} module not loaded: no {name} {kind.lower()}')
        if 'cdr_custom.so' not in cli('module show like cdr_custom.so'):
            raise RuntimeError('Required cdr_custom module not loaded')
    for key in ('AST_DONGLE_DATA', 'AST_DONGLE_AUDIO'):
        if not Path(env[key]).is_char_device():
            raise RuntimeError('Missing character device: ' + key)
    # The same modem (MDM) device under another name (a second link, a /dev/ttyUSB* path) is as deaf.
    if os.path.exists(MDM_LINK) and os.stat(env['AST_DONGLE_DATA']).st_rdev == os.stat(MDM_LINK).st_rdev:
        raise RuntimeError('AST_DONGLE_DATA is the modem (MDM) port ' + MDM_LINK + ' (interface 00): '
                           + DATA_PORT_FIX + ' in .env')
    for directory in (Path(settings.recordings_dir), Path(settings.sms_ingress_dir), Path(settings.cdr_log_path).parent):
        if not directory.is_dir():
            raise RuntimeError('Bootstrap durable storage directory first: ' + str(directory))
        if shutil.disk_usage(directory).free < settings.min_free_bytes:
            raise RuntimeError('Insufficient free disk space')
    # Compare installed files: this check also works while Asterisk is stopped.
    transport_changed = (udp_transport_section((rendered / 'pjsip_custom.conf').read_text())
                         != udp_transport_section((etc / 'pjsip_custom.conf').read_text()))
    # --restart-asterisk: e.g. cdr_csv, which no reload drops while Message/ast_msg_queue holds a CDR.
    restart = transport_changed or force_restart
    if transport_changed and not (allow_restart or force_restart):
        if pre_upgrade:
            raise RuntimeError('SIP transport changed; retry install.sh with --allow-asterisk-restart '
                               '(direct --check-env: --allow-transport-change)')
        if not check:
            raise RuntimeError('SIP transport changed; retry in a maintenance window with --allow-asterisk-restart')
        print('SIP transport changes: deployment needs --allow-asterisk-restart')
    if not pre_upgrade:
        require_durable_sms_driver(settings)
        if not re.search(r'^0 active calls\s*$', cli('core show channels count'), re.M):
            raise RuntimeError('Deployment requires zero active calls')
        # After an outgoing SMS Message/ast_msg_queue holds a CDR until Asterisk stops: no reload drops csv.
        if not restart and re.search(r'^\s+csv\s*$', cli('cdr show status'), re.M) and \
                re.search(r'^\S+/\S+\s', cli('cdr show active'), re.M):
            raise RuntimeError('cdr_csv cannot unregister while a CDR is active: deploy with --restart-asterisk')
    database = Path(settings.db_path)
    if database.is_symlink() or not database.is_absolute():
        raise ValueError('Database path must be absolute and not a symlink')
    database_existed = database.exists()
    check_database_schema(database)
    snapshots_size = database.stat().st_size * DATABASE_COPIES_RESERVED if database_existed else 0
    required_space = snapshots_size + settings.min_free_bytes
    if shutil.disk_usage(stage).free < required_space:
        raise RuntimeError('Insufficient space for database rollback snapshot')
    # Snapshot every file we mutate, including absence. Old code/venv remain untouched.
    dropin = systemd / 'phonelet.service.d/20-phonelet-release.conf'
    unit = stage / 'phonelet-release.conf'
    targets = [(rendered / name, etc / name) for name in CONFIGS]
    targets += [(unit, dropin), (stage / 'scripts/convert-recording.py', helper)]
    # The pre-upgrade check refuses these too, before Asterisk is replaced.
    for _, target in targets:
        if target.is_symlink():
            raise RuntimeError('Refusing symlink deployment target: ' + str(target))
    if pre_upgrade:
        print('Pre-upgrade check passed')
        return
    unit.write_text('\n'.join([
        '[Service]',
        'UMask=0077',
        *SANDBOX,
        f'TimeoutStopSec={UNIT_STOP_TIMEOUT_SECONDS}',
        f'WorkingDirectory={stage}',
        'EnvironmentFile=',
        f'EnvironmentFile={runtime_env}',
        f'Environment=PYTHONPATH={stage / "src"}',
        'ExecStart=',
        f'ExecStart={python} -m uvicorn phonelet.main:app --host {settings.api_host} --port {settings.api_port}',
        '',
    ]))
    if check:
        # Read-only mode: only the release directory itself has been written.
        report_changes(targets, env, settings, etc)
        if not restart:
            for name in running_noload_modules(rendered / 'modules.conf'):
                print('Deploy unloads running module ' + name)
        print('Check passed; no changes made')
        return
    backup = stage / 'rollback'
    backup.mkdir(mode=0o700)
    saved = []
    for i, (_, target) in enumerate(targets):
        copy = backup / str(i)
        if target.exists():
            shutil.copy2(target, copy)
            owner = target.stat()
            os.chown(copy, owner.st_uid, owner.st_gid)  # the rollback restores it with this owner
        saved.append((target, copy))
    (backup / 'manifest.json').write_text(json.dumps([{'path': str(t), 'backup': str(b), 'existed': b.exists()} for t, b in saved]))
    previous_unit = dict(saved)[dropin]
    database_backup = backup / 'phonelet.db'
    failed_release_snapshot = backup / 'failed-release.db'
    with DeploymentSignals() as signals:
        try:
            os.sync()  # the rollback copies above survive a power loss
            stop_application()
            # A change started after preflight: the rollback below starts the previous app, which finishes it.
            refuse_unfinished_wifi_change(settings)
            if database_existed:
                snapshot_database(database, database_backup)
            for source, target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target == helper:
                    mode = 0o755  # executed by Asterisk's recording command
                else:
                    mode = 0o640  # may carry credentials
                if target.exists():
                    owner = target.stat()
                elif target.parent == etc:
                    owner = (etc / 'extensions.conf').stat()
                else:
                    owner = None
                install_file(source, target, mode, None if owner is None else (owner.st_uid, owner.st_gid))
            run('systemctl', 'daemon-reload')
            reload_asterisk(restart)
            if not restart:
                # In file order, a module before the one it needs. Only forward: a rollback leaves
                # unloaded modules out until a restart and never reads the restored samples.
                for name in running_noload_modules(rendered / 'modules.conf'):
                    cli('module unload ' + name)
            restart_application()
            readiness = wait_until_ready(settings, ready_url)
            # After a reload or a restart: a CDR backend cannot unregister while a CDR is active,
            # yet the reload reports success.
            if re.search(r'^\s+(csv|cdr_manager)\s*$', cli('cdr show status'), re.M):
                raise RuntimeError('cdr_csv or cdr_manager still logs CDR (calls were active): deploy again')
        except BaseException:
            signals.rolling_back = True
            errors = []

            def attempt(what, action):
                try:
                    action()
                    return True
                except Exception as error:
                    errors.append(f'{what}: {describe(error)}')
                    return False

            def kill_and_stop():
                run('systemctl', 'kill', '--signal=SIGKILL', 'phonelet')
                stop_application()

            try:
                stop_application()
                stopped = True
            except Exception as error:
                # A hung candidate under a drop-in without TimeoutStopSec, or a stuck stop job.
                print(f'Warning: could not stop application ({describe(error)}); sending SIGKILL')
                stopped = attempt('could not stop application; database untouched', kill_and_stop)
            if not stopped:
                database_compatible = False
            elif database_backup.exists():
                database_compatible = (
                    attempt('failed release database snapshot',
                            lambda: snapshot_database(database, failed_release_snapshot))
                    and attempt('database is not backward compatible; current history/checkpoints preserved',
                                lambda: prepare_database_for_rollback(database, database_backup)))
            elif not database_existed and database.exists():
                # A first deployment has no proven prior schema compatibility.
                attempt('new database snapshot failed', lambda: snapshot_database(database, failed_release_snapshot))
                errors.append('new database preserved; install a compatible release')
                database_compatible = False
            elif not database_existed:
                # No database before the deploy (a first deployment, or one moved aside to be
                # recreated): the previous release would create its own and notify the whole
                # retained journal and CDR again.
                errors.append('no database before the deploy; previous application left stopped')
                database_compatible = False
            else:
                database_compatible = True  # the candidate never opened it
            files_restored = True
            for target, copy in reversed(saved):
                temporary = target.with_name(target.name + '.phonelet-new')
                if not attempt('remove ' + str(temporary), lambda: temporary.unlink(missing_ok=True)):
                    print('Warning: temporary file remains: ' + str(temporary))
                try:
                    if copy.exists():
                        saved_copy = copy.stat()
                        # Its own temporary name: the forward one may be the file that could not be removed.
                        install_file(copy, target, stat.S_IMODE(saved_copy.st_mode),
                                     (saved_copy.st_uid, saved_copy.st_gid), suffix='.phonelet-restore')
                    else:
                        target.unlink(missing_ok=True)
                except Exception as error:
                    errors.append(f'restore {target}: {describe(error)}')
                    files_restored = False
            # Without the reload systemd would start the candidate's drop-in again.
            units_reloaded = attempt('systemd reload', lambda: run('systemctl', 'daemon-reload'))
            attempt('Asterisk recovery', lambda: reload_asterisk(restart))
            if stopped and database_compatible and files_restored and units_reloaded:
                attempt('previous application start', restart_application)
            if errors:
                raise RuntimeError('ROLLBACK INCOMPLETE: ' + ', '.join(errors))
            raise
    # Nothing is removed after a failure or a check; only after the new release is verified.
    try:
        remove_old_releases(stage, previous_unit)
    except Exception as error:
        print(f'Warning: old releases kept: {describe(error)}')
    try:
        run('systemctl', 'reset-failed', 'asterisk')  # the next deploy starts with the full start limit
    except Exception as error:
        print(f'Warning: systemctl reset-failed asterisk: {describe(error)}')
    os.sync()  # the new release, configs and drop-in survive a power loss
    try:
        print_summary(revision, readiness, env, root, (rendered / 'pjsip_custom.conf').read_text(), settings, ready_url)
    except Exception as error:
        print(f'Warning: summary unavailable ({type(error).__name__})')
    print('Deployment verified; rollback snapshot: ' + str(backup))


def open_deploy_lock(path):
    """The lock file, or the descriptor of it that install.sh already holds locked (PHONELET_DEPLOY_LOCK_FD).

    flock on the inherited descriptor succeeds (same open file description), so the lock stays held from
    the Asterisk upgrade through this deployment; anyone else still finds it busy.
    """
    inherited = os.environ.get('PHONELET_DEPLOY_LOCK_FD', '')
    if not inherited:
        return open(path, 'a')
    if not inherited.isdigit():
        raise SystemExit('Invalid PHONELET_DEPLOY_LOCK_FD')
    try:
        lock = os.fdopen(int(inherited), 'a')
    except OSError:
        raise SystemExit('PHONELET_DEPLOY_LOCK_FD is not an open descriptor') from None
    held, expected = os.fstat(lock.fileno()), os.stat(path)
    if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
        lock.close()
        raise SystemExit('PHONELET_DEPLOY_LOCK_FD is not ' + str(path))
    return lock


def render_configs(env, destination, *, bootstrap_wifi=False):
    """Render first-install configs without changing the host or the source templates."""
    if env.get('PHONELET_SMS_COMMAND_KEY') and not bootstrap_wifi:
        print('Warning: SMS commands enabled but Wi-Fi packages not selected; install netplan.io wpasupplicant iw '
              'manually or set PHONELET_BOOTSTRAP_WIFI=1.', file=sys.stderr)
    render(ASTERISK_DIR.parent, env, destination=destination)
    # The make samples pjsip.conf defines its own endpoints: keep only ours.
    (destination / 'pjsip.conf').write_text('#include "pjsip_custom.conf"\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--init-env', type=Path, metavar='FILE', help='create a private configuration file')
    mode.add_argument('--check-env', type=Path, metavar='FILE',
                      help='run static preflight with FILE and --stage before an Asterisk upgrade; '
                           'write only inside the staged release, change no services')
    mode.add_argument('--render-configs', type=Path, nargs=2, metavar=('FILE', 'DIR'),
                      help='validate FILE and render first-install Asterisk configs into DIR')
    parser.add_argument('--allow-transport-change', action='store_true',
                        help='with --check-env, permit a SIP transport configuration change before the upgrade')
    parser.add_argument('--bootstrap-wifi', action='store_true',
                        help='with --render-configs, Wi-Fi packages were selected for bootstrap')
    parser.add_argument('--country-prefix', help='country calling code, including +')
    parser.add_argument('--trunk-prefix', help='one domestic trunk-prefix digit; omit if there is none')
    parser.add_argument('--sip-domain', help='address or DNS name by which the SIP phone reaches this host')
    parser.add_argument('--sip-domain-default', help='proposed SIP address for the interactive prompt')
    parser.add_argument('--root', type=Path, default=Path('/root/phonelet'))
    parser.add_argument('--etc', type=Path, default=Path('/etc/asterisk'))
    parser.add_argument('--systemd', type=Path, default=Path('/etc/systemd/system'))
    parser.add_argument('--helper', type=Path, default=Path('/usr/local/lib/phonelet/convert-recording.py'))
    parser.add_argument('--allow-asterisk-restart', action='store_true',
                        help='permit an Asterisk restart when the SIP transport configuration changed')
    parser.add_argument('--restart-asterisk', action='store_true', help='restart Asterisk even if no SIP transport changed')
    parser.add_argument('--check', action='store_true', help='run every preflight check and show changes; change nothing')
    args = parser.parse_args(argv)
    if (args.check_env is not None or args.render_configs is not None) and (
            args.check or args.allow_asterisk_restart or args.restart_asterisk):
        parser.error('--check, --allow-asterisk-restart and --restart-asterisk are deployment-only options')
    if args.allow_transport_change and args.check_env is None:
        parser.error('--allow-transport-change requires --check-env')
    if args.bootstrap_wifi and args.render_configs is None:
        parser.error('--bootstrap-wifi requires --render-configs')
    if args.init_env is not None:
        try:
            init_env(args.init_env, args.country_prefix, args.trunk_prefix, args.sip_domain, args.sip_domain_default)
        except (ValueError, OSError) as error:
            # Print validation messages without a traceback (which could expose locals).
            raise SystemExit(str(error)) from None
        except (EOFError, KeyboardInterrupt):
            # Nothing is written before all answers are in; end the unfinished prompt line.
            raise SystemExit('\nCancelled: no .env written.') from None
        raise SystemExit(0)
    if args.render_configs is not None:
        env_file, destination = args.render_configs
        try:
            env, _ = configuration(env_file)
        except ValueError as error:
            raise SystemExit('Invalid environment file: ' + str(error)) from None
        try:
            render_configs(env, destination, bootstrap_wifi=args.bootstrap_wifi)
        except ValueError as error:
            raise SystemExit('Invalid Asterisk template: ' + str(error)) from None
        return
    if args.stage is None:
        parser.error('--stage is required unless --init-env or --render-configs is used')
    if args.check_env is not None:
        # install.sh already holds the deploy lock. Unlike a normal deploy, this mode must not
        # create a lock file outside the temporary release or change services.
        try:
            deploy(args.stage.resolve(), args.root, args.etc, args.systemd, args.helper,
                   pre_upgrade=True, allow_restart=args.allow_transport_change, env_file=args.check_env)
        except (ValueError, RuntimeError, subprocess.SubprocessError, OSError, SystemExit) as error:
            raise SystemExit('Pre-upgrade validation failed: ' + str(error) + '; Asterisk not changed') from None
        return
    # Serialize deployments; lock lives outside the release being replaced.
    with open_deploy_lock(args.root / '.deploy.lock') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Refusing another deployment: lock is busy: ' + str(args.root / '.deploy.lock')) from None
        deploy(args.stage.resolve(), args.root, args.etc, args.systemd, args.helper,
               allow_restart=args.allow_asterisk_restart, check=args.check, force_restart=args.restart_asterisk)


if __name__ == '__main__':
    main()
