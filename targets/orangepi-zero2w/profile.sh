#!/bin/bash
# Orange Pi Zero 2W on Armbian, with the owner's PH4 modem power switch.
# Loaded only by an explicit PHONELET_BOOTSTRAP_TARGET=orangepi-zero2w.

target_check_storage() {
  local RAMLOG_REBOOT=3
  # The CDR journal must survive a power loss: no /var/log in zram (armbian-ramlog).
  if [[ -n "$(findmnt -n /var/log || true)" ]]; then
    [[ "${1:-}" == prepare && -f /etc/default/armbian-ramlog ]] || die '/var/log is a separate mount'
    if grep -qx 'ENABLED=false' /etc/default/armbian-ramlog; then
      die 'armbian-ramlog is already off, but /var/log is still a separate mount (for example a partition of' \
        'its own), which this target does not support. If the board was not rebooted since ramlog was disabled,' \
        'reboot it first'
    fi
    sed -i 's/^ENABLED=.*/ENABLED=false/' /etc/default/armbian-ramlog
    grep -qx 'ENABLED=false' /etc/default/armbian-ramlog || die 'Could not disable armbian-ramlog'
    echo 'armbian-ramlog disabled: reboot the board and run again' >&2
    exit "$RAMLOG_REBOOT"
  fi
}

target_install_packages() {
  apt-get install -y --no-install-recommends gpiod
}

target_setup() {
  local WATCHDOG_CONF=/etc/systemd/system.conf.d/90-phonelet-watchdog.conf
  local MODEM_POWER_UNIT=/etc/systemd/system/phonelet-modem-power.service
  local watchdog
  # A hung board reboots by itself: nobody may be near it to pull the plug.
  watchdog=$'[Manager]\n# phonelet: reboot the board if it hangs (sunxi-wdt max 16 s)\nRuntimeWatchdogSec=15s\nRebootWatchdogSec=2min'
  test -c /dev/watchdog0 || die 'no hardware watchdog /dev/watchdog0'
  if [[ ! -f "$WATCHDOG_CONF" || "$(cat "$WATCHDOG_CONF")" != "$watchdog" ]]; then
    install -d -m 0755 "${WATCHDOG_CONF%/*}"
    printf '%s\n' "$watchdog" > "$WATCHDOG_CONF"
  fi
  # system.conf is read only at (re)exec; a rerun after a failed reexec still applies it.
  if [[ "$(systemctl show -p RuntimeWatchdogUSec --value)" != 15s ]]; then
    systemctl daemon-reexec
    [[ "$(systemctl show -p RuntimeWatchdogUSec --value)" == 15s ]] || die 'systemd did not start the watchdog'
  fi

  # Power must be on before looking for the modem on USB. Do not restart a running holder.
  if ! cmp -s "$TARGET_DIR/phonelet-modem-power.service" "$MODEM_POWER_UNIT"; then
    install -D -m 0644 "$TARGET_DIR/phonelet-modem-power.service" "$MODEM_POWER_UNIT"
    systemctl daemon-reload
  fi
  systemctl enable phonelet-modem-power
  # Type=simple returns before gpioset requests the line: allow an immediate failure to surface.
  if ! { systemctl start phonelet-modem-power && sleep 1 && systemctl is-active --quiet phonelet-modem-power; }; then
    die 'phonelet-modem-power failed (GPIO may be busy: Device or resource busy); check journalctl -u phonelet-modem-power' \
      'and targets/orangepi-zero2w/README.md#modem-power before retrying'
  fi
}
