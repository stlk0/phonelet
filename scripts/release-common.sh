#!/bin/bash
# Shared release identity, contents and host-side staging. Call from the source tree.
release_revision() {
  local toplevel
  REVISION=unknown
  if toplevel=$(git rev-parse --show-toplevel 2>/dev/null) && [[ "$toplevel" == "$(pwd -P)" ]]; then
    REVISION=$(git rev-parse --short=12 HEAD 2>/dev/null) || REVISION=unknown
  fi
  if [[ "$REVISION" != unknown ]]; then
    if ! REVISION_STATUS=$(git status --porcelain --untracked-files=normal) || [[ -n "$REVISION_STATUS" ]]; then
      REVISION+=-dirty
    fi
  elif [[ -f archive-revision ]]; then
    REVISION=$(< archive-revision)
    [[ "$REVISION" =~ ^[0-9a-f]{40}$ ]] || REVISION=unknown
  fi
  [[ "$REVISION" =~ ^([0-9a-f]{12,40}(-dirty)?|unknown)$ ]] || {
    echo 'Invalid release revision' >&2
    exit 1
  }
}

release_archive() {
  tar --exclude='__pycache__' -czf "$1" src asterisk scripts pyproject.toml requirements.lock
}

release_directory() {
  (umask 077; mkdir -p /root/phonelet-releases; mktemp -d /root/phonelet-releases/release.XXXXXXXX)
}

require_release_environment() {
  if [[ ! -f /root/phonelet/.env ]]; then
    echo 'Missing /root/phonelet/.env on host; run install.sh first (docs/install.md).' >&2
    exit 1
  fi
}

stage_release() (
  cd "$1" || exit
  tar --no-same-owner -xzf release.tar.gz
  printf '%s\n' "$2" > REVISION
  if [[ "${3:-update}" != first ]]; then require_release_environment; fi
  python3 -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check --no-deps --require-hashes -r requirements.lock
)
