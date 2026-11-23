"""Backups, recording conversion, package metadata and operations examples."""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

from tests.helpers import ROOT, executable, script


@pytest.mark.parametrize('failure', ['encoder', 'empty', 'disk', None])
def test_recording_conversion_is_atomic_and_preserves_failed_input(tmp_path, monkeypatch, failure):
    module = script('convert-recording')
    wav = tmp_path / 'sample.wav'
    wav.write_bytes(b'WAV data')
    encoder = tmp_path / 'lame'
    executable(encoder, 'echo partial > "$5"\nexit 1\n' if failure == 'encoder' else (': > "$5"\n' if failure == 'empty' else 'echo encoded > "$5"\n'))
    if failure == 'disk':
        monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    result = module.convert(wav, tmp_path, 1, str(encoder))
    status = json.loads((tmp_path / 'sample.mp3.status.json').read_text())
    assert result == (failure is None)
    assert status['status'] == ('ready' if result else 'failed')
    assert status.get('error') == {'encoder': 'conversion_failed', 'empty': 'empty_output',
                                   'disk': 'low_disk_space', None: None}[failure]
    assert wav.exists() == (not result)
    assert (tmp_path / 'sample.mp3').exists() == result
    assert not list(tmp_path.glob('.encoding-*'))


@pytest.mark.parametrize('failure, reason', [('missing', 'missing_input'), ('unexpected', 'conversion_failed')])
def test_recording_conversion_stores_only_known_failure_reasons(tmp_path, failure, reason):
    module = script('convert-recording')
    wav = tmp_path / 'sample.wav'
    wav.write_bytes(b'' if failure == 'missing' else b'WAV data')
    # A NUL in the encoder path makes subprocess raise ValueError('embedded null byte').
    assert not module.convert(wav, tmp_path, 1, 'fake\0lame')
    status = json.loads((tmp_path / 'sample.mp3.status.json').read_text())
    assert status == {'status': 'failed', 'error': reason}
    assert wav.exists()
    assert not (tmp_path / 'sample.mp3').exists()
    assert not list(tmp_path.glob('.encoding-*'))


def test_backup_verify_includes_wal_and_detects_corruption(tmp_path, monkeypatch):
    module = script('backup')
    db_path = tmp_path / 'live.db'
    db = sqlite3.connect(db_path)
    db.execute('pragma journal_mode=WAL')
    db.execute('create table messages (body text)')
    db.execute("insert into messages values ('committed in WAL')")
    db.commit()
    folders = [tmp_path / name for name in ('recordings', 'ingress', 'cdr')]
    for folder in folders:
        folder.mkdir()
        (folder / 'example').write_text('preserved')
    env = tmp_path / '.env'
    env.write_text('secret backup')
    destination = tmp_path / 'backup'
    module.backup(destination, db_path, *folders, env, min_free_bytes=1)
    db.close()
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in destination.rglob('*') if p.is_file()}
    def forbidden(*args, **kwargs):
        raise AssertionError('Verification must not copy, stage or require disk space')
    monkeypatch.setattr(module.shutil, 'disk_usage', forbidden)
    monkeypatch.setattr(module.shutil, 'copytree', forbidden)
    monkeypatch.setattr(module.tempfile, 'mkdtemp', forbidden)
    module.verify(destination)
    module.verify(destination)  # Verification leaves the manifest valid for another read.
    assert set(before) == {p for p in destination.rglob('*') if p.is_file()}
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == saved for p, saved in before.items())
    with sqlite3.connect((destination / 'phonelet.db').as_uri() + '?mode=ro&immutable=1', uri=True) as connection:
        assert connection.execute('select body from messages').fetchone() == ('committed in WAL',)
    assert (destination / 'sms-ingress/example').read_text() == 'preserved'
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == saved for p, saved in before.items())
    (destination / 'recordings/example').write_text('corrupted')
    with pytest.raises(ValueError, match='checksum'):
        module.verify(destination)


def test_backup_low_disk_and_symlink_guards(tmp_path, monkeypatch):
    module = script('backup')
    inputs = [tmp_path / name for name in ('db', 'recordings', 'ingress', 'cdr', 'env')]
    for path in inputs: path.write_text('data')
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(ValueError, match='disk space'):
        module.backup(tmp_path / 'backup', *inputs)
    assert not (tmp_path / 'backup').exists()
    inputs[1].unlink()
    inputs[1].symlink_to(inputs[0])
    with pytest.raises(ValueError, match='Symlinks'):
        module.backup(tmp_path / 'backup', *inputs)


def test_converter_rejects_escaped_path_and_symlink_output(tmp_path):
    module = script('convert-recording')
    root = tmp_path / 'recordings'
    root.mkdir()
    outside = tmp_path / 'outside.wav'
    outside.write_text('private')
    with pytest.raises(ValueError):
        module.convert(outside, root)
    (root / 'sample.wav').write_text('wav')
    (root / 'sample.mp3').symlink_to(outside)
    with pytest.raises(ValueError):
        module.convert(root / 'sample.wav', root)
    assert outside.read_text() == 'private'


@pytest.mark.parametrize('alias_kind', ['symlink', 'parent_segments'])
def test_nested_destination_alias_rejected_before_staging_or_copy(tmp_path, monkeypatch, alias_kind):
    module = script('backup')
    recordings, ingress, cdr = [tmp_path / name for name in ('recordings', 'ingress', 'cdr')]
    for path in (recordings, ingress, cdr):
        path.mkdir()
        (path / 'existing').write_text('retained')
    db, env = tmp_path / 'live.db', tmp_path / '.env'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE messages (body TEXT)')
    env.write_text('private environment')
    source = recordings
    if alias_kind == 'symlink':
        parent = tmp_path / 'alias'
        parent.symlink_to(source, target_is_directory=True)
    else:
        outside = tmp_path / 'outside'
        outside.mkdir()
        parent = outside / '..' / source.name
    destination = parent / 'nested-output'
    assert destination.resolve().parent == source.resolve()
    attempted = []

    def forbid_staging_or_copy(*args, **kwargs):
        attempted.append(True)
        raise AssertionError('Nested destination reached staging/copy')

    monkeypatch.setattr(module.tempfile, 'mkdtemp', forbid_staging_or_copy)
    monkeypatch.setattr(module.shutil, 'copytree', forbid_staging_or_copy)
    monkeypatch.setattr(module.shutil, 'copyfile', forbid_staging_or_copy)
    with pytest.raises(ValueError, match='cannot be inside'):
        module.backup(destination, db, recordings, ingress, cdr, env, min_free_bytes=1)
    assert attempted == []
    assert not destination.exists()
    assert not list(source.glob('.phonelet-*'))
    assert (recordings / 'existing').read_text() == 'retained'


def test_verify_source_symlink_still_rejected(tmp_path):
    module = script('backup')
    source = tmp_path / 'source'
    source.mkdir()
    alias = tmp_path / 'source-alias'
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match='Symlinks are forbidden'):
        module.verify(alias)


def test_package_metadata_names_existing_files_and_the_release_repository():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    for name in [project['readme'], *project['license-files']]:
        assert (ROOT / name).is_file(), name
    repository = project['urls']['Repository']
    downloads = re.findall(r'https://\S+', (ROOT / 'asterisk/artifacts.lock').read_text())
    assert downloads and all(url.startswith(repository + '/releases/download/') for url in downloads)
    assert 'Python 3.11 and 3.13' in (ROOT / 'requirements.lock').read_text().splitlines()[0]


def test_locks_are_hashed_and_every_install_requires_hashes():
    for lock in ('requirements.lock', 'requirements-dev.lock'):
        entries = (ROOT / lock).read_text().replace('\\\n', ' ').splitlines()
        pins = [line for line in entries if '==' in line]
        assert pins and all(' --hash=sha256:' in line for line in pins), lock
    installs = []
    for path in ['scripts/release-common.sh', 'install.sh', 'deploy.sh', '.github/workflows/ci.yml',
                 '.github/workflows/audit.yml', 'docs/building.md', 'docs/install.md', 'docs/operations.md']:
        installs += [line for line in (ROOT / path).read_text().splitlines() if re.search(r'-r requirements', line)]
    assert len(installs) >= 5 and all('--require-hashes' in line for line in installs), installs


def install_guide_firewall_example():
    section = (ROOT / 'docs/install.md').read_text().split('\n## Firewall\n', 1)[1].split('\n## ', 1)[0]
    return re.search(r'^```nft\n(.*?)^```$', section, re.M | re.S).group(1)


def test_firewall_example_covers_sip_and_rtp_ports():
    ruleset = install_guide_firewall_example()
    assert 'policy accept;' in ruleset
    assert ruleset.count('udp dport { 5060, 10000-20000 }') == 3


def test_firewall_example_passes_nft_check(tmp_path):
    nft = shutil.which('nft') or shutil.which('nft', path='/usr/sbin:/sbin')
    if not nft:
        pytest.skip('nft is not installed')
    path = tmp_path / 'phonelet.nft'
    path.write_text(install_guide_firewall_example())
    result = subprocess.run([nft, '-c', '-f', str(path)], capture_output=True, text=True)
    if result.returncode and 'Operation not permitted' in result.stderr:
        pytest.skip('nft -c needs CAP_NET_ADMIN here')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('damage', ['database', 'nested-symlink', 'format'])
def test_backup_verify_rejects_invalid_snapshot_after_hash_check(tmp_path, damage):
    module = script('backup')
    source = tmp_path / 'backup'
    source.mkdir()
    database = source / 'phonelet.db'
    if damage == 'database':
        database.write_bytes(b'synthetic invalid SQLite database')
    else:
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE messages (body TEXT)')
    files = {'phonelet.db': module.digest(database)}
    if damage == 'nested-symlink':
        folder = source / 'recordings'
        folder.mkdir()
        (folder / 'alias').symlink_to(database)
    (source / 'manifest.json').write_text(json.dumps({'version': 2 if damage == 'format' else 1, 'sha256': files}))
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        module.verify(source)


def test_backup_min_free_bytes_belongs_only_to_backup(tmp_path):
    module = script('backup')
    db = tmp_path / 'live.db'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE messages (body TEXT)')
    directories = [tmp_path / name for name in ('recordings', 'ingress', 'cdr')]
    for directory in directories:
        directory.mkdir()
    env = tmp_path / 'environment'
    env.write_text('synthetic configuration\n')
    destination = tmp_path / 'backup'
    arguments = ['--destination', str(destination), '--db', str(db), '--env', str(env), '--services-stopped']
    for name, directory in zip(('recordings', 'ingress', 'cdr'), directories):
        arguments.extend(['--' + name, str(directory)])

    def run(*arguments):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/backup.py'), *arguments],
                              capture_output=True, text=True)

    invalid = run('backup', '--min-free-bytes', '0', *arguments)
    assert invalid.returncode == 2 and 'min-free-bytes must be positive' in invalid.stderr
    assert not destination.exists()
    saved = run('backup', '--min-free-bytes', '1', *arguments)
    assert saved.returncode == 0, saved.stderr
    verified = run('verify', str(destination))
    assert verified.returncode == 0, verified.stderr
    assert '--min-free-bytes' in run('backup', '--help').stdout
    assert '--min-free-bytes' not in run('verify', '--help').stdout
    for arguments in (['--min-free-bytes', '0', 'verify', str(destination)],
                      ['verify', str(destination), '--min-free-bytes', '0']):
        rejected = run(*arguments)
        assert rejected.returncode == 2
    module.verify(destination)
