"""Deploy-time Asterisk configuration: injection guard and rendered dialplan.

Pure file work on synthetic .env files and copies of asterisk/*.tpl in tmp_path;
nothing is installed and no Asterisk, network or host path is involved.
"""
import difflib
import importlib.util
import re
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from phonelet.constants import DEFAULT_DONGLE_DEVICE
from phonelet.cdr_events import CdrHandler, SMS_REPORT_MARKER, SMS_REPORT_TYPES
from phonelet.ingress import CDR_FIELDS

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = sorted((ROOT / 'asterisk').glob('*.tpl'))
# Every ${AST_*} placeholder the shipped templates use: a new one is covered automatically.
PLACEHOLDERS = sorted({name for template in TEMPLATES
                       for name in re.findall(r'\$\{(AST_[A-Z_]+)\}', template.read_text())})
# Check derived template inputs by changing their source setting in the file.
DERIVED_SOURCES = {'AST_AMI_SECRET': 'PHONELET_AMI_SECRET',
                   'AST_RECORDINGS_DIR': 'PHONELET_RECORDINGS_DIR',
                   'AST_MIN_FREE_BYTES': 'PHONELET_MIN_FREE_BYTES'}
CHECKED_KEYS = sorted({DERIVED_SOURCES.get(key, key) for key in PLACEHOLDERS} | {'PHONELET_SIP_DOMAIN'})
# Quoted dotenv values decode these escapes before configuration validates them.
TAILS = {'cr': r'\rb', 'lf': r'\nb', 'crlf': r'\r\n[general]', 'semicolon': ';b', 'hash': '#b'}
PATH_KEYS = {'PHONELET_RECORDINGS_DIR', 'AST_DONGLE_DATA', 'AST_DONGLE_AUDIO'}
BASE_ENV = {
    'PHONELET_AMI_SECRET': 'ami-secret-1', 'PHONELET_API_KEY': 'api-key-1',
    'AST_SIP_PASSWORD': 'sip-password-1', 'PHONELET_SIP_DOMAIN': '192.0.2.10',
    'AST_COUNTRY_PREFIX': '+1', 'AST_TRUNK_PREFIX': '0',
}


@pytest.fixture(scope='module')
def deploy():
    spec = importlib.util.spec_from_file_location('deploy_release', ROOT / 'scripts' / 'deploy-release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_env(directory, **overrides):
    values = {**BASE_ENV, **overrides}
    path = directory / '.env'
    path.write_text(''.join(f'{key}="{value}"\n' for key, value in values.items()))
    return path


def stage_with_templates(directory):
    shutil.copytree(ROOT / 'asterisk', directory / 'asterisk')
    return directory


def test_clean_environment_is_accepted(deploy, tmp_path):
    env, _ = deploy.configuration(write_env(tmp_path))
    assert env['AST_COUNTRY_PREFIX'] == '+1'
    assert env['AST_AMI_SECRET'] == BASE_ENV['PHONELET_AMI_SECRET']
    stage = stage_with_templates(tmp_path)
    rendered = deploy.render(stage, env)
    assert all('${AST_' not in path.read_text() for path in rendered.iterdir())
    assert 'secret=' + BASE_ENV['PHONELET_AMI_SECRET'] in (rendered / 'manager_custom.conf').read_text()
    assert f'[{DEFAULT_DONGLE_DEVICE}]' in (rendered / 'dongle.conf').read_text()
    dialplan = (rendered / 'extensions_custom.conf').read_text()
    assert f'Dial(Dongle/{DEFAULT_DONGLE_DEVICE}/' in dialplan
    assert f'DongleSendSMSBase64({DEFAULT_DONGLE_DEVICE},' in dialplan
    assert 'permit=127.0.0.1/255.255.255.255' in (rendered / 'manager_custom.conf').read_text()


@pytest.mark.parametrize('tail', TAILS.values(), ids=TAILS.keys())
@pytest.mark.parametrize('key', CHECKED_KEYS)
def test_unsafe_characters_in_any_substituted_value_are_rejected(deploy, tmp_path, key, tail):
    # Paths start with "/", so only the appended character can make the value unsafe.
    value = '/srv/a' if key in PATH_KEYS else 'abc'
    if key == 'AST_SIP_PASSWORD':
        value = 'synthetic123'
    value += tail
    overrides = {key: value}
    if '\\r' in tail or '\\n' in tail:
        message = 'Multiline environment values are not supported'
    elif key in ('PHONELET_MIN_FREE_BYTES', 'PHONELET_SIP_DOMAIN'):
        message = key
    else:
        derived = next((name for name, source in DERIVED_SOURCES.items() if source == key), key)
        message = 'Unsafe configuration value: ' + derived
    with pytest.raises(ValueError, match=message):
        deploy.configuration(write_env(tmp_path, **overrides))


def test_only_ast_placeholders_are_substituted(deploy, tmp_path):
    stage = stage_with_templates(tmp_path)
    (stage / 'asterisk' / 'extensions_custom.conf.tpl').write_text(
        'exten => _X.,1,Dial(Dongle/dongle0/${EXTEN:1})\n'
        ' same => n,Set(WHO=${CALLERID(num)}@${AST_SIP_USERNAME} ${PHONELET_API_KEY} ${UNKNOWN})\n')
    env, _ = deploy.configuration(write_env(tmp_path))
    assert env['PHONELET_API_KEY'] == 'api-key-1'  # application secrets share the dictionary
    rendered = (deploy.render(stage, env) / 'extensions_custom.conf').read_text()
    assert rendered == ('exten => _X.,1,Dial(Dongle/dongle0/${EXTEN:1})\n'
                        ' same => n,Set(WHO=${CALLERID(num)}@phone ${PHONELET_API_KEY} ${UNKNOWN})\n')


@pytest.mark.parametrize('template', TEMPLATES, ids=lambda path: path.name)
@pytest.mark.parametrize('record_calls,trunk', [('no', '0'), ('yes', '0'), ('no', '')])
def test_rendering_preserves_asterisk_variables(deploy, tmp_path, template, record_calls, trunk):
    stage = stage_with_templates(tmp_path)
    env, _ = deploy.configuration(write_env(tmp_path, AST_RECORD_CALLS=record_calls, AST_TRUNK_PREFIX=trunk))
    rendered = (deploy.render(stage, env) / template.name.removesuffix('.tpl')).read_text()
    assert '${AST_' not in rendered
    # ${EXTEN}, ${CALLERID(num)}, ${BASE64_ENCODE(...)}, ${UNIQUEID} ... stay for Asterisk, byte for byte.
    patterns = ['.*?'.join(map(re.escape, re.split(r'\$\{AST_[A-Z_]+\}', line)))
                for line in template.read_text().splitlines()]
    for line in rendered.splitlines():
        assert any(re.fullmatch(pattern, line) for pattern in patterns), line
    assert not any(marker in rendered for marker in ('; IF ', '; ELSE', '; ENDIF'))


@pytest.mark.parametrize('template', TEMPLATES, ids=lambda path: path.name)
def test_templates_have_no_preprocessor_markers(template):
    assert not re.search(r'^\s*;\s*(?:IF|ELSE|ENDIF)\b', template.read_text(), re.M)


def without_key(path, key):
    path.write_text(''.join(line for line in path.read_text().splitlines(True) if not line.startswith(key + '=')))
    return path


@pytest.mark.parametrize('value', [None, ''])
def test_country_prefix_is_required(deploy, tmp_path, value):
    path = write_env(tmp_path, AST_COUNTRY_PREFIX=value or '')
    if value is None:
        without_key(path, 'AST_COUNTRY_PREFIX')
    with pytest.raises(ValueError, match='AST_COUNTRY_PREFIX is required'):
        deploy.configuration(path)


@pytest.mark.parametrize('value', [None, ''])
def test_trunk_prefix_is_optional(deploy, tmp_path, value):
    # Countries without a trunk prefix leave it empty or out: the local rule is then off.
    path = write_env(tmp_path, AST_TRUNK_PREFIX=value or '')
    if value is None:
        without_key(path, 'AST_TRUNK_PREFIX')
    env, _ = deploy.configuration(path)
    assert env['AST_TRUNK_PREFIX'] == ''


@pytest.mark.parametrize('key,value', [('AST_COUNTRY_PREFIX', '1'), ('AST_COUNTRY_PREFIX', '+'),
                                     ('AST_TRUNK_PREFIX', '00'), ('AST_TRUNK_PREFIX', '01'),
                                     ('AST_TRUNK_PREFIX', '+'), ('AST_TRUNK_PREFIX', ' ')])
def test_local_number_prefix_format_is_validated(deploy, tmp_path, key, value):
    with pytest.raises(ValueError, match='Unsafe configuration value: ' + key):
        deploy.configuration(write_env(tmp_path, **{key: value}))


def sip_bind(deploy, tmp_path, env):
    rendered = (deploy.render(stage_with_templates(tmp_path), env) / 'pjsip_custom.conf').read_text()
    return re.findall(r'^bind=(.*)$', deploy.udp_transport_section(rendered), re.M)


def test_sip_bind_defaults_to_all_interfaces_on_5060(deploy, tmp_path):
    # Installations without the key keep the former fixed bind.
    env, _ = deploy.configuration(without_key(write_env(tmp_path), 'AST_SIP_BIND'))
    assert env['AST_SIP_BIND'] == '0.0.0.0:5060'
    assert sip_bind(deploy, tmp_path, env) == ['0.0.0.0:5060']


@pytest.mark.parametrize('value', ['192.0.2.10', '192.0.2.10:5060', '100.64.0.1:5080', '0.0.0.0:65535'])
def test_sip_bind_takes_an_ipv4_address_with_an_optional_port(deploy, tmp_path, value):
    env, _ = deploy.configuration(write_env(tmp_path, AST_SIP_BIND=value))
    assert sip_bind(deploy, tmp_path, env) == [value]


@pytest.mark.parametrize('value,message', [
    ('', 'Unsafe'), ('localhost', 'Unsafe'), ('192.0.2.10:', 'Unsafe'), ('[::1]:5060', 'Unsafe'),
    ('::', 'Unsafe'), ('192.0.2.10:5060:1', 'Unsafe'), ('192.0.2.10 :5060', 'Unsafe'),
    ('192.0.2.10:123456', 'Unsafe'), ('192.0.2.256', 'IPv4'), ('192.0.2', 'IPv4'), ('192.0.2.1.5', 'IPv4'),
    ('192.0.2.010', 'IPv4'), ('192.0.2.10:0', 'IPv4'), ('192.0.2.10:65536', 'IPv4'),
])
def test_bad_sip_bind_is_refused(deploy, tmp_path, value, message):
    expected = {'Unsafe': 'Unsafe configuration value: AST_SIP_BIND',
                'IPv4': 'AST_SIP_BIND must be an IPv4 address'}[message]
    with pytest.raises(ValueError, match=expected):
        deploy.configuration(write_env(tmp_path, AST_SIP_BIND=value))


@pytest.mark.parametrize('key', ['AST_DONGLE_RXGAIN', 'AST_DONGLE_TXGAIN'])
@pytest.mark.parametrize('value', ['-32767', '-3', '-1', '0', '+1', '3', '32767'])
def test_audio_gain_is_rendered_from_environment(deploy, tmp_path, key, value):
    stage = stage_with_templates(tmp_path)
    env, _ = deploy.configuration(write_env(tmp_path, **{key: value}))
    dongle = (deploy.render(stage, env) / 'dongle.conf').read_text()
    field = 'rxgain' if key == 'AST_DONGLE_RXGAIN' else 'txgain'
    assert f'\n{field}={value}\n' in dongle


@pytest.mark.parametrize('key', ['AST_DONGLE_RXGAIN', 'AST_DONGLE_TXGAIN'])
@pytest.mark.parametrize('value', ['', 'abc', '1.5', '3x', '1 2', '-32768', '32768', '2147483648'])
def test_invalid_audio_gain_fails_preflight(deploy, tmp_path, key, value):
    with pytest.raises(ValueError, match=key + ' must be an integer between -32767 and 32767'):
        deploy.configuration(write_env(tmp_path, **{key: value}))


@pytest.mark.parametrize('value', ['', 'YES', 'true', '1', 'maybe'])
def test_recording_switch_requires_yes_or_no(deploy, tmp_path, value):
    with pytest.raises(ValueError, match='AST_RECORD_CALLS must be yes or no'):
        deploy.configuration(write_env(tmp_path, AST_RECORD_CALLS=value))


@pytest.mark.parametrize('record_calls', ['no', 'yes'])
def test_app_learns_the_recording_switch_only_from_asterisk(deploy, tmp_path, record_calls):
    _, settings = deploy.configuration(write_env(tmp_path, AST_RECORD_CALLS=record_calls))
    assert settings.record_calls is (record_calls == 'yes')
    path = write_env(tmp_path, AST_RECORD_CALLS=record_calls)
    with path.open('a') as stream:
        stream.write('PHONELET_RECORD_CALLS=true\n')
    with pytest.raises(ValueError, match='PHONELET_RECORD_CALLS is derived from AST_RECORD_CALLS'):
        deploy.configuration(path)


@pytest.mark.parametrize('retention', ['-1', 'x'])
def test_invalid_recording_retention_fails_preflight(deploy, tmp_path, retention):
    path = write_env(tmp_path)
    with path.open('a') as stream:
        stream.write('PHONELET_RECORDING_RETENTION_DAYS=' + retention + '\n')
    with pytest.raises(ValueError, match='PHONELET_RECORDING_RETENTION_DAYS'):
        deploy.configuration(path)


@pytest.mark.parametrize('record_calls', [None, 'no', 'yes'])
def test_recording_is_opt_in_and_audio_gains_default_to_neutral(deploy, tmp_path, record_calls):
    stage = stage_with_templates(tmp_path)
    path = write_env(tmp_path, AST_RECORD_CALLS=record_calls or 'no', PHONELET_RECORDINGS_DIR='/srv/recordings')
    if record_calls is None:
        path.write_text(''.join(line for line in path.read_text().splitlines(True)
                                if not line.startswith('AST_RECORD_CALLS=')))
    env, _ = deploy.configuration(path)
    rendered = deploy.render(stage, env)
    dongle = (rendered / 'dongle.conf').read_text()
    assert '\nrxgain=0\n' in dongle and '\ntxgain=0\n' in dongle
    dialplan = (rendered / 'extensions_custom.conf').read_text()
    # Failed-SMS report CDR markers remain enabled in both modes.
    assert f'Set(CDR(userfield)={SMS_REPORT_MARKER}' in dialplan
    incoming = [step[2] for step in context_steps(dialplan, 'dongle-incoming')]
    assert [step for step in incoming if step.startswith('Dial(')] == [
        'Dial(PJSIP/phone,30,b(sub-prepare-incoming-call^s^1)U(sub-start-recording^${RECORDING_PATH}))']
    assert 'Gosub(sub-prepare-recording,s,1(${SAFE_CALLER}))' in incoming
    assert 'Dial(Dongle/dongle0/${DIALNUM},60)' in dialplan
    # Outgoing calls are never recorded: no recording path, no MixMonitor() and no CDR userfield.
    outgoing = ' '.join(step[2] for step in context_steps(dialplan, 'sub-dial-outgoing'))
    assert 'recording' not in outgoing.lower() and 'MixMonitor' not in outgoing and 'CDR(' not in outgoing

    variables = {'ARG1': '+15551234567', 'RECORDING_PATH': 'stale-path', 'EPOCH': '1790337600',
                 'UNIQUEID': '1790337600.1', 'STRFTIME(1790337600,,%Y/%m/%d)': '2026/09/25',
                 'STRFTIME(1790337600,,%Y%m%d-%H%M%S)': '20260925-120000'}
    effects = run_recording_subroutine(dialplan, 'sub-prepare-recording', variables)
    if record_calls == 'yes':
        path = '/srv/recordings/2026/09/25/20260925-120000-1790337600.1-+15551234567'
        assert variables['RECORDING_PATH'] == path
        assert variables['CDR(userfield)'] == path + '.mp3'
        assert effects == [
            'TrySystem(/usr/local/lib/phonelet/convert-recording.py --check-space '
            '--root /srv/recordings --min-free-bytes 104857600)',
            'TrySystem(mkdir -p /srv/recordings/2026/09/25)']
    else:
        assert variables['RECORDING_PATH'] == ''
        assert 'RECORDING_DIR' not in variables and 'CDR(userfield)' not in variables
        assert effects == []  # No check-space helper and no mkdir, even with a stale path on entry.
    variables['ARG1'] = variables['RECORDING_PATH']
    effects = run_recording_subroutine(dialplan, 'sub-start-recording', variables)
    if record_calls == 'yes':
        assert effects == [f'MixMonitor({path}.wav,b,/usr/local/lib/phonelet/convert-recording.py '
                           f'--root /srv/recordings --min-free-bytes 104857600 {path}.wav)']
    else:
        assert effects == []


async def test_answered_call_without_recording_has_no_path():
    on_call_end = AsyncMock()
    handler = CdrHandler(on_call_end=on_call_end, on_sms_failed=AsyncMock())
    await handler.handle_cdr({'Channel': 'Dongle/dongle0-00000001', 'LastApplication': 'Dial',
                              'UniqueID': '1790337600.1', 'StartTime': '2026-09-25 12:00:00',
                              'Source': '+12025550123', 'Disposition': 'ANSWERED',
                              'BillableSeconds': '30', 'UserField': ''})
    on_call_end.assert_awaited_once()
    call = on_call_end.call_args.kwargs
    assert call['status'] == 'answered'
    assert call['recording_path'] is None


@pytest.fixture
def dialplan(deploy, tmp_path):
    stage = stage_with_templates(tmp_path)
    env, _ = deploy.configuration(write_env(tmp_path))
    return (deploy.render(stage, env) / 'extensions_custom.conf').read_text()


def context_steps(dialplan, name):
    body = re.search(r'^\[' + re.escape(name) + r'\]\n(.*?)(?=^\[|\Z)', dialplan, re.M | re.S)[1]
    return re.findall(r'^(?:exten => (\S+),1|\s+same => n(?:\((\w+)\))?),(.*)$', body, re.M)


def test_incoming_dialplan_marks_only_decline_responses_inside_dial(dialplan):
    assert context_steps(dialplan, 'sub-prepare-incoming-call') == [
        ('s', '', 'Set(CHANNEL(hangup_handler_push)=sub-incoming-hangup,s,1)'),
        ('', '', 'Return()'),
    ]
    assert context_steps(dialplan, 'sub-incoming-hangup') == [
        ('s', '', 'ExecIf(${REGEX("^SIP (480|486|600|603) " ${HANGUPCAUSE(${CHANNEL},tech)})}?'
                   'Set(CDR(userfield)=phonelet-call-rejected))'),
        ('', '', 'Return()'),
    ]
    # Party B's synchronous handler can update the last (even finalized) Dial CDR.
    # A Set on party A after Dial would instead update the new post-Dial CDR.
    incoming = [step[2] for step in context_steps(dialplan, 'dongle-incoming')]
    dial = 'Dial(PJSIP/phone,30,b(sub-prepare-incoming-call^s^1)U(sub-start-recording^${RECORDING_PATH}))'
    assert incoming[incoming.index(dial):] == [dial, 'Hangup()']
    assert dialplan.count('?Set(CDR(userfield)=phonelet-call-rejected)') == 1


@pytest.mark.parametrize('tech_cause,rejected', [
    ('SIP 480 Temporarily Unavailable', True),
    ('SIP 480 Unavailable', True),  # The code, not the reason phrase, defines the policy.
    ('SIP 486 Busy Here', True),
    ('SIP 600 Busy Everywhere', True),
    ('SIP 603 Decline', True),
    ('SIP 487 Request Terminated', False),
    ('SIP 180 Ringing', False),  # Local timeout/CANCEL before the phone sends 487.
    ('SIP 183 Session Progress', False),
    ('', False),  # No response was stored before the local timeout.
    ('SIP 403 Forbidden', False),  # Cause 21 alone no longer marks a call rejected.
    ('SIP 408 Request Timeout', False),
    ('SIP 503 Service Unavailable', False),
    ('SIP 4800 Invalid', False),
    ('other SIP 480 Temporarily Unavailable', False),
])
def test_incoming_dialplan_decline_response_policy(dialplan, tech_cause, rejected):
    # Check the rendered condition's POSIX-compatible pattern with synthetic tech
    # causes. This does not execute Asterisk or simulate its cause hash/CDR engine.
    command = context_steps(dialplan, 'sub-incoming-hangup')[0][2]
    match = re.fullmatch(
        r'ExecIf\(\$\{REGEX\("([^"]+)" \$\{HANGUPCAUSE\(\$\{CHANNEL\},tech\)\}\)\}\?'
        r'Set\(CDR\(userfield\)=phonelet-call-rejected\)\)', command,
    )
    assert match, command
    assert bool(re.search(match[1], tech_cause)) is rejected


def run_recording_subroutine(dialplan, name, variables):
    """Follow the rendered recording guards; external applications only record fake effects."""
    effects, jump = [], None
    for _, label, command in context_steps(dialplan, name):
        if jump and label != jump:
            continue
        jump = None
        while '${' in command:
            command = re.sub(r'\$\{([^{}]+)\}', lambda m: variables[m[1]], command)
        if command == 'Return()':
            return effects
        if match := re.fullmatch(r'Set\(([^=]+)=(.*)\)', command):
            variables[match[1]] = match[2]
        elif match := re.fullmatch(r'GotoIf\(\$\["([^"]*)" (=|!=) "([^"]*)"\]\?(\w+)\)', command):
            if (match[1] == match[3]) == (match[2] == '='):
                jump = match[4]
        elif command.startswith(('TrySystem(', 'MixMonitor(')):
            effects.append(command)
            variables['SYSTEMSTATUS'] = 'SUCCESS'
        else:
            pytest.fail('Unsupported recording step: ' + command)
    pytest.fail('Recording subroutine did not return')


def routed_number(dialplan, context, variable, number):
    """Match X/literal/dot patterns, ordered by Asterisk's per-character specificity."""
    matches = []
    for pattern, _, command in context_steps(dialplan, context):
        if not pattern:
            continue
        tokens = pattern.removeprefix('_')
        assert all(token in '0123456789+X.' for token in tokens)
        regex = ''.join({'X': '[0-9]', '.': '.+'}.get(token, re.escape(token)) for token in tokens)
        if re.fullmatch(regex, number):
            # A literal outranks X (ten possible digits); dot sorts after fixed character sets.
            priority = tuple((10, '0123456789') if token == 'X' else
                             (float('inf'), '') if token == '.' else (1, token) for token in tokens)
            matches.append((priority, pattern, command))
    assert matches, number
    _, pattern, command = min(matches)
    value = re.fullmatch(r'Set\(' + variable + r'=(.*)\)', command)[1]
    value = value.replace('${EXTEN:1}', number[1:]).replace('${EXTEN:2}', number[2:]).replace('${EXTEN}', number)
    value = re.sub(r'\$\{LEN\(([^()]*)\)\}', lambda m: str(len(m[1])), value)
    value = re.sub(r'\$\{IF\(\$\[(\d+) > (\d+)\]\?\+\)\}',
                   lambda m: '+' if int(m[1]) > int(m[2]) else '', value)
    assert '${' not in value
    return pattern, value


# Rules: 'plus' = +... as it is, 'access' = 00 becomes +, 'local' = trunk digit becomes the
# country prefix, 'other' = short numbers as they are, from six digits + added.
RULE_PATTERNS = {'plus': '_+X.', 'access': '_00XXX.', 'other': ('_X', '_X.')}


@pytest.mark.parametrize('context,variable', [('default', 'DIALNUM'), ('sms-outbound', 'SMS_TO')])
@pytest.mark.parametrize('local,country,cases', [
    ('1', '+1', [
        ('1', '1', 'other'), ('12', '12', 'other'), ('112', '112', 'other'), ('911', '911', 'other'),
        ('1234', '1234', 'other'), ('12345', '12345', 'other'),
        ('123456', '+123456', 'local'), ('1234567890', '+1234567890', 'local'),
        ('23456', '23456', 'other'), ('234567', '+234567', 'other'),
        ('15551234567', '+15551234567', 'local'), ('+15551234567', '+15551234567', 'plus'),
        ('0015551234567', '+15551234567', 'access'), ('00441234567', '+441234567', 'access'),
        ('001234', '+1234', 'access'), ('00123', '00123', 'other'),
    ]),
    ('0', '+1', [
        ('0', '0', 'other'), ('01', '01', 'other'), ('112', '112', 'other'), ('911', '911', 'other'),
        ('12345', '12345', 'other'), ('01234', '01234', 'other'), ('012345', '+112345', 'local'),
        ('05551234567', '+15551234567', 'local'), ('123456', '+123456', 'other'),
        ('15551234567', '+15551234567', 'other'), ('+15551234567', '+15551234567', 'plus'),
        # With trunk 0, 00 is international access, not trunk 0 before a local 0.
        ('00441234567', '+441234567', 'access'), ('0015551234567', '+15551234567', 'access'),
        ('001234', '+1234', 'access'), ('00123', '00123', 'other'), ('0012', '0012', 'other'),
        ('000', '000', 'other'), ('010234567', '+110234567', 'local'),
    ]),
    ('8', '+7', [
        ('89161234567', '+79161234567', 'local'), ('812345', '+712345', 'local'),
        ('81234', '81234', 'other'), ('112', '112', 'other'), ('0', '0', 'other'),
        ('00441234567', '+441234567', 'access'), ('0441234567', '+0441234567', 'other'),
        ('79161234567', '+79161234567', 'other'), ('+79161234567', '+79161234567', 'plus'),
        ('810441234567', '+710441234567', 'local'),
    ]),
    ('', '+44', [
        # No trunk prefix: a local-looking number only gets +; the 00 rule stays.
        ('07911123456', '+07911123456', 'other'), ('0441234567', '+0441234567', 'other'),
        ('441234567', '+441234567', 'other'), ('123456', '+123456', 'other'),
        ('00441234567', '+441234567', 'access'), ('001234', '+1234', 'access'),
        ('00123', '00123', 'other'), ('0', '0', 'other'), ('112', '112', 'other'), ('12345', '12345', 'other'),
        ('+441234567', '+441234567', 'plus'),
    ]),
])
def test_rendered_sms_and_call_number_routing(deploy, tmp_path, context, variable, local, country, cases):
    stage = stage_with_templates(tmp_path)
    env, _ = deploy.configuration(write_env(tmp_path, AST_TRUNK_PREFIX=local, AST_COUNTRY_PREFIX=country))
    dialplan = (deploy.render(stage, env) / 'extensions_custom.conf').read_text()
    patterns = RULE_PATTERNS | {'local': f'_{local}XXXX.'}
    for number, expected, rule in cases:
        pattern, actual = routed_number(dialplan, context, variable, number)
        assert actual == expected, (context, number, pattern)
        # With +1 the resulting string alone cannot distinguish local conversion from adding +.
        assert pattern in (patterns[rule] if rule == 'other' else (patterns[rule],)), (context, number, pattern)


@pytest.mark.parametrize('local', ['', '0'])
def test_empty_trunk_prefix_leaves_out_only_the_local_rules(deploy, tmp_path, local):
    stage = stage_with_templates(tmp_path)
    env, _ = deploy.configuration(write_env(tmp_path, AST_TRUNK_PREFIX=local, AST_COUNTRY_PREFIX='+44'))
    dialplan = (deploy.render(stage, env) / 'extensions_custom.conf').read_text()
    local_rules = {'default': ('_0XXXX.', '', 'Set(DIALNUM=+44${EXTEN:1})'),
                   'sms-outbound': ('_0XXXX.', '', 'Set(SMS_TO=+44${EXTEN:1})')}
    for context, rule in local_rules.items():
        patterns = [step[0] for step in context_steps(dialplan, context) if step[0]]
        assert patterns == ['_+X.', '_00XXX.'] + ['_0XXXX.'] * (local == '0') + ['_X', '_X.'], context
        assert (rule in context_steps(dialplan, context)) == (local == '0'), context
    # Nothing else changes: the dialplan without the rules is the one with them, minus their lines.
    full_env, _ = deploy.configuration(write_env(tmp_path, AST_TRUNK_PREFIX='0', AST_COUNTRY_PREFIX='+44'))
    full = (deploy.render(stage, full_env) / 'extensions_custom.conf').read_text().splitlines()
    changes = [line for line in difflib.ndiff(full, dialplan.splitlines()) if line[:2] in ('- ', '+ ')]
    assert changes == ([] if local == '0' else [
        '- exten => _0XXXX.,1,Set(SMS_TO=+44${EXTEN:1})', '-  same => n,Goto(sms-send,s,1)',
        '- exten => _0XXXX.,1,Set(DIALNUM=+44${EXTEN:1})', '-  same => n,Gosub(sub-dial-outgoing,s,1)',
        '-  same => n,Hangup()'])


def test_render_refuses_a_trunk_prefix_outside_the_local_rule(deploy, tmp_path):
    stage = stage_with_templates(tmp_path)
    template = stage / 'asterisk' / 'extensions_custom.conf.tpl'
    template.write_text(template.read_text() + '; trunk ${AST_TRUNK_PREFIX}\n')
    env, _ = deploy.configuration(write_env(tmp_path, AST_TRUNK_PREFIX=''))
    with pytest.raises(ValueError, match='AST_TRUNK_PREFIX outside a local number rule'):
        deploy.render(stage, env)


def test_outgoing_sms_goes_through_base64_application(dialplan):
    # The plain application would make Asterisk split the body on commas and brackets.
    assert re.findall(r'DongleSendSMS\w*', dialplan) == ['DongleSendSMSBase64']
    assert not re.search(r'DongleSendSMS\(', dialplan)
    # Each pattern only names the recipient as the driver gets it; one place sends.
    assert context_steps(dialplan, 'sms-outbound') == [
        ('_+X.', '', 'Set(SMS_TO=${EXTEN})'), ('', '', 'Goto(sms-send,s,1)'),
        ('_00XXX.', '', 'Set(SMS_TO=+${EXTEN:2})'), ('', '', 'Goto(sms-send,s,1)'),
        ('_0XXXX.', '', 'Set(SMS_TO=+1${EXTEN:1})'), ('', '', 'Goto(sms-send,s,1)'),
        ('_X', '', 'Set(SMS_TO=${EXTEN})'), ('', '', 'Goto(sms-send,s,1)'),
        ('_X.', '', 'Set(SMS_TO=${IF($[${LEN(${EXTEN})} > 5]?+)}${EXTEN})'),
        ('', '', 'Goto(sms-send,s,1)'),
    ]


def test_outgoing_sms_rejected_before_the_driver_queue_starts_a_report(dialplan):
    message = '${BASE64_ENCODE(${MESSAGE(body)})}'
    assert context_steps(dialplan, 'sms-send') == [
        # TryExec() expands its argument again: only digits with an optional "+" reach it.
        ('s', '', 'Set(TRYSTATUS=BADNUMBER)'),
        ('', '', 'GotoIf(${REGEX("^[+]?[0-9]+$" ${SMS_TO})}?:rejected)'),
        ('', '', f'TryExec(DongleSendSMSBase64(dongle0,${{SMS_TO}},{message},,,1))'),
        # Success: the driver has the SMS and reports it; no CDR here.
        ('', '', 'GotoIf($["${TRYSTATUS}" = "SUCCESS"]?done)'),
        # FAILED, NOAPP or a refused number: the status only, never the number or the text.
        ('', 'rejected', 'Log(NOTICE,Outgoing SMS rejected before the driver queue status=${TRYSTATUS})'),
        # A report of type r into report@dongle-incoming: its one marked CDR line, src = recipient.
        ('', '', 'Originate(Local/report@dongle-incoming,app,Wait,1,,,'
                 'ac(${FILTER(0-9+,${SMS_TO})})v(SMS_REPORT_TYPE=r^SMS_REPORT_SUCCESS=0))'),
        # No report channel, no alert: say so, again without the number.
        ('', '', 'ExecIf($["${ORIGINATE_STATUS}" != "SUCCESS"]?'
                 'Log(ERROR,No alert for the rejected outgoing SMS status=${ORIGINATE_STATUS}))'),
        ('', 'done', 'Hangup()'),
    ]
    assert 'r' in SMS_REPORT_TYPES
    # The Message channel that runs these contexts keeps one uniqueid for every SMS: no CDR there.
    for name in ('sms-outbound', 'sms-send'):
        assert not any('CDR' in step[2] for step in context_steps(dialplan, name))


def test_message_body_is_only_ever_used_inside_base64_encode(dialplan):
    uses = [dialplan[max(0, m.start() - len('${BASE64_ENCODE(')):m.start()]
            for m in re.finditer(r'\$\{MESSAGE\(body\)\}', dialplan)]
    assert uses and all(prefix == '${BASE64_ENCODE(' for prefix in uses)


def test_dongle_uses_one_data_port_on_pcui(deploy, tmp_path):
    stage = stage_with_templates(tmp_path)
    env, _ = deploy.configuration(write_env(tmp_path))
    rendered = (deploy.render(stage, env) / 'dongle.conf').read_text()
    settings = re.findall(r'^(\w+)=(.*)$', rendered, re.M)
    assert ('data', '/dev/dongle0-at') in settings and ('audio', '/dev/dongle0-audio') in settings
    assert 'urc' not in dict(settings)


def test_outgoing_sms_report_keeps_one_marked_cdr_only_on_failure(dialplan):
    context = re.search(r'^\[dongle-incoming\]\n(.*?)(?=^\[|\Z)', dialplan, re.M | re.S)[1]
    steps = re.findall(r'^(?:exten => report,1|\s+same => n(?:\((\w+)\))?),(.*)$',
                       re.search(r'^exten => report,1,.*?(?=^\S|\Z)', context, re.M | re.S)[0], re.M)
    # The other half of the Local pair is disabled by name (its CDR would be a FAILED line);
    # success disables this half too, failure keeps its CDR (src = recipient) with the marker.
    assert steps == [
        ('', 'Log(NOTICE,Outgoing SMS report type=${SMS_REPORT_TYPE} success=${SMS_REPORT_SUCCESS})'),
        ('', 'Set(EXPORT(${CHANNEL:0:-1}${IF($["${CHANNEL:-1}" = "1"]?2:1)},CDR_PROP(disable))=1)'),
        ('', 'GotoIf($["${SMS_REPORT_SUCCESS}" != "1"]?failed)'),
        ('', 'Set(CDR_PROP(disable)=1)'),
        ('', 'Hangup()'),
        ('failed', f'Set(CDR(userfield)={SMS_REPORT_MARKER}${{SMS_REPORT_TYPE}})'),
        ('', 'Hangup()'),
    ]
    # Only the outcome: no number (CALLERID), text (SMS_REPORT_PAYLOAD) or report string (SMS_REPORT).
    assert set(re.findall(r'\$\{([^}]*)\}', steps[0][1])) == {'SMS_REPORT_TYPE', 'SMS_REPORT_SUCCESS'}
    # The driver starts report@ in the dongle context only.
    assert not re.search(r'^exten => report,', dialplan.replace(context, ''), re.M)


def test_incoming_ussd_leaves_no_log_and_no_cdr(dialplan):
    # The driver starts Local/ussd@dongle-incoming for each incoming USSD: without the
    # extension that is "no such extension". Like a successful report, but with no Log().
    context = re.search(r'^\[dongle-incoming\]\n(.*?)(?=^\[|\Z)', dialplan, re.M | re.S)[1]
    steps = re.findall(r'^(?:exten => ussd,1|\s+same => n(?:\((\w+)\))?),(.*)$',
                       re.search(r'^exten => ussd,1,.*?(?=^\S|\Z)', context, re.M | re.S)[0], re.M)
    assert steps == [
        ('', 'Set(EXPORT(${CHANNEL:0:-1}${IF($["${CHANNEL:-1}" = "1"]?2:1)},CDR_PROP(disable))=1)'),
        ('', 'Set(CDR_PROP(disable)=1)'),
        ('', 'Hangup()'),
    ]
    assert not re.search(r'^exten => ussd,', dialplan.replace(context, ''), re.M)


def test_cdr_keeps_unanswered_lines():
    # A failed outgoing SMS line is NO ANSWER, like a missed call: without this both vanish.
    assert re.search(r'^unanswered\s*=\s*yes\s*$', (ROOT / 'asterisk' / 'cdr.conf').read_text(), re.M)


def test_outgoing_call_patterns_only_name_the_number(dialplan):
    # One place checks and dials; the patterns never put EXTEN into Dial() or Gosub() arguments.
    dial = ('', '', 'Gosub(sub-dial-outgoing,s,1)'), ('', '', 'Hangup()')
    assert context_steps(dialplan, 'default') == [
        ('_+X.', '', 'Set(DIALNUM=${EXTEN})'), *dial,
        ('_00XXX.', '', 'Set(DIALNUM=+${EXTEN:2})'), *dial,
        ('_0XXXX.', '', 'Set(DIALNUM=+1${EXTEN:1})'), *dial,
        ('_X', '', 'Set(DIALNUM=${EXTEN})'), *dial,
        # Up to five digits a short number (112) is dialed as it is, longer ones get "+".
        # The IF() holds only the length, so no character of EXTEN can change its branches.
        ('_X.', '', 'Set(DIALNUM=${IF($[${LEN(${EXTEN})} > 5]?+)}${EXTEN})'), *dial,
    ]


def test_outgoing_call_dials_only_a_checked_number(dialplan):
    steps = context_steps(dialplan, 'sub-dial-outgoing')
    assert steps[0] == ('s', '', 'GotoIf(${REGEX("^[+]?[0-9]+$" ${DIALNUM})}?:rejected)')
    assert steps[1] == ('', '', 'Dial(Dongle/dongle0/${DIALNUM},60)')
    # No recording path (no space, mkdir failed): no MixMonitor() on the incoming call.
    recording = context_steps(dialplan, 'sub-start-recording')
    assert recording[0] == ('s', '', 'GotoIf($["${ARG1}" = ""]?done)')
    assert recording[1][2].startswith('MixMonitor(${ARG1}.wav,')
    assert recording[2] == ('', 'done', 'Return()')
    # Refused without the number in the log: cause 28 is "invalid number format".
    assert steps[-2:] == [('', 'rejected', 'Log(NOTICE,Outgoing call rejected: not a valid number)'),
                          ('', '', 'Hangup(28)')]
    assert re.findall(r'Dial\(Dongle/[^)]*', dialplan) == ['Dial(Dongle/dongle0/${DIALNUM},60']


@pytest.mark.parametrize('number, valid', [
    ('+15551234567', True), ('+15551234568', True), ('112', True), ('0500', True),
    ('+15551,,tT', False), ('+15551&Dongle/dongle0/+15552', False), ('112;', False),
    ('+', False), ('', False), ('++15551', False), ('1555+1', False), ('+1 555', False),
])
def test_outgoing_call_number_check(dialplan, number, valid):
    # REGEX() is POSIX extended syntax; this expression means the same in Python.
    pattern = re.search(r'REGEX\("([^"]*)" \$\{DIALNUM\}\)', dialplan)[1]
    assert bool(re.search(pattern, number)) is valid


def test_recording_is_named_only_once_its_directory_exists(dialplan):
    steps = [step[2] for step in context_steps(dialplan, 'sub-prepare-recording')]
    mkdir = steps.index('TrySystem(mkdir -p ${RECORDING_DIR})')
    # No path, so no MixMonitor() and no recording in the CDR, when mkdir fails.
    assert steps[mkdir + 1:mkdir + 4] == ['GotoIf($["${SYSTEMSTATUS}" != "SUCCESS"]?done)',
                                          'Set(RECORDING_PATH=${RECORDING_DIR}/${RECORDING_FILE})',
                                          'Set(CDR(userfield)=${RECORDING_PATH}.mp3)']
    assert not any(step.startswith('System(') for step in steps)  # System() hangs up when it cannot run mkdir


def test_incoming_sms_never_goes_to_the_phone_from_the_dialplan():
    # The ingress patch stops the driver starting Local/sms@: Phonelet delivers incoming SMS
    # through its outbox. Nothing may send them from the dialplan past that queue.
    patches = {path.name: path.read_text() for path in (ROOT / 'chan_dongle_patch').glob('*.patch')}
    removed = '-\t\t\t\t\tstart_local_channel (pvt, "sms", oa, vars);'
    assert removed in patches['chan_dongle_ingress.patch'].splitlines()
    assert not any(re.search(r'^\+.*start_local_channel\s*\(pvt, "sms"', text, re.M) for text in patches.values())
    template = (ROOT / 'asterisk' / 'extensions_custom.conf.tpl').read_text()
    assert not re.search(r'^exten => sms,', template, re.M) and 'SMS_BASE64' not in template


def test_cdr_journal_columns_match_the_replay_fields():
    # ingress.py reads phonelet.csv rows by position: the mapping must keep CDR_FIELDS' order.
    names = {'uniqueid': 'UniqueID', 'channel': 'Channel', 'lastapp': 'LastApplication', 'src': 'Source',
             'disposition': 'Disposition', 'billsec': 'BillableSeconds', 'userfield': 'UserField',
             'start': 'StartTime'}
    mapping = re.search(r'^phonelet\.csv => (.*)$', (ROOT / 'asterisk' / 'cdr_custom.conf').read_text(), re.M)[1]
    columns = re.findall(r'\$\{CSV_QUOTE\(.*?\$\{CDR\((\w+)', mapping)
    assert len(columns) == mapping.count('CSV_QUOTE(')
    assert tuple(names[column] for column in columns) == CDR_FIELDS
    assert '${STRFTIME(${CDR(start,u)},UTC,%Y-%m-%dT%H:%M:%S%z)}' in mapping


def test_patch_series_lists_every_driver_patch_once():
    # prepare-sources.sh (build-asterisk.sh runs it) and the PTY tests read the order from series.
    text = (ROOT / 'chan_dongle_patch' / 'series').read_text()
    names = text.splitlines()
    # bash mapfile -t keeps a CR and turns a blank line into an empty name: one bare name per line.
    assert text == ''.join(name + '\n' for name in names) and all(re.fullmatch(r'\w+', n) for n in names)
    assert names == ['ingress', 'outgoing_base64', 'unexpected_error', 'command_deadline', 'readable_deadline',
                     'me_storage']
    patches = sorted(path.name for path in (ROOT / 'chan_dongle_patch').glob('*.patch'))
    assert sorted(f'chan_dongle_{name}.patch' for name in names) == patches


def test_cdr_goes_only_to_the_phonelet_journal():
    # Without [csv] cdr_csv declines to load; cdr_manager stays deployed but off (Asterisk 20.22.0).
    cdr = (ROOT / 'asterisk' / 'cdr.conf').read_text()
    assert re.findall(r'^\[(\w+)\]', cdr, re.M) == ['general']
    manager = (ROOT / 'asterisk' / 'cdr_manager.conf').read_text()
    assert re.findall(r'^(\w+)\s*=\s*(\S+)', manager, re.M) == [('enabled', 'no')]


def test_primary_configs_hold_only_phonelet():
    # The samples put demo into [default] (a guest IAX2 call could dial out) and AMI on 0.0.0.0.
    extensions = (ROOT / 'asterisk' / 'extensions.conf').read_text()
    assert re.findall(r'^\[(\w+)\]', extensions, re.M) == ['general']
    assert re.findall(r'^#include\s+"(\S+)"', extensions, re.M) == ['extensions_custom.conf']
    assert not re.search(r'^\s*include\s*=>', extensions, re.M)
    manager = (ROOT / 'asterisk' / 'manager.conf').read_text()
    assert dict(re.findall(r'^(\w+)\s*=\s*(\S+)', manager, re.M)) == {
        'enabled': 'yes', 'port': '5038', 'bindaddr': '127.0.0.1'}
    assert re.findall(r'^#include\s+"(\S+)"', manager, re.M) == ['manager_custom.conf']


def test_modules_conf_keeps_network_listeners_unloaded():
    noloads = re.findall(r'^noload => (\S+)$', (ROOT / 'asterisk' / 'modules.conf').read_text(), re.M)
    assert {'chan_iax2.so', 'chan_unistim.so', 'chan_skinny.so', 'chan_mgcp.so', 'chan_sip.so',
            'chan_ooh323.so', 'chan_motif.so', 'res_xmpp.so', 'pbx_dundi.so', 'res_hep.so',
            'pbx_ael.so', 'pbx_lua.so'} <= set(noloads)  # the last two load sample dialplans
    # Deploy unloads in file order: a module before the one it depends on.
    assert noloads.index('chan_motif.so') < noloads.index('res_xmpp.so')
    assert max(noloads.index('res_hep_pjsip.so'), noloads.index('res_hep_rtcp.so')) < noloads.index('res_hep.so')
    needed = {'chan_pjsip.so', 'res_pjsip.so', 'res_pjsip_messaging.so', 'chan_dongle.so', 'pbx_config.so',
              'cdr_custom.so', 'app_mixmonitor.so', 'app_exec.so', 'app_originate.so', 'func_strings.so'}
    assert not needed & set(noloads)


def test_every_context_the_channels_use_is_phonelets():
    defined = set(re.findall(r'^\[([\w-]+)\]', (ROOT / 'asterisk' / 'extensions_custom.conf.tpl').read_text(), re.M))
    used = {value for name in ('dongle.conf.tpl', 'pjsip_custom.conf.tpl')
            for value in re.findall(r'^(?:message_)?context\s*=\s*(\S+)', (ROOT / 'asterisk' / name).read_text(), re.M)}
    assert used == {'default', 'dongle-incoming', 'sms-outbound'} and used <= defined


@pytest.mark.parametrize('key', [key for key in BASE_ENV if key != 'AST_TRUNK_PREFIX'])
def test_each_required_key_must_be_in_the_file(deploy, tmp_path, monkeypatch, key):
    path = write_env(tmp_path)
    path.write_text(''.join(line for line in path.read_text().splitlines(True)
                            if not line.startswith(key + '=')))
    monkeypatch.setenv(key, BASE_ENV[key])  # The developer's shell cannot supply a missing answer.
    with pytest.raises(ValueError, match=key):
        deploy.configuration(path)


@pytest.mark.parametrize('key,source', DERIVED_SOURCES.items())
@pytest.mark.parametrize('value', ['private-synthetic-value', '', None])
def test_derived_template_keys_cannot_be_configured(deploy, tmp_path, key, source, value):
    path = write_env(tmp_path)
    with path.open('a') as stream:
        stream.write(key + ('=' + value if value is not None else '') + '\n')
    with pytest.raises(ValueError, match=key + ' is derived from ' + source) as caught:
        deploy.configuration(path)
    assert 'private-synthetic-value' not in str(caught.value)


@pytest.mark.parametrize('key', ['AST_HOST', 'AST_LOCAL_PREFIX', 'AST_AMI_PERMIT', 'AST_DONGLE_DEVICE',
                                 'AST_DONGLE_URC', 'PHONELET_CDR_TIMEZONE', 'PHONELET_DONGLE_DEVICE',
                                 'PHONELET_SIP_MESSAGE_DOMAIN'])
def test_removed_names_are_rejected_without_values(deploy, tmp_path, key):
    with pytest.raises(ValueError, match=key) as caught:
        deploy.configuration(write_env(tmp_path, **{key: 'private-synthetic-value'}))
    assert 'private-synthetic-value' not in str(caught.value)


@pytest.mark.parametrize('domain', ['', 'replace_me', 'changeme', '192.0.2.10;bad', 'bad domain'])
def test_deployment_requires_a_real_sip_domain(deploy, tmp_path, domain):
    with pytest.raises(ValueError, match='PHONELET_SIP_DOMAIN'):
        deploy.configuration(write_env(tmp_path, PHONELET_SIP_DOMAIN=domain))


def test_recording_inputs_are_derived_from_app_settings(deploy, tmp_path):
    env, settings = deploy.configuration(write_env(tmp_path, PHONELET_RECORDINGS_DIR='/srv/recordings',
                                                  PHONELET_MIN_FREE_BYTES='123456'))
    assert env['AST_RECORDINGS_DIR'] == settings.recordings_dir == '/srv/recordings'
    assert env['AST_MIN_FREE_BYTES'] == str(settings.min_free_bytes) == '123456'


@pytest.mark.parametrize('directory, accepted', [
    ('/var/lib/phonelet/recordings', True), ('/srv/recordings', True), ('/usrdata/recordings', True),
    ('/root/recordings', False), ('/home/owner/recordings', False), ('//home/owner', False),
    ('/./etc/recordings', False), ('/usr/local/recordings', False), ('/boot', False), ('/run/user/0/rec', False),
    ('/tmp/recordings', False), ('/var/tmp/recordings', False),  # PrivateTmp: Asterisk has its own /tmp
])
def test_recordings_stay_outside_what_asterisk_service_sees_read_only(deploy, tmp_path, directory, accepted):
    """asterisk.service has ProtectSystem=full and ProtectHome=read-only: MixMonitor cannot write there."""
    path = write_env(tmp_path, PHONELET_RECORDINGS_DIR=directory)
    if accepted:
        deploy.configuration(path)
        return
    with pytest.raises(ValueError, match='PHONELET_RECORDINGS_DIR must be outside') as caught:
        deploy.configuration(path)
    assert 'owner' not in str(caught.value)  # the message names the key, not the value
    unit = (ROOT / 'asterisk/systemd/asterisk.service.d/10-phonelet.conf').read_text()
    assert 'ProtectSystem=full\n' in unit and 'ProtectHome=read-only\n' in unit


@pytest.mark.parametrize('target', ['forbidden', 'forbidden/inner', 'allowed'])
def test_recordings_through_a_symlink_are_judged_by_their_target(deploy, tmp_path, monkeypatch, target):
    """Only the symlink's target is in the refused list: the refusal must come from realpath."""
    for name in ('forbidden/inner', 'allowed'):
        (tmp_path / name).mkdir(parents=True)
    monkeypatch.setattr(deploy, 'ASTERISK_UNWRITABLE', (str(tmp_path / 'forbidden'),))
    link = tmp_path / 'srv-link'
    link.symlink_to(tmp_path / target)
    path = write_env(tmp_path, PHONELET_RECORDINGS_DIR=str(link / 'recordings'))
    if target == 'allowed':
        assert deploy.configuration(path)[1].recordings_dir == str(link / 'recordings')
        return
    with pytest.raises(ValueError, match='PHONELET_RECORDINGS_DIR must be outside'):
        deploy.configuration(path)


def test_modem_mdm_port_error_does_not_echo_the_input_path(deploy, tmp_path):
    with pytest.raises(ValueError, match=r'AST_DONGLE_DATA is the modem \(MDM\) port') as caught:
        deploy.configuration(write_env(tmp_path, AST_DONGLE_DATA='/dev/private-synthetic-value/../dongle0-modem'))
    assert 'private-synthetic-value' not in str(caught.value)


@pytest.mark.parametrize('overrides,message', [
    ({'PHONELET_TELEGRAM_TOKEN': 'private-telegram-token'},
     'Telegram token and chat ID must be configured together'),
    ({'PHONELET_HA_URL': 'http://192.0.2.10'},
     'Home Assistant URL and token must be configured together'),
    ({'PHONELET_HA_URL': 'ftp://192.0.2.10', 'PHONELET_HA_TOKEN': 'private-ha-token'},
     'Home Assistant URL must use http or https'),
    ({'PHONELET_LOG_LEVEL': 'private-invalid-level'}, 'Invalid log level'),
    ({'PHONELET_SIP_MESSAGE_DESTINATION': 'private-invalid-destination'}, 'Invalid SIP message destination'),
    ({'PHONELET_SMS_COMMAND_KEY': 'private-invalid-command-key'},
     "PHONELET_SMS_COMMAND_KEY must be base64url without '=' of exactly 32 bytes"),
    ({'PHONELET_API_HOST': 'private-invalid-host'}, 'Invalid PHONELET_API_HOST'),
    ({'PHONELET_WIFI_INTERFACE': 'private invalid interface'}, 'Invalid Wi-Fi interface'),
])
def test_model_validation_preserves_safe_reason_without_input_values(deploy, tmp_path, overrides, message):
    with pytest.raises(ValueError) as caught:
        deploy.configuration(write_env(tmp_path, **overrides))
    assert str(caught.value) == 'Invalid configuration: ' + message
    assert all(value not in str(caught.value) for value in overrides.values())
    assert all(BASE_ENV[key] not in str(caught.value) for key in
               ('PHONELET_AMI_SECRET', 'PHONELET_API_KEY', 'AST_SIP_PASSWORD'))


@pytest.mark.parametrize('overrides,keys', [
    ({'PHONELET_API_PORT': 'private-invalid-port'}, 'PHONELET_API_PORT'),
    ({'PHONELET_API_PORT': 'private-invalid-port', 'PHONELET_AMI_PORT': 'private-invalid-port'},
     'PHONELET_AMI_PORT, PHONELET_API_PORT'),
])
def test_field_validation_reports_only_the_key(deploy, tmp_path, overrides, keys):
    with pytest.raises(ValueError) as caught:
        deploy.configuration(write_env(tmp_path, **overrides))
    assert str(caught.value) == 'Invalid configuration: ' + keys
