#!/bin/bash
set -euo pipefail
# Staging changes no running code/configuration. The remote engine owns rollback.
cd "$(dirname "$0")"
usage() { echo 'Usage: deploy.sh HOST [--check|--allow-asterisk-restart|--restart-asterisk]' >&2; exit 2; }
(( $# >= 1 )) || usage
HOST="$1"
[[ "$HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo 'Invalid host' >&2; exit 2; }
# --check runs every preflight check and prints the changes without applying them.
MODE="${2:-}"
[[ -z "$MODE" || "$MODE" == --allow-asterisk-restart || "$MODE" == --restart-asterisk || "$MODE" == --check ]] || usage
(( $# <= 2 )) || usage  # Never drop an extra --check and deploy for real.
# shellcheck source=scripts/release-common.sh
source scripts/release-common.sh
release_revision
REMOTE="root@$HOST"
STAGE=$(ssh "$REMOTE" "$(declare -f release_directory); release_directory")
[[ "$STAGE" =~ ^/root/phonelet-releases/release\.[a-zA-Z0-9]+$ ]] || {
  echo "Unexpected release directory from $HOST: $STAGE" >&2
  exit 1
}
ARCHIVE=
cleanup() {
  [[ -z "$ARCHIVE" ]] || rm -f "$ARCHIVE"
  # A check leaves no release behind, whether it passed or failed.
  [[ "$MODE" != --check ]] || ssh "$REMOTE" "rm -rf '$STAGE'" || echo "Could not remove check release $STAGE on $HOST" >&2
}
trap cleanup EXIT
ARCHIVE=$(mktemp)
release_archive "$ARCHIVE"
scp -q "$ARCHIVE" "$REMOTE:$STAGE/release.tar.gz"
ssh "$REMOTE" "set -eu
  cd '$STAGE'
  $(declare -f require_release_environment stage_release)
  stage_release '$STAGE' '$REVISION'
  PYTHONPATH=src .venv/bin/python scripts/deploy-release.py --stage '$STAGE' $MODE
"
