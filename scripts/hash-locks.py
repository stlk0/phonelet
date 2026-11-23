#!/usr/bin/env python3
"""Rewrite the lock files with the PyPI sha256 of every file published for each pinned version.

Run from the repository root: python3 scripts/hash-locks.py [LOCK ...]
Versions are not changed; only the --hash lines are regenerated (docs/building.md).
"""
import json
from pathlib import Path
import re
import sys
import urllib.request

LOCKS = ('requirements.lock', 'requirements-dev.lock')
PIN = re.compile(r'^([A-Za-z0-9._-]+)==([A-Za-z0-9.+!-]+)$')


def published_hashes(name, version):
    with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=60) as response:
        files = json.load(response)['urls']
    if not files:
        raise SystemExit(f'{name}=={version}: no files on PyPI')
    return sorted(f['digests']['sha256'] for f in files)


def rehash(path):
    # Join continuation lines and drop old hashes, keeping comments, -r lines and the order.
    logical = Path(path).read_text().replace('\\\n', ' ').splitlines()
    out = []
    for line in logical:
        requirement = line.split(' --hash=')[0].strip()
        pin = PIN.match(requirement)
        if not pin:
            out.append(line)
            continue
        out.append(' \\\n'.join([requirement] + [f'    --hash=sha256:{h}' for h in published_hashes(*pin.groups())]))
    Path(path).write_text('\n'.join(out) + '\n')


if __name__ == '__main__':
    for lock in sys.argv[1:] or LOCKS:
        rehash(lock)
