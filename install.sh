#!/bin/bash
set -euo pipefail
# Run on the destination host, from a checkout or an unpacked source archive.
[[ "$(id -u)" == 0 ]] || { echo 'Run install.sh as root on the host itself.' >&2; exit 1; }
umask 077
usage() {
  echo 'Usage: install.sh [--asterisk-archive FILE | --asterisk-build DIR] [--country-prefix P [--trunk-prefix D] --sip-domain ADDR] [--check | --allow-asterisk-restart | --restart-asterisk]' >&2
  exit 2
}
die() { echo "$*" >&2; exit 1; }
# Values never start with --, so only an option name can equal one.
answered() { local item; for item in "${ANSWERS[@]}"; do [[ "$item" != "$1" ]] || return 0; done; return 1; }
MODE=
BUILD_DIR=
LOCAL_ARCHIVE=
ANSWERS=()
while (( $# )); do
  case "$1" in
    --asterisk-archive|--asterisk-build)
      (( $# >= 2 )) && [[ -n "$2" && "$2" != --* && -z "$BUILD_DIR$LOCAL_ARCHIVE" ]] || usage
      if [[ "$1" == --asterisk-build ]]; then BUILD_DIR=$2; else LOCAL_ARCHIVE=$2; fi
      shift 2 ;;
    --country-prefix|--trunk-prefix|--sip-domain)
      (( $# >= 2 )) && [[ -n "$2" && "$2" != --* ]] || usage
      ! answered "$1" || usage
      ANSWERS+=("$1" "$2")
      shift 2 ;;
    --check|--allow-asterisk-restart|--restart-asterisk)
      [[ -z "$MODE" ]] || usage
      MODE=$1
      shift ;;
    *) usage ;;
  esac
done
# One install.sh at a time: the installer sweeps the work directories of interrupted runs, which
# would include another run's. /run exists on a fresh host and only root writes there. Held until exit.
exec {INSTALL_LOCK}>>/run/phonelet-installer.lock
flock -n "$INSTALL_LOCK" || die 'Refusing: another install.sh is running (it holds /run/phonelet-installer.lock); nothing changed'
FIRST=first
[[ ! -e /etc/systemd/system/phonelet.service.d/20-phonelet-release.conf ]] || FIRST=update
if [[ "$FIRST" == first ]]; then
  [[ -z "$MODE" ]] || usage
else
  (( ${#ANSWERS[@]} == 0 )) || { echo 'Answer options apply to first installation only.' >&2; usage; }
  # An Asterisk upgrade (docs/operations.md, "Upgrading Asterisk") changes the host: no --check.
  [[ -z "$BUILD_DIR$LOCAL_ARCHIVE" || "$MODE" != --check ]] || {
    echo 'An Asterisk upgrade cannot run with --check.' >&2
    usage
  }
fi
ENV_FILE=/root/phonelet/.env
if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
  (( ${#ANSWERS[@]} == 0 )) || { echo 'Answer options require a new .env file.' >&2; usage; }
elif [[ "$FIRST" == first && ! -t 0 ]] && ! { answered --country-prefix && answered --sip-domain; }; then
  echo '--country-prefix and --sip-domain are required when stdin is not a TTY (--trunk-prefix is optional).' >&2
  usage
fi
CHECKOUT=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/release-common.sh
source "$CHECKOUT/scripts/release-common.sh"
if [[ "$FIRST" == first ]]; then
  TARGET_DIR=
  PHONELET_BOOTSTRAP_TARGET=${PHONELET_BOOTSTRAP_TARGET:-}
  PHONELET_BOOTSTRAP_WIFI=${PHONELET_BOOTSTRAP_WIFI:-0}
  PHONELET_BOOTSTRAP_MODEM_USB_ID=${PHONELET_BOOTSTRAP_MODEM_USB_ID:-12d1:1001}
  if [[ -n "$PHONELET_BOOTSTRAP_TARGET" ]]; then
    [[ "$PHONELET_BOOTSTRAP_TARGET" =~ ^[a-z0-9][a-z0-9-]*$ ]] || usage
    TARGET_DIR="$CHECKOUT/targets/$PHONELET_BOOTSTRAP_TARGET"
    [[ -d "$TARGET_DIR" ]] || { echo "Unknown bootstrap target: $PHONELET_BOOTSTRAP_TARGET" >&2; usage; }
    [[ -f "$TARGET_DIR/profile.sh" ]] || {
      echo "Target has no profile: run without PHONELET_BOOTSTRAP_TARGET and follow targets/$PHONELET_BOOTSTRAP_TARGET/README.md" >&2
      usage
    }
  fi
  [[ "$PHONELET_BOOTSTRAP_WIFI" == 0 || "$PHONELET_BOOTSTRAP_WIFI" == 1 ]] || usage
  [[ "$PHONELET_BOOTSTRAP_MODEM_USB_ID" =~ ^12d1:[0-9a-f]{4}$ ]] || usage
  export TARGET_DIR
  export PHONELET_BOOTSTRAP_WIFI PHONELET_BOOTSTRAP_MODEM_USB_ID
fi
if [[ "$FIRST" == update ]]; then require_release_environment; fi
if [[ "$FIRST" == first && -z "$BUILD_DIR$LOCAL_ARCHIVE" ]]; then
  command -v curl >/dev/null || die 'Missing curl: run apt-get install curl, then run the same command again.'
fi
# Resolve caller-relative inputs before entering the source tree.
if [[ -n "$BUILD_DIR" ]]; then BUILD_DIR=$(realpath -e -- "$BUILD_DIR") || die "Missing build directory: $BUILD_DIR"; fi
if [[ -n "$LOCAL_ARCHIVE" ]]; then LOCAL_ARCHIVE=$(realpath -e -- "$LOCAL_ARCHIVE") || die 'Missing Asterisk archive'; fi
cd "$CHECKOUT"
if [[ "$MODE" != --check ]]; then
  # Only root-owned directories with mktemp's exact suffix; neither top-level nor nested symlinks
  # are followed. Sweep both first installs and updates while the installer lock is held.
  for old in /root/phonelet-install.????????; do
    [[ "${old##*/}" =~ ^phonelet-install\.[a-zA-Z0-9]{8}$ ]] || continue
    if [[ -d "$old" && ! -L "$old" && "$(stat -c %u -- "$old")" == 0 ]]; then
      # Caller inputs may live in an interrupted work directory, including through an aliased /root.
      old_resolved=$(realpath -e -- "$old")
      [[ "$BUILD_DIR" != "$old_resolved" && "$BUILD_DIR" != "$old_resolved/"* && "$LOCAL_ARCHIVE" != "$old_resolved/"* ]] || continue
      rm -rf -- "$old"
    fi
  done
fi
WORK=
RELEASE=
REBOOT=0
cleanup() {
  local status=$?
  trap - EXIT
  [[ -z "$WORK" ]] || rm -rf -- "$WORK"
  if [[ "$MODE" == --check && -n "$RELEASE" ]]; then rm -rf -- "$RELEASE"; fi
  if (( REBOOT )); then exit 3; fi
  if (( status )); then exit 1; fi
  exit 0
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
if [[ "$FIRST" == first || -n "$BUILD_DIR$LOCAL_ARCHIVE" ]]; then
  WORK=$(mktemp -d /root/phonelet-install.XXXXXXXX)
  if [[ -z "$BUILD_DIR" ]]; then
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in arm64|amd64) ;; *) die "Unsupported host architecture: $ARCH (expected arm64 or amd64)" ;; esac
    FETCH_ARGS=("$ARCH" "$WORK/asterisk")
    [[ -z "$LOCAL_ARCHIVE" ]] || FETCH_ARGS+=("$LOCAL_ARCHIVE")
    bash scripts/fetch-asterisk.sh "${FETCH_ARGS[@]}"
    BUILD_DIR="$WORK/asterisk"
  fi
  shopt -s nullglob
  archives=("$BUILD_DIR"/asterisk-*.tar.gz)
  shopt -u nullglob
  (( ${#archives[@]} == 1 )) || die "Expected one asterisk-VERSION-ARCH.tar.gz (arm64 or amd64) in $BUILD_DIR"
  ARCHIVE=${archives[0]}
  ARCHIVE_NAME=${ARCHIVE##*/}
  [[ "$ARCHIVE_NAME" =~ ^asterisk-[0-9A-Za-z.]+-(arm64|amd64)\.tar\.gz$ ]] || die "Unsupported Asterisk archive: $ARCHIVE_NAME (expected arm64 or amd64)"
  [[ -f "$BUILD_DIR/artifacts.sha256" ]] || die "Missing $BUILD_DIR/artifacts.sha256"
  if [[ "$BUILD_DIR" != "$WORK/asterisk" ]]; then
    # Verify private copies: the caller may rebuild or replace the source files while installation waits.
    mkdir "$WORK/asterisk"
    cp -- "$ARCHIVE" "$BUILD_DIR/artifacts.sha256" "$WORK/asterisk/"
    BUILD_DIR="$WORK/asterisk"
    ARCHIVE="$BUILD_DIR/$ARCHIVE_NAME"
  fi
  expected=$(awk -v name="$ARCHIVE_NAME" '$2 == name {print $1}' "$BUILD_DIR/artifacts.sha256")
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || die "No checksum for $ARCHIVE_NAME in artifacts.sha256"
  actual=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
  [[ "$actual" == "$expected" ]] || die "Checksum mismatch for $ARCHIVE_NAME"
  # bootstrap.sh counts an installed Asterisk as ours only when both binaries have these sums (an interrupted
  # unpack is redone): check them before any change.
  for binary in stage/usr/sbin/asterisk stage/usr/lib/asterisk/modules/chan_dongle.so; do
    [[ "$(awk -v name="$binary" '$2 == name {print $1}' "$BUILD_DIR/artifacts.sha256")" =~ ^[0-9a-f]{64}$ ]] ||
      die "Missing or malformed $binary checksum"
  done
fi
if [[ "$FIRST" == first ]]; then
  # Preserve the profile's reboot status; no package or configuration changes precede prepare.
  status=0
  bash scripts/bootstrap.sh prepare "$ARCHIVE_NAME" || status=$?
  if (( status == 3 )); then REBOOT=1; exit 3; fi
  (( status == 0 )) || exit 1
  if ! dpkg-query -W -f='${Status}\n' python3 python3-venv 2>/dev/null | \
      { mapfile -t packages; [[ ${#packages[@]} == 2 && "${packages[0]}" == 'install ok installed' && "${packages[1]}" == 'install ok installed' ]]; }; then
    (
      umask 022
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y --no-install-recommends python3 python3-venv
    )
  fi
fi
if [[ "$FIRST" == update && -n "$BUILD_DIR" ]]; then
  # deploy-release.py's lock: no Asterisk upgrade under another deployment. Held from here through the
  # application deploy, which locks this inherited descriptor (PHONELET_DEPLOY_LOCK_FD) instead of the file.
  exec {DEPLOY_LOCK}>>/root/phonelet/.deploy.lock
  flock -n "$DEPLOY_LOCK" ||
    die 'Refusing the Asterisk upgrade: another deployment holds /root/phonelet/.deploy.lock; nothing changed'
  export PHONELET_DEPLOY_LOCK_FD=$DEPLOY_LOCK
fi
release_revision
RELEASE=$(release_directory)
release_archive "$RELEASE/release.tar.gz"
stage_release "$RELEASE" "$REVISION" "$FIRST"
PYTHON="$RELEASE/.venv/bin/python"
if [[ "$FIRST" == update && -n "$BUILD_DIR" ]]; then
  # The release is staged and .env passes the deploy's validation first: Asterisk is stopped only once
  # the application can follow (a recordings directory the new unit cannot write stops here).
  PRE_UPGRADE_FLAGS=()
  if [[ "$MODE" == --allow-asterisk-restart || "$MODE" == --restart-asterisk ]]; then
    PRE_UPGRADE_FLAGS+=(--allow-transport-change)
  fi
  env -i PATH="$PATH" HOME="$HOME" PYTHONPATH=src "$PYTHON" scripts/deploy-release.py \
    --check-env "$ENV_FILE" --stage "$RELEASE" "${PRE_UPGRADE_FLAGS[@]}"
  cp "$BUILD_DIR/artifacts.sha256" "$WORK/"
  bash scripts/bootstrap.sh upgrade "$WORK" "$ARCHIVE"
fi
if [[ "$FIRST" == first ]]; then
  if [[ ! -e "$ENV_FILE" ]]; then
    install -d -m 0700 /root/phonelet
    DEFAULT_SIP=
    read -r -a addresses <<< "$(hostname -I 2>/dev/null || true)"
    for address in "${addresses[@]}"; do
      if [[ "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then DEFAULT_SIP=$address; break; fi
    done
    PYTHONPATH="$RELEASE/src" "$PYTHON" "$RELEASE/scripts/deploy-release.py" --init-env "$ENV_FILE" \
      --sip-domain-default "$DEFAULT_SIP" "${ANSWERS[@]}"
  fi
  # Render with the deploy code, so the .env is validated exactly as deploy validates it. Errors name
  # keys, never values. Every deploy-release.py mode reads settings from the .env only, never from PHONELET_*
  # of the caller's shell.
  RENDER_FLAGS=()
  [[ "$PHONELET_BOOTSTRAP_WIFI" != 1 ]] || RENDER_FLAGS+=(--bootstrap-wifi)
  env -i PATH="$PATH" HOME="$HOME" PYTHONPATH=src "$PYTHON" scripts/deploy-release.py \
    --render-configs "$ENV_FILE" "$WORK/etc-asterisk" "${RENDER_FLAGS[@]}"
  cp "$BUILD_DIR/artifacts.sha256" "$WORK/"
  bash scripts/bootstrap.sh install "$WORK" "$ARCHIVE"
fi
cd "$RELEASE"
if [[ "$FIRST" == first ]]; then
  PYTHONPATH=src "$PYTHON" scripts/deploy-release.py --stage "$RELEASE" --check
fi
FLAGS=()
[[ -z "$MODE" ]] || FLAGS+=("$MODE")
PYTHONPATH=src "$PYTHON" scripts/deploy-release.py --stage "$RELEASE" "${FLAGS[@]}"
if [[ "$MODE" != --check ]]; then
  echo 'See Summary: above; configure the SIP phone, then test SMS and a call (docs/install.md).'
fi
