"""Initial environment creation and validation through --init-env."""
import builtins
import io
import os
import re
import subprocess
import sys

import pytest

from tests.helpers import ROOT, script


def init_env_command(path, *options):
    return subprocess.run([sys.executable, str(ROOT / 'scripts/deploy-release.py'), '--init-env', str(path),
                           *options], capture_output=True, text=True, input='', cwd=ROOT)


INIT_OPTIONS = ('--country-prefix', '+1', '--trunk-prefix', '0', '--sip-domain', '192.0.2.10')


def test_init_env_creates_private_valid_configuration_without_printing_secrets(tmp_path):
    from dotenv import dotenv_values

    path = tmp_path / 'phonelet config.env'
    result = init_env_command(path, *INIT_OPTIONS)
    assert result.returncode == 0, result.stderr
    assert path.stat().st_mode & 0o777 == 0o600
    module = script('deploy-release')
    values = dotenv_values(path, interpolate=False)
    assert set(values) == set(module.REQUIRED_KEYS) | {'AST_TRUNK_PREFIX'}
    assert len(path.read_text().splitlines()) == 7
    assert path.read_text().splitlines()[0] == '# See docs/install.md; optional settings are listed in .env.example.'
    secrets = [values[key] for key in ('PHONELET_AMI_SECRET', 'PHONELET_API_KEY', 'AST_SIP_PASSWORD')]
    assert len(set(secrets)) == 3
    assert all(re.fullmatch(r'[A-Za-z0-9_-]{43}', value) for value in secrets)
    assert all(value not in result.stdout + result.stderr for value in secrets)
    env, settings = module.configuration(path)
    assert env['AST_AMI_SECRET'] == settings.ami_secret == secrets[0]
    assert result.stderr == '' and len(result.stdout.splitlines()) == 1
    assert "grep AST_SIP_PASSWORD '" + str(path) + "'" in result.stdout


@pytest.mark.parametrize('symlink', [False, True])
def test_init_env_never_overwrites_an_existing_file(tmp_path, symlink):
    existing = tmp_path / 'existing.env'
    existing.write_bytes(b'synthetic original\n')
    path = tmp_path / 'link.env' if symlink else existing
    if symlink:
        path.symlink_to(existing)
    result = init_env_command(path, *INIT_OPTIONS)
    assert result.returncode != 0
    assert existing.read_bytes() == b'synthetic original\n'
    assert path.exists() and path.is_symlink() == symlink


@pytest.mark.parametrize('index,value,key', [(1, '1', 'AST_COUNTRY_PREFIX'),
                                            (3, '00', 'AST_TRUNK_PREFIX'),
                                            (5, 'replace_me', 'PHONELET_SIP_DOMAIN'),
                                            (5, 'private-value\nPHONELET_API_KEY=private-value', 'environment')])
def test_init_env_invalid_answer_removes_created_file(tmp_path, index, value, key):
    path = tmp_path / 'invalid.env'
    options = list(INIT_OPTIONS)
    options[index] = value
    result = init_env_command(path, *options)
    assert result.returncode != 0 and not path.exists()
    assert key.lower() in result.stderr.lower()
    if index == 5:
        assert value not in result.stdout + result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('answer', ['x"y', '"', "x'y", 'x\ny', 'x\r\n"=y'])
def test_init_env_quotes_and_newlines_in_answers_parse(tmp_path, monkeypatch, answer):
    from dotenv import dotenv_values
    from dotenv.parser import parse_stream

    module = script('deploy-release')
    monkeypatch.setattr(module, 'configuration', lambda path: None)
    path = tmp_path / 'quoted.env'
    module.init_env(path, answer, answer, answer)
    assert not any(binding.error for binding in parse_stream(io.StringIO(path.read_text())))
    values = dotenv_values(path, interpolate=False)
    assert [values[key] for key in ('AST_COUNTRY_PREFIX', 'AST_TRUNK_PREFIX', 'PHONELET_SIP_DOMAIN')] == [answer] * 3
    with pytest.raises(ValueError) as caught:
        script('deploy-release').configuration(path)
    assert 'cannot be parsed' not in str(caught.value)


@pytest.mark.parametrize('index', [0, 4])
def test_init_env_non_tty_requires_each_option(tmp_path, index):
    path = tmp_path / 'missing.env'
    options = list(INIT_OPTIONS)
    missing = options[index]
    del options[index:index + 2]
    result = init_env_command(path, *options)
    assert result.returncode != 0 and missing in result.stderr and not path.exists()


@pytest.mark.parametrize('options', [INIT_OPTIONS[:2] + INIT_OPTIONS[4:],
                                     INIT_OPTIONS[:3] + ('',) + INIT_OPTIONS[4:]])
def test_init_env_non_tty_trunk_prefix_is_optional(tmp_path, options):
    from dotenv import dotenv_values

    path = tmp_path / 'no-trunk.env'
    result = init_env_command(path, *options)
    assert result.returncode == 0, result.stderr
    assert dotenv_values(path, interpolate=False)['AST_TRUNK_PREFIX'] == ''
    env, _ = script('deploy-release').configuration(path)
    assert env['AST_TRUNK_PREFIX'] == ''


def test_init_env_prompts_for_missing_answers_only(tmp_path, monkeypatch, capsys):
    module = script('deploy-release')
    prompts = []
    answers = iter(['+1', '192.0.2.10'])

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(builtins, 'input', answer)
    calls = []
    generated = iter(['synthetic-ami-secret', 'synthetic-api-key', 'synthetic-sip-password'])

    def token(size):
        calls.append(size)
        return next(generated)

    monkeypatch.setattr(module.secrets, 'token_urlsafe', token)
    path = tmp_path / 'interactive.env'
    module.init_env(path, trunk_prefix='0')
    assert len(prompts) == 2
    assert prompts[0].startswith('Country calling code') and '+44' in prompts[0]
    assert prompts[1].startswith('Address or DNS name the SIP phone') and 'VPN or Tailscale' in prompts[1]
    assert not any('--' in prompt for prompt in prompts)
    assert calls == [32, 32, 32]
    module.configuration(path)
    output = capsys.readouterr()
    assert 'synthetic-' not in output.out + output.err


def test_init_env_validation_error_does_not_print_generated_secrets(tmp_path, monkeypatch, capsys):
    module = script('deploy-release')
    generated = ['synthetic-ami-secret', 'synthetic-api-key', 'synthetic-sip-password']
    values = iter(generated)
    monkeypatch.setattr(module.secrets, 'token_urlsafe', lambda _: next(values))
    path = tmp_path / 'invalid.env'
    with pytest.raises(ValueError, match='PHONELET_SIP_DOMAIN') as caught:
        # Reuse a generated secret in an invalid answer: error messages must name only keys.
        module.init_env(path, '+1', '0', generated[0] + ';bad')
    assert not path.exists()
    output = capsys.readouterr()
    assert all(value not in str(caught.value) + output.out + output.err for value in generated)


def test_init_env_cli_ignores_caller_phonelet_environment(tmp_path, monkeypatch):
    from dotenv import dotenv_values

    monkeypatch.setenv('PHONELET_TELEGRAM_TOKEN', 'private-stray-token')
    path = tmp_path / 'isolated.env'
    result = init_env_command(path, *INIT_OPTIONS)
    assert result.returncode == 0, result.stderr
    assert path.stat().st_mode & 0o777 == 0o600
    values = dotenv_values(path, interpolate=False)
    assert set(values) == set(script('deploy-release').REQUIRED_KEYS) | {'AST_TRUNK_PREFIX'}
    assert all(value not in result.stdout + result.stderr for key, value in values.items()
               if key in ('PHONELET_AMI_SECRET', 'PHONELET_API_KEY', 'AST_SIP_PASSWORD'))
    assert 'private-stray-token' not in result.stdout + result.stderr + path.read_text()
    assert os.environ['PHONELET_TELEGRAM_TOKEN'] == 'private-stray-token'


@pytest.mark.parametrize('country_prefix', ['+1', 'invalid'])
def test_init_env_validation_isolates_and_restores_environment(tmp_path, monkeypatch, country_prefix):
    module = script('deploy-release')
    monkeypatch.setenv('PHONELET_TELEGRAM_TOKEN', 'private-stray-token')
    monkeypatch.setenv('PHONELET_DB_PATH', '/tmp/c18-synthetic-shell.db')
    path = tmp_path / 'isolated.env'
    if country_prefix == '+1':
        module.init_env(path, country_prefix, '0', '192.0.2.10')
        # A lone Telegram token from the shell would fail validation: deployment reads the .env only.
        _, settings = module.configuration(path)
        assert settings.telegram_token == '' and settings.db_path == '/var/lib/phonelet/phonelet.db'
    else:
        with pytest.raises(ValueError, match='AST_COUNTRY_PREFIX'):
            module.init_env(path, country_prefix, '0', '192.0.2.10')
        assert not path.exists()
    assert os.environ['PHONELET_TELEGRAM_TOKEN'] == 'private-stray-token'
    assert os.environ['PHONELET_DB_PATH'] == '/tmp/c18-synthetic-shell.db'
