#!/bin/bash
set -euo pipefail
umask 022
usage() { echo 'Usage: fetch-asterisk.sh ARCH DIR [ARCHIVE_FILE] (ARCH: arm64 or amd64; DIR must not exist)' >&2; exit 2; }
die() { echo "$*" >&2; exit 1; }
(( $# == 2 || $# == 3 )) || usage
[[ "$1" == arm64 || "$1" == amd64 ]] || usage
[[ -n "$2" && ! -e "$2" && ! -L "$2" ]] || usage
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR=$(realpath -m -- "$2")
mapfile -t entries < <(awk -v arch="$1" '$1 == arch {print}' "$ROOT/asterisk/artifacts.lock")
(( ${#entries[@]} == 1 )) || die "Expected exactly one artifacts.lock entry for $1"
read -r arch archive_sum asterisk_sum dongle_sum url extra <<< "${entries[0]}"
[[ -z "$extra" && "$archive_sum" =~ ^[0-9a-f]{64}$ && "$asterisk_sum" =~ ^[0-9a-f]{64}$ &&
   "$dongle_sum" =~ ^[0-9a-f]{64}$ && "$url" == https://* ]] || die "Malformed artifacts.lock entry for $arch"
[[ ! "$archive_sum" =~ ^0+$ ]] || die "Asterisk build for $arch not published yet: artifacts.lock has placeholder sums"
NAME=${url##*/}
[[ "$NAME" =~ ^asterisk-([0-9A-Za-z.]+)-$arch\.tar\.gz$ ]] || die "Invalid archive name: $NAME"
VERSION=$(awk -F= '$1 == "ASTERISK_VERSION" {print $2}' "$ROOT/asterisk/sources.lock")
[[ "${BASH_REMATCH[1]}" == "$VERSION" ]] || die "Archive version does not match sources.lock: $NAME"
WORK=$(mktemp -d "$DIR.tmp.XXXXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
if (( $# == 3 )); then
  cp -- "$3" "$WORK/archive.tmp"
else
  curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' --output "$WORK/archive.tmp" "$url"
fi
actual=$(sha256sum "$WORK/archive.tmp" | cut -d' ' -f1)
[[ "$actual" == "$archive_sum" ]] || die "Checksum mismatch for $NAME: expected $archive_sum, got $actual"
mv -- "$WORK/archive.tmp" "$WORK/$NAME"
printf '%s  %s\n' "$archive_sum" "$NAME" "$asterisk_sum" stage/usr/sbin/asterisk \
  "$dongle_sum" stage/usr/lib/asterisk/modules/chan_dongle.so > "$WORK/artifacts.sha256"
[[ ! -e "$DIR" && ! -L "$DIR" ]] || die "Destination already exists: $DIR"
mv -T -- "$WORK" "$DIR"
printf '%s\n' "$DIR"
