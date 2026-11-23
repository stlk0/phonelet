# Phonelet

Phonelet is a self-hosted, single-SIM GSM-to-SIP gateway and SMS forwarder for
Huawei voice-capable 3G USB modems compatible with `chan_dongle`; only the E1550 (firmware
11.608.13.02.00) has been tested on hardware. It is a software alternative to a single-port GoIP box for personal use.

Asterisk with a patched `chan_dongle` connects voice calls and outgoing SMS to your SIP phone.
A Python service persists incoming SMS and call history in SQLite and delivers notifications to
Telegram, Home Assistant and SIP through independent, durable queues.

```text
SIM / Huawei 3G USB modem (chan_dongle)
    |
Asterisk (patched chan_dongle) <------> SIP phone (voice + outgoing SMS)
    |
    | fsynced SMS journal + CDR / recordings
    v
Python service (SQLite history + outbox) ---> Telegram / Home Assistant / SIP MESSAGE
```

The first version targets Debian 13, configured through files and SSH (no web panel).
Asterisk is required for both SMS and voice. For SIP access outside your local network, pair the
host with a private VPN such as Tailscale or WireGuard.

## Hardware

- Huawei voice-capable 3G USB modems compatible with `chan_dongle`; only the E1550 (firmware
  11.608.13.02.00) has been tested on hardware. Use a working SIM; other Huawei USB IDs can be
  configured with `PHONELET_BOOTSTRAP_MODEM_USB_ID` — see [modem notes](docs/e1550.md).
- A Debian 13 host (`arm64` or `amd64`) with USB and a power supply that handles transmit peaks.
  The reference host is an [Orange Pi Zero 2W](targets/orangepi-zero2w/README.md).
- A SIP phone or softphone supporting voice calls and SIP MESSAGE.

The mobile network must still provide compatible 2G/3G service; these modems do not support LTE
or VoLTE.

## Status

Phonelet is early software with one hardware-verified reference setup: an Orange Pi Zero 2W
(`arm64`) with a Huawei E1550 (`12d1:1001`). The pinned Asterisk build 4 (`asterisk-20.22.0-4`)
runs on that board; calls and SMS were last checked there with build 3, whose `chan_dongle.so` is
byte-identical.

- **Verified on reference hardware (this version, Asterisk build 4):** on-device `install.sh`
  upgrade of the application and of Asterisk, service start and modem registration.
- **Verified on reference hardware (an earlier revision with Asterisk build 3, not yet re-run on this version):**
  multipart SMS reception, notifications to all three channels,
  outgoing SMS from the SIP phone, incoming and outgoing calls, rejected calls (no missed-call
  alert), and missed calls when the phone is unreachable.
- **Verified on reference hardware (earlier Phonelet versions, not re-run on the current version):** call
  recording and the encrypted Wi-Fi SMS command.
- **Verified in VM (this version, Asterisk build 4) and automated tests:** fresh install, repeated
  runs, an interrupted install and backup verification on a clean Debian 13 `amd64` VM with a modem
  attached (no SIM), plus software tests using temporary databases, fake
  transports and a synthetic PTY serial bench (journal replay, multipart assembly, retries and
  failure paths).
- **Not hardware-verified:** `amd64` with a live SIM/network, modem models other than E1550, and
  call routing or audio quality across arbitrary carriers and SIP clients.

## Features

- **Durable SMS reception.** The patched driver writes and fsyncs each fragment to a journal before
  deleting it from the SIM or modem memory. The service replays the journal after a restart and
  assembles multipart messages. Reception recovery does not depend on AMI events.
- **Notifications to Telegram, Home Assistant and the SIP phone.** Each channel has its own durable
  delivery queue and retries. Channels are plugins, with an explicit set of events they accept.
- **Calls to and from the SIP phone** through Asterisk and the modem, with incoming call history in SQLite.
- **Optional call recording**, with WAV capture and MP3 publishing.
- **Commands by encrypted SMS**, including remote Wi-Fi configuration when enabled.

The journal protects messages that have reached it. Network non-delivery, a full modem inbox or
failed storage can still prevent reception; this is not an end-to-end guarantee from the carrier.

## Quick start

On a Debian 13 host, unpack the Phonelet source archive into `phonelet-src`, enter that source
directory and run as root:

```sh
bash install.sh
# For later updates, unpack the newer source tree the same way and run the same command.
```

The installer fetches and verifies the pinned Asterisk archive, checks the host, asks for
dialing prefixes (country code and optional domestic trunk prefix) and the SIP address, generates
secrets, and deploys Phonelet. It reuses an existing `/root/phonelet/.env`. Configure your SIP
phone from the deployment summary, then test SMS and a call.

See [Installation](docs/install.md) for getting the source tree, prerequisites, target profiles,
other Huawei USB IDs, local-archive/own-build options and post-install checks. From another
computer, copy the source tree to the host and run `install.sh` over root SSH; developers can
update with `deploy.sh HOST`.

## Documentation

- [Install targets](targets/README.md) — install variants and optional profiles.
- [Installation](docs/install.md) — first setup, optional settings and configuration reference.
- [Operations](docs/operations.md) — configuration, updates, backups and recovery.
- [Encrypted SMS commands](docs/sms-commands.md) — setup, format and supported commands.
- [Architecture](docs/architecture.md) — journals, assembly, delivery queues, API and plugins.
- [Building and testing](docs/building.md) — pinned sources, containers and verification levels.
- [Modem notes (Huawei E1550 reference)](docs/e1550.md) — ports, other `chan_dongle` modems, SMS storage, power and known hazards.
- [chan_dongle patches](chan_dongle_patch/README.md) — purpose, order and driver constraints.
- [Contributor and agent notes](AGENTS.md) — code map, rules and checks.

## Limitations

- One modem, one SIP endpoint and one service worker.
- Asterisk and Phonelet run as root, the only supported mode; SIP listens on UDP on all interfaces by default (`AST_SIP_BIND`), so restrict access to trusted clients.
- Outgoing SMS can only be sent from the SIP phone; there is no REST endpoint for sending SMS and outgoing SMS are not stored.
- SIP delivery success means Asterisk accepted the request, not that the physical phone acknowledged it.
- Registration loss has no proven permanent fix; modem or carrier failures can interrupt reception and calls.

See [Operations: limitations](docs/operations.md#limitations) and the [modem notes](docs/e1550.md).
For vulnerability reporting and scope, see [Security policy](SECURITY.md).

## License

Original Phonelet code, scripts, configuration and documentation use the [MIT License](LICENSE).
The chan_dongle patches and the chan_dongle/Asterisk source snapshots in `tests/fixtures/` use
[GPL-2.0](COPYING.GPL-2.0), with the preserved BSD exception for `memmem.c`.
See [NOTICE](NOTICE) for the exact scope, source versions and third-party attribution.

## Thanks

- [wdoekes/asterisk-chan-dongle](https://github.com/wdoekes/asterisk-chan-dongle) and its authors
  for the modem driver.
- [Sheepsticked/aster](https://github.com/Sheepsticked/aster) for the unexpected-error and
  command-deadline patches incorporated into this project's patch set.
- [Asterisk](https://www.asterisk.org/) and its contributors for the voice and SIP foundation.
