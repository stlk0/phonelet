# Install targets

A target is an install variant: a board, a VM or another host setup. Container installation is not supported. See the
[status table](../docs/install.md#installing-phonelet) for supported variants and their guides.
The generic Debian 13 bootstrap is the base for every variant.

Each `targets/<name>/` contains a required `README.md` with its status, hardware,
steps outside the generic bootstrap and checks. An optional `profile.sh` defines only
functions; files its hooks install live beside it.

Set `PHONELET_BOOTSTRAP_TARGET=<name>` to select a profile. Names must match
`^[a-z0-9][a-z0-9-]*$`; the folder is resolved in the checkout, independently of the
caller's directory. Only folders with `profile.sh` are selectable. Empty runs the
generic steps only. For a target without a profile, leave the setting empty and follow
its README. Run `PHONELET_BOOTSTRAP_TARGET=<name> bash install.sh` as root on the device from the source directory.
The bootstrap sources the selected profile once in each first-install phase, directly from the checkout.
Prepare runs before configuration or package changes. Upgrades never load a profile.

## Hook interface

All hooks are optional. The core calls only functions the profile defines.

| Hook | When it runs | Phase | What it may do |
| --- | --- | --- | --- |
| `target_check_storage PHASE` | During host checks, before the generic persistent `/var/log` check | `prepare`, then `install` | Check target storage; in prepare, make an idempotent storage setting change and exit 3 if a reboot is required |
| `target_install_packages` | After generic runtime packages and optional Wi-Fi packages | `install` | Install target packages |
| `target_setup` | Before udev discovery and Asterisk | `install` | Set up target hardware and services; install files from `TARGET_DIR` |

Hooks run as root on the destination host with `set -euo pipefail`, umask 022 and
`die MESSAGE` (stderr, exit 1). `TARGET_DIR` is the exported absolute path to the selected
folder in the checkout, available in both prepare and install; hooks can read its supporting files.
Exit 3 in prepare means "reboot and rerun the same command".

Hooks must be idempotent: a rerun repeats every step. Follow [AGENTS.md](../AGENTS.md),
including no AT commands and no secrets in output. Keep stdout free during prepare:
the core uses it to return Asterisk's installation status; send diagnostics to stderr.

## Add a target

1. Add its folder and README, plus a profile and supporting files if needed.
2. Add a row to the [installation status table](../docs/install.md#installing-phonelet).
3. Add tests for its hooks with fake commands and temporary paths.
4. Run the [AGENTS.md checks](../AGENTS.md#checks).

Nothing in `scripts/` changes when a target is added.
