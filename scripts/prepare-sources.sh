#!/bin/bash
# Fetch and patch reproducible source trees only. Does not build/install/restart.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST=${1:?Usage: prepare-sources.sh NEW_DESTINATION}
[[ ! -e "$DEST" ]] || { echo 'Destination must not exist' >&2; exit 1; }
source "$ROOT/asterisk/sources.lock"
fetch_pinned() {  # DIR REPOSITORY COMMIT
  git init -q "$1"
  git -C "$1" fetch --depth 1 "$2" "$3"
  git -C "$1" checkout --detach FETCH_HEAD
  [[ "$(git -C "$1" rev-parse HEAD)" == "$3" ]]
}
mkdir -p "$DEST"
fetch_pinned "$DEST/asterisk" "$ASTERISK_REPOSITORY" "$ASTERISK_COMMIT"
fetch_pinned "$DEST/chan-dongle" "$CHAN_DONGLE_REPOSITORY" "$CHAN_DONGLE_COMMIT"
# chan_dongle_patch/series: the apply order, one short patch name per line.
mapfile -t PATCHES < "$ROOT/chan_dongle_patch/series"
# Each patch must apply exactly: git apply never fuzzes, but takes an offset silently without --verbose.
for name in "${PATCHES[@]}"; do
  if ! applied=$(LC_ALL=C git -C "$DEST/chan-dongle" apply --verbose "$ROOT/chan_dongle_patch/chan_dongle_$name.patch" 2>&1); then
    printf '%s\n' "$applied" >&2
    echo "Patch $name does not apply" >&2
    exit 1
  fi
  if grep -Eq '\(offset -?[0-9]+ lines?\)' <<< "$applied"; then
    printf '%s\n' "$applied" >&2
    echo "Patch $name applies only at an offset: refresh it against the pinned sources" >&2
    exit 1
  fi
  echo "Patch $name applied without offsets"
done
sha256sum "$ROOT/asterisk/sources.lock" "$ROOT/chan_dongle_patch/series" "$ROOT/chan_dongle_patch/"*.patch > "$DEST/applied-sources.sha256"
echo "Source trees prepared under $DEST; see docs/building.md before build/install."
