#!/usr/bin/env python3
"""Offline backup into a NEW directory and in-place backup verification."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

DEFAULT_MIN_FREE_BYTES = 100 * 1024 * 1024  # default of PHONELET_MIN_FREE_BYTES
# File names inside a backup directory, shared by backup and verification.
MANIFEST = 'manifest.json'
DB_NAME = 'phonelet.db'


def size_tree(path):
    """Total file size under path; raises ValueError on any symlink (callers rely on this check)."""
    if path.is_symlink():
        raise ValueError('Symlinks are forbidden in backup input')
    if path.is_file():
        return path.stat().st_size
    return sum(size_tree(item) for item in path.iterdir())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_integrity(path):
    # SQLite's backup API produces a complete snapshot. Immutable reads avoid creating or changing
    # WAL/SHM sidecars when verifying it, which would invalidate the backup's own manifest.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')


def fsync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(root):
    for path in root.rglob('*'):
        fsync_path(path)
    fsync_path(root)


@contextmanager
def staged_directory(destination, prefix):
    """Yield a staging dir beside destination; publish it atomically on success."""
    staging = Path(tempfile.mkdtemp(prefix=prefix, dir=destination.parent))
    try:
        yield staging
        sync_tree(staging)
        os.rename(staging, destination)
        fsync_path(destination.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def backup(destination, db, recordings, ingress, cdr, env, min_free_bytes=DEFAULT_MIN_FREE_BYTES):
    destination = Path(destination).absolute()
    db = Path(db).absolute()
    if destination.exists():
        raise ValueError('Backup destination already exists')
    inputs = {'recordings': Path(recordings), 'sms-ingress': Path(ingress), 'cdr': Path(cdr), 'environment': Path(env)}
    if any(destination.resolve().is_relative_to(path.resolve()) for path in inputs.values() if path.is_dir()):
        raise ValueError('Backup destination cannot be inside its inputs')
    needed = size_tree(db) * 3 + sum(size_tree(p) for p in inputs.values())
    if shutil.disk_usage(destination.parent).free < needed + min_free_bytes:
        raise ValueError('Insufficient backup disk space')
    with staged_directory(destination, '.phonelet-backup-') as staging:
        # SQLite backup incorporates WAL; copying only the .db file would lose commits.
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as source, \
                closing(sqlite3.connect(staging / DB_NAME)) as target:
            source.backup(target)
        check_integrity(staging / DB_NAME)
        for name, path in inputs.items():
            if path.is_dir():
                shutil.copytree(path, staging / name)
            else:
                shutil.copyfile(path, staging / name)
        files = {str(p.relative_to(staging)): digest(p) for p in staging.rglob('*') if p.is_file()}
        (staging / MANIFEST).write_text(json.dumps({'version': 1, 'sha256': files}, indent=2))


def verify(source):
    source = Path(source).absolute()
    size_tree(source)  # Reject symlinks, including nested ones.
    manifest = json.loads((source / MANIFEST).read_text())
    if manifest.get('version') != 1:
        raise ValueError('Unsupported backup format')
    actual = {str(p.relative_to(source)): digest(p) for p in source.rglob('*')
              if p.is_file() and p != source / MANIFEST}
    if actual != manifest['sha256']:
        raise ValueError('Backup checksum mismatch')
    check_integrity(source / DB_NAME)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    save = commands.add_parser('backup')
    save.add_argument('--min-free-bytes', type=int, default=DEFAULT_MIN_FREE_BYTES)
    for name in ('destination', 'db', 'recordings', 'ingress', 'cdr', 'env'):
        save.add_argument('--' + name, required=True)
    save.add_argument('--services-stopped', action='store_true', required=True,
                      help='Confirm Asterisk and Phonelet are stopped for a consistent file snapshot')
    load = commands.add_parser('verify')
    load.add_argument('source')
    args = vars(parser.parse_args())
    action = args.pop('action')
    args.pop('services_stopped', None)
    if action == 'verify':
        verify(**args)
    else:
        if args['min_free_bytes'] <= 0:
            parser.error('min-free-bytes must be positive')
        backup(**args)
