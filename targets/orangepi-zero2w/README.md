# Orange Pi Zero 2W

Status: Reference target (arm64). This version runs on it with Asterisk build 4 (on-device upgrade, modem registers); calls and SMS were tested on hardware with an earlier revision and build 3.

Follow the [generic installation guide](../../docs/install.md) for archives, configuration and
bootstrap prerequisites, and [installation checks](../../docs/install.md#configure-the-sip-phone-and-verify)
after setup. The [target interface](../README.md) describes how this profile runs.

## Bootstrap

This profile targets the Orange Pi Zero 2W (Allwinner H618) running Armbian Debian 13 minimal.
It requires `/var/log` on the root filesystem.
Select it only with compatible watchdog and modem power hardware. It:

1. Disables Armbian ramlog before anything is staged if `/var/log` is mounted separately, then exits
   with status 3. Reboot and rerun the same `install.sh` command. A remaining separate mount is refused.
2. Installs `gpiod` (libgpiod 2).
3. Requires `/dev/watchdog0` and enables the sunxi hardware watchdog with
   `RuntimeWatchdogSec=15s` and `RebootWatchdogSec=2min`. It reexecutes systemd to apply them.
4. Installs and starts `phonelet-modem-power` before USB discovery and Asterisk. It holds PH4
   (`gpiochip1` line 228, physical pin 18) high. A busy GPIO stops setup without killing its holder.
   See [modem power](#modem-power) for the required circuit and manual takeover.

Connect data to the USB-C host port. The OTG port is in peripheral mode with the mainline
device tree. Provide adequate modem power through a powered hub or a correctly wired external
power switch. Selecting the profile does not add a power circuit. A watchdog cannot fix power faults.

On the device as root, from the unpacked source tree, select the target and Wi-Fi packages:

```sh
PHONELET_BOOTSTRAP_TARGET=orangepi-zero2w \
PHONELET_BOOTSTRAP_WIFI=1 \
bash install.sh
```

Bootstrap fetches the pinned arm64 archive; for an offline copy see
[Get the Asterisk archive](../../docs/install.md#get-the-asterisk-archive). For a source build, follow
[building](../../docs/building.md) and pass `--asterisk-build BUILD_DIR` to `install.sh`.

After installation, `findmnt /var/log` should print nothing for this profile.
Check `systemctl show -p RuntimeWatchdogUSec` for `15s` and confirm
`phonelet-modem-power` is active.

## Modem power

Most hosts use a powered USB connection and need no GPIO unit. The optional profile
assumes a high-enabled power switch: PH4 drives a BC547C and AO3401 P-MOSFET controlling the
modem's 5 V supply. PH4 is physical pin 18, `gpiochip1` line 228. Do not connect a modem's power
input directly to a GPIO. See [modem notes](../../docs/e1550.md) for power requirements.

[phonelet-modem-power.service](phonelet-modem-power.service) uses libgpiod 2's
foreground `gpioset` to hold logical 1. It restarts after two seconds on exit and starts before
Asterisk. This orders processes; USB discovery can still take time. To change a compatible circuit's
chip or line, use `systemctl edit phonelet-modem-power`:

```ini
[Service]
Environment=MODEM_POWER_CHIP=gpiochip1
Environment=MODEM_POWER_LINE=228
```

Apply with `systemctl daemon-reload` and `systemctl restart phonelet-modem-power` in a maintenance
window. Releasing the GPIO does not guarantee a low output or removal of power. Use your circuit's
safe shutdown procedure; do not use stopping this unit as a modem power-cycle command.

If `phonelet-modem-power failed` reports `Device or resource busy`, another process (for example
a manual `gpioset`) holds the line. Stop that holder, then rerun the same command with the same
`PHONELET_BOOTSTRAP_*` settings from the source directory: without `PHONELET_BOOTSTRAP_TARGET=orangepi-zero2w`
the profile and the modem power setup are skipped.
