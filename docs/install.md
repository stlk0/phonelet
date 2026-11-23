# Installing Phonelet

| Variant | Status | Guide |
| --- | --- | --- |
| Generic Debian 13 host (bare metal or VM) | `amd64` VM install without SIM passed on this version with Asterisk build 4; `arm64` calls and SMS were checked on hardware with an earlier revision and build 3, not yet re-run on this version | [On-device installation](#install-on-the-device) |
| Orange Pi Zero 2W | Reference target (`arm64`): this version runs on it with Asterisk build 4 (on-device upgrade, modem registers); calls and SMS were checked with an earlier revision and build 3, not yet re-run on this version | [Target guide](../targets/orangepi-zero2w/README.md) |

Phonelet turns a `chan_dongle`-compatible Huawei 3G USB modem into a single-SIM GSM-to-SIP
gateway and SMS forwarder. This guide covers installation on Debian 13 with `install.sh` (there is
no Debian package yet). See [operations](operations.md) for maintenance and
[architecture](architecture.md) for the data flow.

**Contents:** [Hardware and prerequisites](#hardware-and-prerequisites) ·
[Install on the device](#install-on-the-device) · [Prepare configuration](#prepare-configuration) ·
[Get the Asterisk archive](#get-the-asterisk-archive) ·
[Optional bootstrap settings](#optional-bootstrap-settings) ·
[Manual installation](#manual-installation) · [Firewall](#firewall) ·
[Configure the SIP phone and verify](#configure-the-sip-phone-and-verify) ·
[Configuration reference](#configuration-reference)

## Hardware and prerequisites

- Huawei voice-capable 3G USB modems compatible with `chan_dongle`; only the E1550 (firmware
  11.608.13.02.00) has been tested on hardware. Use a SIM with SMS and voice service on a compatible
  2G/3G network; another Huawei USB ID can be passed via `PHONELET_BOOTSTRAP_MODEM_USB_ID`
  (see [modem notes](e1550.md) for port mapping and hardware limits).
- A Debian 13 (trixie) `arm64` or `amd64` host with persistent storage, networking and a root
  terminal (local or SSH). Container installation is not supported.
- A SIP phone or app that supports calls and SIP MESSAGE for SMS.
- On the host: Bash, `tar`, GNU coreutils (including `realpath` and `sha256sum`), `awk`,
  standard Debian tools and `curl` for downloading the pinned Asterisk archive. If curl is
  missing, install it with `apt-get install curl` before starting. No compiler, Git checkout
  or development computer is needed for an installation from a source archive.
- Access to Debian packages and the locked Python dependencies. The installer creates its
  own venv and installs Python and venv support if needed.

Use a stable host address, such as a DHCP reservation. Keep `/var/log` on persistent storage.
Installation refuses tmpfs, ramfs and zram there. A separate persistent partition is supported
by the generic path. See the [target guides](../targets/README.md) for variant-specific requirements.

Disable the SIM PIN in a phone before inserting the SIM; the driver cannot enter it.
Connect the modem and SIM with the host powered off. Use a supply or powered USB hub that
can handle the modem's transmit current peaks. Remove ModemManager if installed: it takes
modem ports. Installation refuses to proceed if its unit is present and removes no packages.

Configure the [firewall](#firewall) before exposing SIP. Asterisk listens on UDP 5060 on all interfaces
unless `AST_SIP_BIND` names one address.
Allow only trusted SIP peers and the media traffic they need. The API and AMI bind to loopback
by default. The API uses only an API key over HTTP, without TLS; expose it only through a
protected reverse proxy with TLS and access controls. Read the [limitations](operations.md#limitations)
before installing.

## Install on the device

Get and unpack the source tree on the host into its own folder, not `/root/phonelet`, which holds
the configuration. Application release tags use `vX.Y.Z`; the first is `v0.1.0`.
Replace v0.1.0 with the release you install. Run installer examples as root from the unpacked source directory:

```sh
curl -fL -o phonelet.tar.gz https://github.com/stlk0/phonelet/archive/refs/tags/v0.1.0.tar.gz
rm -rf phonelet-src && mkdir phonelet-src && tar -xzf phonelet.tar.gz -C phonelet-src --strip-components=1
cd phonelet-src
bash install.sh
```

For a commit without a release tag, create a source archive in a developer checkout:
`git archive --format=tar.gz --output=phonelet.tar.gz COMMIT`, then copy it to the device.
Unpack a `git archive` copy without `--strip-components=1` (it has no top-level folder):
`rm -rf phonelet-src && mkdir phonelet-src && tar -xzf phonelet.tar.gz -C phonelet-src`.
A `git archive` copy records its commit hash. The `-dirty` suffix applies only when `install.sh`
runs in a Git checkout with uncommitted changes.

First the script verifies the Asterisk archive and checks the host. A board
[target profile](../targets/README.md) may exit with status 3 when a reboot is required first
(for example, after changing logging storage); if prompted, reboot and run the same command again.
Only after these checks does it install Python support,
stage a private release and prepare configuration. It then performs the host setup,
runs deployment preflight and deploys that same release.

At the end, use the engine's `Summary:` to configure the SIP phone, then
[verify SMS and calls](#configure-the-sip-phone-and-verify). The summary omits the password;
read it privately from `/root/phonelet/.env`. Software readiness does not prove radio or audio works.
Updates and reconfiguration use the same command from a newer tree; see
[Running and upgrading](operations.md#running-and-upgrading).

An existing Asterisk without Phonelet's `/var/lib/phonelet/asterisk-bootstrap` marker is refused.
A rerun after failure reuses the configuration and repeats idempotent setup steps. Repeat the same
`PHONELET_BOOTSTRAP_*` settings (without `PHONELET_BOOTSTRAP_TARGET` the target profile is skipped); once
`/root/phonelet/.env` exists, leave out the answer options (`install.sh` refuses them). Both installed
binary checksums must match the entries in `artifacts.sha256` before unpacking is skipped. Once the release drop-in exists,
`install.sh` performs an application update; it never upgrades Asterisk as a side effect.
Use [Upgrading Asterisk](operations.md#upgrading-asterisk) for that.

From another computer, copy the tree to the host and run `install.sh` over root SSH:

```sh
git archive --format=tar.gz --output=phonelet.tar.gz master
scp phonelet.tar.gz root@HOST:
ssh root@HOST 'rm -rf phonelet-src && mkdir phonelet-src && tar -xzf phonelet.tar.gz -C phonelet-src'
ssh -t root@HOST 'cd phonelet-src && bash install.sh'
```

A first installation can take a long time; over SSH run it inside `tmux` or `screen` on the host, so a
dropped connection does not stop it halfway.

Without `-t` there is no TTY, so the first installation needs the answers as options (leave out
`--trunk-prefix` if your country has no trunk prefix):
`ssh root@HOST 'cd phonelet-src && bash install.sh --country-prefix +44 --trunk-prefix 0 --sip-domain 192.0.2.10'`.
Pass a target and [optional settings](#optional-bootstrap-settings) before the command, for example
`ssh -t root@HOST 'cd phonelet-src && PHONELET_BOOTSTRAP_TARGET=orangepi-zero2w bash install.sh'`.
Updates use the same copy and the same command. Developers can also update with `deploy.sh HOST`
([Application updates](operations.md#application-updates)).

## Prepare configuration

If `/root/phonelet/.env` exists, the installer validates and uses it byte for byte. Otherwise
it creates the directory with mode 0700 and generates a new 0600 file: it asks for the country
calling code (for example `+44`), one domestic trunk-prefix digit (for example `0`; empty if
your country has none), then
confirmation of the proposed SIP address. Enter accepts the host's first IPv4 address; type
another address or DNS name to replace it. Use a VPN address, such as Tailscale, when the phone
connects over that VPN. No secret is printed. AMI, API and SIP secrets are generated automatically.
An existing file is never overwritten; an invalid new file is removed.

For unattended generation, supply the values; `--trunk-prefix` is optional and its absence means
no trunk prefix:

```sh
bash install.sh --country-prefix +44 --trunk-prefix 0 --sip-domain 192.0.2.10
```

These options are refused with an existing `.env`. Add optional settings from
[.env.example](../.env.example) directly to `/root/phonelet/.env`, then rerun `bash install.sh`.
Keep that file private. It uses dotenv syntax; do not source it. Values must be single-line;
`AST_*` values cannot contain `;` or `#`. Only `AST_*` template placeholders are substituted;
Asterisk variables such as `${EXTEN}` remain intact. Deployment rejects unknown `AST_*` names,
SIP passwords shorter than 12 characters and `PHONELET_AMI_PORT` other than 5038, and writes a
private canonical `runtime.env` for systemd from the validated application settings.

## Get the Asterisk archive

The default first installation downloads the pinned Asterisk and patched chan_dongle archive
for `dpkg --print-architecture`, verifying its SHA-256 against
[asterisk/artifacts.lock](../asterisk/artifacts.lock). The pinned build (`asterisk-20.22.0-4`)
runs on the arm64 reference board; calls and SMS were checked on hardware with build 3, whose
`chan_dongle.so` is byte-identical. A lock whose sums are all zeros (a build
not made yet) stops with `Asterisk build for ARCH not published yet: artifacts.lock has
placeholder sums`.

For an offline copy of the pinned archive, or an own build from [building](building.md):

```sh
bash install.sh --asterisk-archive /root/asterisk-VERSION-ARCH.tar.gz
# Own build, with exactly one archive and its artifacts.sha256:
bash install.sh --asterisk-build /root/build-output
```

The local archive must match the pin. An own build is checked against its `artifacts.sha256`,
which also supplies the binary sums used to skip an already installed build. Neither option downloads Asterisk; Debian
packages and Python dependencies still need to be available. On an installed host the same
options upgrade Asterisk ([Upgrading Asterisk](operations.md#upgrading-asterisk)).

To download the pinned archive on its own (for example before [Upgrading Asterisk](operations.md#upgrading-asterisk)), get a `BUILD_DIR`
without building:

```sh
bash scripts/fetch-asterisk.sh ARCH BUILD_DIR [ARCHIVE_FILE]
```

`ARCH` is `arm64` or `amd64`; `BUILD_DIR` must not exist. Omit `ARCHIVE_FILE` to download,
or pass a local pinned archive. Both paths check the lock and produce an archive plus
`artifacts.sha256`. A build that differs from the lock uses its own sums directly.

## Optional bootstrap settings

Set these in the environment of `install.sh` for first installation.
All optional features default to off. Switches accept `0` or `1`.

| Setting | Default | Effect |
| --- | --- | --- |
| `PHONELET_BOOTSTRAP_TARGET` | Empty | Select a [target profile](../targets/README.md). Empty runs only the generic steps. |
| `PHONELET_BOOTSTRAP_WIFI` | `0` | `1` installs `netplan.io`, `wpasupplicant` and `iw` for the [Wi-Fi command](sms-commands.md#wi-fi-command). |
| `PHONELET_BOOTSTRAP_MODEM_USB_ID` | `12d1:1001` | USB ID for all three udev rules. Another Huawei ID must be lowercase `12d1:PID`; verify its interface mapping first. Other models are not guaranteed to work. |

With an SMS command key and no Wi-Fi package selection, bootstrap warns and continues.
Install the Wi-Fi requirements manually or select the Wi-Fi switch.

## Manual installation

`install.sh` (backed by [scripts/bootstrap.sh](../scripts/bootstrap.sh)) is the
maintained installation path on Debian 13. For manual host setup, use its exact commands;
unpacking the archive overwrites an existing Asterisk installation. The essentials are:

- **Runtime packages:** `python3`, `python3-venv`, `lame`, `libedit2`, `libjansson4`,
  `libsqlite3-0`, `libxml2`, `libuuid1`, `libssl3t64`, `libncurses6`, `logrotate`, `curl`,
  `ca-certificates` (plus `netplan.io`, `wpasupplicant`, `iw` when Wi-Fi commands are enabled).
- **Modem udev rule:** `/etc/udev/rules.d/99-phonelet-modem.rules` mapping USB interfaces `00`/`01`/`02`
  to the unused modem (MDM) port `/dev/dongle0-modem`, the audio port `/dev/dongle0-audio`
  and the PCUI `data=` port `/dev/dongle0-at`,
  reloaded and triggered with `udevadm`; another interface layout needs script changes per
  [modem notes](e1550.md).
- **Asterisk + patched `chan_dongle`:** unpacked to `/` from the verified archive (skipping
  `./var/run` to preserve `/var/run -> /run`), then `ldconfig`. Bootstrap writes the archive name
  to `/var/lib/phonelet/asterisk-bootstrap` before unpacking; for a manual install, check the installed
  `/usr/sbin/asterisk` and `/usr/lib/asterisk/modules/chan_dongle.so` against their `stage/...` entries
  in `artifacts.sha256`, then write that marker. Without it, `--asterisk-archive`/`--asterisk-build` refuse upgrades.
  Start and enable the archive's `asterisk.service`; keep the default soft open-file limit of 1024 (`FD_SETSIZE`).
- **Storage, log rotation and boot lock cleanup:** `bash scripts/provision-storage.sh`,
  `/var/lib/phonelet/wifi` (0700), `/etc/logrotate.d/phonelet-asterisk`, and
  `/etc/tmpfiles.d/phonelet-serial-locks.conf` (`r! /var/lock/LCK..ttyUSB*`, boot-only — never run
  `systemd-tmpfiles --remove --boot` on a running gateway).
- **Configuration and services:** `/root/phonelet/.env` (0600, created via
  `scripts/deploy-release.py --init-env`), `/etc/asterisk` rendered with `scripts/deploy-release.py
  --render-configs FILE DIR` (it also writes the one-line `pjsip.conf`), and
  a placeholder `/etc/systemd/system/phonelet.service` (`ExecStart=/bin/sleep infinity`, no
  `EnvironmentFile`), then `systemctl daemon-reload` and `systemctl enable --now phonelet`.
  `install.sh` runs `scripts/deploy-release.py --stage RELEASE --check` and deploys locally from the staged release;
  its drop-in replaces the placeholder with the single-worker service.

## Firewall

The SIP phone must reach the host on the same local network or through a VPN of your choice,
for example [Tailscale](https://tailscale.com/kb/1031/install-linux) or
[WireGuard](https://www.wireguard.com/install/). Never expose SIP or RTP to the internet.

Asterisk takes SIP on UDP 5060 (the listening address is `AST_SIP_BIND`) and RTP media on UDP 10000–20000: the `rtp.conf` from
`make samples` sets `rtpstart=10000` and `rtpend=20000`. The nftables example below accepts SIP and RTP only from the trusted
prefixes and drops them from everywhere else. Everything else, SSH included, stays accepted.
The API binds `127.0.0.1:8080` by default and needs no rule; if you set `PHONELET_API_HOST`
to another address, protect that port too. Replace `192.0.2.0/24` and `2001:db8::/32` with your
LAN or VPN (for example tailnet) prefixes. To stop SIP listening elsewhere at all, also set
`AST_SIP_BIND` to the host's LAN address, but only to a static address present at boot: Asterisk
does not bind SIP to an address that appears later, and deployment refuses an address the host does
not have. With DHCP or Tailscale keep `0.0.0.0` and rely on these rules. RTP media still listens on
all interfaces. If you change the SIP port, change `5060` in the rules too:

```nft
table inet phonelet {
	set trusted4 {
		type ipv4_addr
		flags interval
		elements = { 192.0.2.0/24 }
	}
	set trusted6 {
		type ipv6_addr
		flags interval
		elements = { 2001:db8::/32 }
	}
	chain input {
		type filter hook input priority filter; policy accept;
		iif "lo" accept
		ct state established,related accept
		udp dport { 5060, 10000-20000 } ip saddr @trusted4 accept
		udp dport { 5060, 10000-20000 } ip6 saddr @trusted6 accept
		udp dport { 5060, 10000-20000 } drop
	}
}
```

On Debian, append the block to `/etc/nftables.conf` (it starts with `flush ruleset`, so reloading
replaces the rules), check it with `nft -c -f /etc/nftables.conf`, then load it now and at boot
with `systemctl enable --now nftables`.

## Configure the SIP phone and verify

Sipnetic (Android) is the tested SIP client. Set its server to `PHONELET_SIP_DOMAIN`, UDP port from `AST_SIP_BIND` (5060 by default).
Use `AST_SIP_USERNAME` (default `phone`) for authentication. Read the generated SIP password on the host:

```sh
grep AST_SIP_PASSWORD /root/phonelet/.env
```

Enter that password in the phone. The endpoint is named `phone` and accepts one contact;
a new registration replaces the previous contact. Set SIP MESSAGE recipients to the mobile number,
for example `+15555550123`. Inbound SMS delivery can be disabled independently of calls.

On the host, inspect the installation:

```sh
lsusb
ls -lL /dev/dongle0-at /dev/dongle0-audio
systemctl status phonelet asterisk
asterisk -rx 'dongle show devices'
asterisk -rx 'pjsip show endpoint phone'
ss -lntup
```

Confirm `12d1:1001`, character devices and persistent storage. Asterisk should listen only on
the `AST_SIP_BIND` address and port (`0.0.0.0:5060/udp` by default) and `127.0.0.1:5038`, plus two random UDP ports of the PJSIP DNS resolver.
Check [health and readiness](operations.md#health-and-readiness). Then verify an incoming call,
a long incoming SMS, an outgoing SIP SMS and the selected notifications with your own equipment.
Include recording only if enabled with the required consent. Software readiness alone does not
prove mobile network or audio behaviour.

## Configuration reference

This table covers every setting in [.env.example](../.env.example), including commented options.
Defaults are those used by the reference deployment when a setting is omitted. Example credentials
and `192.0.2.10` are placeholders, not usable defaults. “No” means the default can be kept;
“paired” means both channel settings must be set together or both left empty.

Fixed for deployment: `PHONELET_AMI_HOST=127.0.0.1` (`localhost` is also accepted),
`PHONELET_AMI_PORT=5038`, `PHONELET_AMI_USERNAME=phonelet`,
`PHONELET_SMS_INGRESS_DIR=/var/spool/asterisk/phonelet-sms` and
`PHONELET_CDR_LOG_PATH=/var/log/asterisk/cdr-custom/phonelet.csv`.

| Setting | Default | Required? | Meaning |
| --- | --- | --- | --- |
| `PHONELET_AMI_SECRET` | No usable default | Yes | Generated AMI secret, also used by Asterisk. |
| `PHONELET_DB_PATH` | `/var/lib/phonelet/phonelet.db` | No | SQLite database; custom paths need private permissions. |
| `PHONELET_RECORDINGS_DIR` | `/var/lib/phonelet/recordings` | No | Recording root, also used by Asterisk. Not under `/usr`, `/etc`, `/boot`, `/efi`, `/home`, `/root`, `/run/user`, `/tmp` or `/var/tmp`, also through a symlink: [asterisk.service](operations.md#asterisk-service) cannot write there. |
| `PHONELET_RECORDING_RETENTION_DAYS` | `0` | No | Delete recordings of calls older than this many days (at most 36500); `0` keeps them. See [space and backup](operations.md#space-and-backup). |
| `PHONELET_API_KEY` | No usable default | Yes | Generated secret for `X-API-Key` on protected HTTP endpoints. |
| `PHONELET_TELEGRAM_TOKEN` | Empty | No; paired | Telegram bot token; empty with chat ID disables the channel. |
| `PHONELET_TELEGRAM_CHAT_ID` | Empty | No; paired | Destination Telegram chat. |
| `PHONELET_HA_URL` | Empty | No; paired | Home Assistant base URL, using `http://` or `https://`. |
| `PHONELET_HA_TOKEN` | Empty | No; paired | Home Assistant bearer token; empty with URL disables the channel. |
| `PHONELET_SIP_DOMAIN` | None | Yes | Address or DNS name by which the SIP phone reaches this host; also the host part of SMS forwarded to the phone. Without deployment, unset falls back to the AMI host. |
| `PHONELET_SIP_MESSAGE_DESTINATION` | `pjsip:phone` | No | SIP SMS destination. Set explicitly empty to disable this channel; calls still work. |
| `PHONELET_MODEM_POLL_INTERVAL` | `30` | No | Modem status polling, seconds; positive integer. |
| `PHONELET_SMS_REASSEMBLY_TIMEOUT` | `600` | No | Positive seconds before an incomplete SMS is published with placeholders. Late parts update it and notify again. |
| `PHONELET_AMI_RECONNECT_TIMEOUT` | `5` | No | Positive AMI reconnect delay, seconds. |
| `PHONELET_SMS_COMMAND_KEY` | Empty | No | Empty disables commands. Otherwise a 32-byte key encoded as unpadded Base64url; see [SMS commands](sms-commands.md). |
| `PHONELET_WIFI_NETPLAN_FILE` | `/etc/netplan/30-wifis-dhcp.yaml` | No | File replaced by the Wi-Fi command. |
| `PHONELET_WIFI_INTERFACE` | `wlan0` | No | Wi-Fi interface managed by netplan and wpa_supplicant. |
| `PHONELET_WIFI_STATE_DIR` | `/var/lib/phonelet/wifi` | No | Private rollback state directory, mode 0700. |
| `PHONELET_WIFI_CONNECT_TIMEOUT` | `120` | No | Positive seconds to wait for Wi-Fi connection, also used during rollback. |
| `PHONELET_LOG_LEVEL` | `INFO` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `PHONELET_INGRESS_POLL_INTERVAL` | `5` | No | Positive SMS/CDR and command polling interval, seconds. |
| `PHONELET_MIN_FREE_BYTES` | `104857600` | No | Disk reserve (100 MiB), also used for recording admission. |
| `PHONELET_API_HOST` | `127.0.0.1` | No | HTTP bind IP. Use a firewall or protected proxy if exposing it. |
| `PHONELET_API_PORT` | `8080` | No | HTTP port; readiness checks use this setting. |
| `AST_SIP_USERNAME` | `phone` | No | SIP authentication username; endpoint name remains `phone`. |
| `AST_SIP_PASSWORD` | None | Yes | Generated SIP authentication password, at least 12 characters. |
| `AST_SIP_BIND` | `0.0.0.0:5060` | No | SIP UDP listen address: IPv4 address with an optional port (default 5060). Only a static address present at boot; with DHCP or Tailscale keep `0.0.0.0` and use the [firewall](#firewall). A change restarts Asterisk: deploy it with `--allow-asterisk-restart` ([Application updates](operations.md#application-updates)). |
| `AST_COUNTRY_PREFIX` | None | Yes | Country calling code with leading `+`, used for number conversion below. |
| `AST_TRUNK_PREFIX` | Empty | No | One domestic trunk-prefix digit, used only from six dialed digits. Empty if your country has none: no local conversion. A two-digit trunk prefix (such as `06`) is not supported. |
| `AST_RECORD_CALLS` | `no` | No | `yes` records incoming calls; outgoing calls are not recorded. Obtain any required consent first. |
| `AST_DONGLE_DATA` | `/dev/dongle0-at` | No | PCUI command/data port, USB interface 02. |
| `AST_DONGLE_AUDIO` | `/dev/dongle0-audio` | No | Audio port, USB interface 01. |
| `AST_DONGLE_RXGAIN` | `0` | No | GSM-to-SIP audio gain, integer -32767 to 32767. |
| `AST_DONGLE_TXGAIN` | `0` | No | SIP-to-GSM audio gain, integer -32767 to 32767. |

### Number prefixes, recording and gains

SMS recipients and dialed numbers follow the same rules. Examples use `AST_COUNTRY_PREFIX=+44`
and `AST_TRUNK_PREFIX=0`:

1. A number starting with `+` stays unchanged: `+441234567890` → `+441234567890`.
2. Six or more digits starting with `00`, the international access code, get `+` in place of `00`:
   `00441234567` → `+441234567`. This holds with any trunk prefix, `0` included. Dial numbers that
   use another access code (`011`, `0011`, `010`, `810`) with `+` instead.
3. Six or more digits starting with `AST_TRUNK_PREFIX` have that digit replaced by
   `AST_COUNTRY_PREFIX`: `07911123456` → `+447911123456`. With an empty `AST_TRUNK_PREFIX` this
   rule is off.
4. Any other number of up to five digits stays as dialed, including short and emergency numbers:
   `112` → `112`. From six digits it gets a leading `+`: `15551234567` → `+15551234567`.

`AST_COUNTRY_PREFIX` is required even if you normally dial international numbers. Set
`AST_TRUNK_PREFIX` to the digit dropped in international form; if your numbering plan has none,
leave it empty and dial domestic numbers in international form.

Warning: a number of six or more digits without `+` or `00` that does not start with the trunk
digit gets `+` in front, so it is dialed as an international number. With an empty
`AST_TRUNK_PREFIX` this holds for every such number, a domestic one included. In the North
American Numbering Plan a 10-digit number dialed without the leading `1` would go to another country: dial such
numbers with `+1` or `1`.

Call recording is off by default and covers only incoming calls; outgoing calls are never recorded.
Recording conversations can require consent under applicable law. Check the law and obtain any
required consent before setting `AST_RECORD_CALLS=yes`.
With recording off, calls remain in history with `recording_state=none`. Failed-SMS reporting
still works. See [recording recovery](operations.md#recording-recovery) for conversion and space checks.

Gain `0` is neutral. Values `-1`, `0` and `1` leave samples unchanged. Values above `1` multiply
audio samples and can clip; values below `-1` divide by their absolute value. Adjust each direction
only as needed for your equipment.

### Notification channels

Get a Telegram bot token from [@BotFather](https://core.telegram.org/bots/tutorial#obtain-your-bot-token).
After sending the bot a message, get the chat ID from `https://api.telegram.org/bot<token>/getUpdates`.
Get a Home Assistant long-lived access token on your
[user profile page](https://www.home-assistant.io/docs/authentication/#your-account-profile).

Telegram and Home Assistant receive SMS, missed and answered call events, and health alerts.
A phone that answers busy (486), temporarily unavailable or Do Not Disturb (480), or decline (600/603)
gives a `rejected` call: it stays in history and the API and never notifies any channel. A ring-out or an
unreachable phone gives a `missed` call with a notification.
Home Assistant publishes `sim_sms_received`, `sim_call_received` (missed and answered calls),
`sim_health_alert`, `sim_network_lost`, `sim_network_restored` and `sim_modem_unavailable` events and updates
`sensor.phonelet_modem`. SIP receives SMS only. Each channel has its own durable delivery queue;
one channel's failure does not discard another channel's jobs. Retries have capped backoff and
no attempt limit. Delivery can repeat if an external send succeeds before its acknowledgement is saved.

Disabling a channel leaves its existing jobs untouched. Pending jobs remain visible in
`/api/stats`; enabling it again resumes delivery. A newly enabled channel does not resend stored
SMS, calls or previously encountered alerts, including failed-SMS and quarantine records in retained journals.
