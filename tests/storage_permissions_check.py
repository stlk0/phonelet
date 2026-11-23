"""Root-only check of scripts/provision-storage.sh, outside pytest.

Run as root ONLY inside a disposable container; every file is under a temporary staging root:
    python3 tests/storage_permissions_check.py scripts/provision-storage.sh
"""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

DB_SUFFIXES = ('', '-wal', '-shm')


def provision(script, root, **kwargs):
    return subprocess.run(['bash', script, str(root)], **kwargs)


def check(script):
    with tempfile.TemporaryDirectory(prefix='phonelet-permissions-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        parent = root / 'var/lib/phonelet'
        parent.mkdir(parents=True)
        for suffix in DB_SUFFIXES:
            path = parent / ('phonelet.db' + suffix)
            path.write_text('private history')
            path.chmod(0o644)  # Previously possible with inherited default umask.
        provision(script, root, check=True)
        journal = root / 'var/log/asterisk/cdr-custom/phonelet.csv'
        assert journal.is_file() and journal.read_bytes() == b''  # No call needed.
        assert journal.stat().st_uid == 0
        assert journal.stat().st_gid == 0
        assert stat.S_IMODE(journal.stat().st_mode) == 0o640
        journal.write_bytes(b'previous CDR history\n')
        provision(script, root, check=True)
        assert journal.read_bytes() == b'previous CDR history\n'
        assert parent.stat().st_uid == 0
        assert parent.stat().st_gid == 0
        assert stat.S_IMODE(parent.stat().st_mode) == 0o710
        spool = root / 'var/spool/asterisk/phonelet-sms'
        assert spool.stat().st_gid == 0
        assert stat.S_IMODE(spool.stat().st_mode) == 0o2750
        recordings = parent / 'recordings'
        assert recordings.stat().st_uid == 0
        for suffix in DB_SUFFIXES:
            path = parent / ('phonelet.db' + suffix)
            assert path.stat().st_uid == 0
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with journal.open('ab') as stream:
            stream.write(b'new CDR\n')
        year = recordings / '2026'
        year.mkdir()
        (year / 'call.wav').write_text('recording')
        assert journal.read_bytes() == b'previous CDR history\nnew CDR\n'
        journal.unlink()
        victim = root / 'must-not-follow'
        victim.write_bytes(b'untouched')
        journal.symlink_to(victim)
        rejected = provision(script, root, capture_output=True)
        assert rejected.returncode != 0
        assert journal.is_symlink() and victim.read_bytes() == b'untouched'
    print('PASS: root service ownership, private database modes and retained journals')


if __name__ == '__main__':
    assert os.geteuid() == 0, 'Run only in a disposable container as root'
    check(sys.argv[1])
