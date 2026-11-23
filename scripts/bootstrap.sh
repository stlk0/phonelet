#!/bin/bash
set -euo pipefail
# Backend of install.sh, run as root on the Debian host:
#   prepare [ARCHIVE]      checks the host/archive (selected profile may disable ramlog)
#   install STAGE ARCHIVE  installs using rendered configuration in the install work directory
#   upgrade STAGE ARCHIVE  replaces the bootstrapped Asterisk with the verified private copy of the archive
# install and upgrade read the build's sums of asterisk and chan_dongle.so from STAGE/artifacts.sha256.
# install.sh validates bootstrap settings and exports TARGET_DIR from the checkout for first install only.
umask 022
CHECKOUT=$(cd "$(dirname "$0")/.." && pwd)
RELEASE_DROPIN=/etc/systemd/system/phonelet.service.d/20-phonelet-release.conf
ASTERISK_UNIT=/usr/lib/systemd/system/asterisk.service
ASTERISK_BIN=/usr/sbin/asterisk
CHAN_DONGLE=/usr/lib/asterisk/modules/chan_dongle.so
# Written before the archive is unpacked: an Asterisk without it is not ours and is never replaced.
ASTERISK_MARKER=/var/lib/phonelet/asterisk-bootstrap
SMS_JOURNAL_DIR=/var/spool/asterisk/phonelet-sms
die() { echo "$*" >&2; exit 1; }

if [[ "${1:-}" != upgrade && -n "${TARGET_DIR:-}" ]]; then
  # Profiles define only target_* hooks; they must not reuse core function names defined below.
  # shellcheck disable=SC1091
  source "$TARGET_DIR/profile.sh"
fi

# Journals must survive a power loss; never change mount configuration here.
require_persistent() {
  local path=$1 fstype source
  while [[ ! -e "$path" ]]; do path=$(dirname -- "$path"); done
  read -r fstype source < <(findmnt -n -o FSTYPE,SOURCE --target "$path" || true) || true
  case "$fstype:$source" in
    tmpfs:*|ramfs:*|*:/dev/zram*) die "$1 is on tmpfs, ramfs or zram; use persistent $1 storage" ;;
  esac
}

check_architecture() {  # [ARCHIVE]
  case "$(uname -m)" in aarch64|x86_64) ;; *) die 'Not an arm64 (aarch64) or amd64 (x86_64) host' ;; esac
  local arch archive_arch
  arch=$(dpkg --print-architecture)
  case "$arch" in arm64|amd64) ;; *) die "Unsupported host architecture: $arch (expected arm64 or amd64)" ;; esac
  if [[ -n "${1:-}" ]]; then
    [[ "$1" =~ ^asterisk-[0-9A-Za-z.]+-(arm64|amd64)\.tar\.gz$ ]] ||
      die "Unsupported Asterisk archive: $1 (expected arm64 or amd64)"
    archive_arch=${BASH_REMATCH[1]}
    [[ "$archive_arch" == "$arch" ]] ||
      die "Asterisk archive architecture $archive_arch does not match host architecture $arch"
  fi
}

check_host() {
  # The udev rules, the modem ports and the native units need the host itself.
  if systemd-detect-virt --container --quiet; then die 'Containers are not supported'; fi
  check_architecture "${2##*/}"
  grep -qx 'ID=debian' /etc/os-release && grep -qx 'VERSION_CODENAME=trixie' /etc/os-release ||
    die 'Not Debian 13 (trixie)'
  # ModemManager would open the modem ports. The package is the owner's to remove.
  local units
  units=$(systemctl list-unit-files --no-legend)
  if grep -qi modemmanager <<<"$units"; then die 'remove ModemManager first'; fi
  [[ ! -e "$RELEASE_DROPIN" ]] || die 'already bootstrapped, use install.sh (docs/install.md)'
  if [[ ! -e "$ASTERISK_MARKER" ]] &&
     [[ -e "$ASTERISK_BIN" || -n "$(find /etc/asterisk -mindepth 1 -print -quit 2>/dev/null)" ]]; then
    die "Another Asterisk installation found ($ASTERISK_BIN or a non-empty /etc/asterisk): bootstrap does not" \
      'replace it. Remove it yourself or install manually (docs/install.md, "Manual installation")'
  fi
  if declare -F target_check_storage >/dev/null; then target_check_storage "${1:-}"; fi
  require_persistent /var/log  # CDR journal
  require_persistent "$SMS_JOURNAL_DIR"
}

wait_for_asterisk() {
  local _
  for _ in $(seq 30); do
    asterisk -rx 'core waitfullybooted' >/dev/null 2>&1 && return 0
    sleep 2
  done
  die 'Asterisk did not start'
}

file_has_sum() {
  [[ -f "$1" && "$(sha256sum < "$1" | cut -d' ' -f1)" == "$2" ]]
}

# Installed means ours (check_host refused any other) and exactly this build: else unpack again.
asterisk_installed() {
  [[ -e "$ASTERISK_MARKER" ]] && file_has_sum "$ASTERISK_BIN" "$1" && file_has_sum "$CHAN_DONGLE" "$2"
}

# A running Asterisk that asterisk.service does not track is not ours to replace or to start beside.
require_no_untracked_asterisk() {
  local running main
  running=$(pidof asterisk || true)
  [[ -n "$running" ]] || return 0
  main=$(systemctl show -p MainPID --value asterisk)
  [[ "$running" == "$main" ]] ||
    die "Asterisk (PID $running) runs outside asterisk.service: stop it, then run install.sh again"
}

# Unpacked size of ARCHIVE, plus a margin for blocks, inodes and the logs of the start, must fit in each
# filesystem it writes to (replaced files are not counted as freed).
require_space_for_archive() {  # ARCHIVE
  local size avail
  size=$(tar -tvzf "$1" | awk '{ total += $3 } END { print total + 0 }')
  size=$(( size + size / 10 + 16 * 1024 * 1024 ))
  for avail in $(df --output=avail -B1 / /usr /var | tail -n +2); do
    (( avail > size )) || die "Not enough free space to unpack $1 ($size bytes); nothing changed"
  done
}

# Unpacks a verified ARCHIVE over /. The caller owns its lifetime.
unpack_asterisk() {  # ARCHIVE [TAR_OPTION...]
  local archive=$1
  shift
  mkdir -p "${ASTERISK_MARKER%/*}"  # provision-storage.sh sets its final mode
  printf '%s\n' "${archive##*/}" > "$ASTERISK_MARKER"
  # Skip the archive's ./var/run/: /run is tmpfs and Asterisk creates /var/run/asterisk at start;
  # extracting it fails on some tar versions ("Invalid cross-device link"). Keep any other directory symlink.
  tar -C / --exclude=./var/run "$@" --keep-directory-symlink --no-overwrite-dir -xzf "$archive"
  ldconfig
}

# Starts the native unit and checks that systemd tracks the one running daemon.
start_native_asterisk() {
  local running main
  systemctl daemon-reload
  [[ "$(systemctl show -p FragmentPath --value asterisk)" == "$ASTERISK_UNIT" ]] ||
    die "asterisk.service is not loaded from $ASTERISK_UNIT"
  systemctl enable asterisk
  systemctl reset-failed asterisk
  systemctl start asterisk
  wait_for_asterisk
  running=$(pidof asterisk || true)
  main=$(systemctl show -p MainPID --value asterisk)
  [[ -n "$running" && "$running" == "$main" ]] || die "systemd MainPID ($main) and pidof asterisk ($running) differ"
}

# Stops Asterisk without active calls and waits for the daemon to exit before anything is replaced.
# Error paths start Asterisk again: nothing there may abort first.
stop_asterisk() {
  local calls _
  if pidof asterisk >/dev/null; then
    calls=$(asterisk -rx 'core show channels count') || die 'Asterisk does not answer; nothing changed'
    grep -Eq '^0 active calls' <<<"$calls" || die 'Asterisk has active calls; nothing changed'
  fi
  if ! systemctl stop asterisk; then
    systemctl start asterisk || true
    die 'systemctl stop asterisk failed; nothing changed'
  fi
  for _ in $(seq 60); do
    pidof asterisk >/dev/null || return 0
    sleep 1
  done
  systemctl start asterisk || true
  die 'Asterisk did not stop within 60 s: check pidof asterisk; nothing changed'
}

# Once Asterisk is stopped, any failure (set -e or die) still starts whatever asterisk.service now is.
start_asterisk_on_failure() {
  trap 'systemctl daemon-reload || true; systemctl start asterisk || true
        echo "Asterisk installation failed after it was stopped; asterisk.service was started again" >&2' EXIT
}

# docs/operations.md, "Upgrading Asterisk": stop, unpack, start the native unit, never leave Asterisk
# stopped. /etc/asterisk keeps the deployed configuration: the archive's samples are not unpacked.
upgrade_asterisk() {
  local archive=$1 asterisk_sum dongle_sum
  [[ -e "$ASTERISK_MARKER" ]] ||
    die "No $ASTERISK_MARKER: this Asterisk was not installed by bootstrap; upgrade it yourself"
  check_architecture "${archive##*/}"
  asterisk_sum=$(awk '$2 == "stage/usr/sbin/asterisk" {print $1}' "$STAGE/artifacts.sha256")
  dongle_sum=$(awk '$2 == "stage/usr/lib/asterisk/modules/chan_dongle.so" {print $1}' "$STAGE/artifacts.sha256")
  [[ "$asterisk_sum" =~ ^[0-9a-f]{64}$ && "$dongle_sum" =~ ^[0-9a-f]{64}$ ]] || die 'Malformed Asterisk checksums'
  # The FragmentPath proves the native unit.
  if asterisk_installed "$asterisk_sum" "$dongle_sum" &&
     [[ "$(systemctl show -p FragmentPath --value asterisk)" == "$ASTERISK_UNIT" ]] &&
     systemctl is-active --quiet asterisk; then
    echo 'This Asterisk build already runs under asterisk.service'
    return 0
  fi
  require_space_for_archive "$archive"
  require_no_untracked_asterisk
  stop_asterisk
  start_asterisk_on_failure
  unpack_asterisk "$archive" --exclude=./etc/asterisk
  start_native_asterisk
  trap - EXIT
  echo 'Asterisk upgraded; it runs under the native asterisk.service'
}

USAGE='Usage: bootstrap.sh prepare [ARCHIVE] | install STAGE ARCHIVE | upgrade STAGE ARCHIVE'
if [[ "${1:-}" == prepare ]]; then
  (( $# <= 2 )) || die "$USAGE"
  check_host prepare "${2:-}"
  exit 0
fi
[[ ( "${1:-}" == install || "${1:-}" == upgrade ) && "${2:-}" =~ ^/root/phonelet-install\.[a-zA-Z0-9]{8}$ && -n "${3:-}" ]] ||
  die "$USAGE"
STAGE=$2
if [[ "$1" == upgrade ]]; then
  upgrade_asterisk "$3"
  exit 0
fi
ARCHIVE=$3
check_host install "$ARCHIVE"
ASTERISK_SUM=$(awk '$2 == "stage/usr/sbin/asterisk" {print $1}' "$STAGE/artifacts.sha256")
DONGLE_SUM=$(awk '$2 == "stage/usr/lib/asterisk/modules/chan_dongle.so" {print $1}' "$STAGE/artifacts.sha256")
[[ "$ASTERISK_SUM" =~ ^[0-9a-f]{64}$ && "$DONGLE_SUM" =~ ^[0-9a-f]{64}$ ]] || die 'Malformed Asterisk checksums'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv lame libedit2 libjansson4 libsqlite3-0 libxml2 \
  libuuid1 libssl3t64 libncurses6 curl ca-certificates logrotate
if [[ "$PHONELET_BOOTSTRAP_WIFI" == 1 ]]; then
  apt-get install -y --no-install-recommends netplan.io wpasupplicant iw
fi
if declare -F target_install_packages >/dev/null; then target_install_packages; fi

if declare -F target_setup >/dev/null; then target_setup; fi

# Read the validated, rendered paths; dotenv quoting has already been resolved by deploy-release.py.
MODEM_DATA=$(sed -n 's/^data=//p' "$STAGE/etc-asterisk/dongle.conf")
MODEM_AUDIO=$(sed -n 's/^audio=//p' "$STAGE/etc-asterisk/dongle.conf")

# Stable links by USB interface number (AGENTS.md); no command is sent to the modem.
echo "Huawei udev USB ID: $PHONELET_BOOTSTRAP_MODEM_USB_ID; set PHONELET_BOOTSTRAP_MODEM_USB_ID=12d1:PID for another Huawei modem (verify interfaces 00/01/02 first)" >&2
cat > /etc/udev/rules.d/99-phonelet-modem.rules <<EOF
SUBSYSTEM=="tty", ATTRS{idVendor}=="${PHONELET_BOOTSTRAP_MODEM_USB_ID%:*}", ATTRS{idProduct}=="${PHONELET_BOOTSTRAP_MODEM_USB_ID#*:}", ENV{ID_USB_INTERFACE_NUM}=="00", SYMLINK+="dongle0-modem"
SUBSYSTEM=="tty", ATTRS{idVendor}=="${PHONELET_BOOTSTRAP_MODEM_USB_ID%:*}", ATTRS{idProduct}=="${PHONELET_BOOTSTRAP_MODEM_USB_ID#*:}", ENV{ID_USB_INTERFACE_NUM}=="01", SYMLINK+="dongle0-audio"
SUBSYSTEM=="tty", ATTRS{idVendor}=="${PHONELET_BOOTSTRAP_MODEM_USB_ID%:*}", ATTRS{idProduct}=="${PHONELET_BOOTSTRAP_MODEM_USB_ID#*:}", ENV{ID_USB_INTERFACE_NUM}=="02", SYMLINK+="dongle0-at"
EOF
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty
udevadm settle  # trigger only queues the events
for _ in $(seq 30); do
  if test -c "$MODEM_DATA" && test -c "$MODEM_AUDIO"; then break; fi
  sleep 1
done
if ! test -c "$MODEM_DATA" || ! test -c "$MODEM_AUDIO"; then
  die "modem not found after 30 s ($MODEM_DATA, $MODEM_AUDIO): power off, check the modem, its 5 V and the USB host port"
fi

if ! asterisk_installed "$ASTERISK_SUM" "$DONGLE_SUM"; then
  # A rerun over an interrupted bootstrap of another build: never unpack under a running daemon.
  require_no_untracked_asterisk
  if pidof asterisk >/dev/null; then
    stop_asterisk
    start_asterisk_on_failure
  fi
  unpack_asterisk "$ARCHIVE"
fi

# First install only (check_host stops on a deployed host); deploy owns these files later.
install -m 0640 -o root -g root "$STAGE"/etc-asterisk/* /etc/asterisk/

# A rerun keeps an Asterisk the unit already runs (start does nothing); deploy reloads its configuration.
start_native_asterisk
trap - EXIT
install -m 0644 "$CHECKOUT/asterisk/logrotate/phonelet-asterisk" /etc/logrotate.d/phonelet-asterisk

# Remove serial locks left on persistent /var/lock only at boot.
if ! cmp -s "$CHECKOUT/asterisk/systemd/phonelet-serial-locks.conf" /etc/tmpfiles.d/phonelet-serial-locks.conf; then
  install -D -m 0644 "$CHECKOUT/asterisk/systemd/phonelet-serial-locks.conf" /etc/tmpfiles.d/phonelet-serial-locks.conf
fi

bash "$CHECKOUT/scripts/provision-storage.sh"
# The Wi-Fi command: the copy of the previous netplan file (with its password) and the rollback marker.
install -d -m 0700 -o root -g root /var/lib/phonelet/wifi

# Placeholder service: preflight needs it loaded, and the first deploy's drop-in replaces ExecStart.
# No EnvironmentFile: dotenv and systemd parse it differently (docs/install.md#manual-installation).
if [[ ! -e /etc/systemd/system/phonelet.service ]]; then
  cat > /etc/systemd/system/phonelet.service <<'EOF'
[Unit]
Description=Phonelet GSM-to-SIP gateway and SMS forwarder
After=network-online.target asterisk.service
Wants=network-online.target asterisk.service

[Service]
Type=simple
User=root
# Placeholder until the first deploy: its 20-phonelet-release.conf drop-in replaces ExecStart.
ExecStart=/bin/sleep infinity
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload
systemctl enable --now phonelet
echo 'Host bootstrapped; deploying'
