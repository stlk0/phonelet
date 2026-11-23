#!/bin/bash
# Provision private durable storage for the root services; safe to rerun, no service changes.
set -euo pipefail
USAGE='Usage: provision-storage.sh [STORAGE_ROOT]'
[[ "$(id -u)" == 0 ]] || { echo 'Run provision-storage.sh as root' >&2; exit 1; }
(( $# <= 1 )) || { echo "$USAGE" >&2; exit 2; }
STORAGE_ROOT=${1:-/}
STORAGE_ROOT=${STORAGE_ROOT%/}
[[ -z "$STORAGE_ROOT" || "$STORAGE_ROOT" == /* ]] || { echo 'STORAGE_ROOT must be absolute' >&2; exit 1; }
# DB files are private even with a legacy loose umask.
for suffix in '' -wal -shm; do
  database="$STORAGE_ROOT/var/lib/phonelet/phonelet.db$suffix"
  if [[ -e "$database" || -L "$database" ]]; then
    [[ -f "$database" && ! -L "$database" ]] || { echo 'Refusing non-regular database file' >&2; exit 1; }
    chown root:root "$database"
    chmod 0600 "$database"
  fi
done
# Private service data; both services run as root.
install -d -m 0710 -o root -g root "$STORAGE_ROOT/var/lib/phonelet"
install -d -m 2750 -o root -g root "$STORAGE_ROOT/var/spool/asterisk/phonelet-sms"
install -d -m 0750 -o root -g root \
  "$STORAGE_ROOT/var/log/asterisk/cdr-custom" \
  "$STORAGE_ROOT/var/lib/phonelet/recordings"
# cdr_custom creates its file only on the first call. Readiness needs an empty
# journal on an idle new host, without truncating retained history on reruns.
python3 - "$STORAGE_ROOT/var/log/asterisk/cdr-custom/phonelet.csv" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
except FileExistsError:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError('Refusing non-regular CDR journal')
    os.fchown(fd, 0, 0)
    os.fchmod(fd, 0o640)
    os.fsync(fd)
finally:
    os.close(fd)
directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
