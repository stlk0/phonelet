# Phonelet: notes for contributors and coding agents

Phonelet is a self-hosted, single-SIM GSM-to-SIP gateway and SMS forwarder for Huawei voice-capable 3G USB modems
compatible with `chan_dongle`; only the E1550 (firmware 11.608.13.02.00) has been tested on hardware.
Asterisk with a patched `chan_dongle` drives the modem and a SIP
phone; a Python service stores incoming SMS and calls, recovers them from the driver's journals after a restart,
and delivers notifications through a durable per-channel queue.

Read first: [README.md](README.md); installation and configuration: [docs/install.md](docs/install.md); operations:
[docs/operations.md](docs/operations.md).
Architecture: [docs/architecture.md](docs/architecture.md); building and testing: [docs/building.md](docs/building.md).

## Rules

- Do the change at hand: no drive-by refactoring, no new dependencies or documents unless the change needs them.
- Tests use temporary databases, a fake transport and synthetic traces only. Never point code or tests at a real
  modem, Asterisk, Telegram, Home Assistant or SIP phone, and never send real SMS, calls or notifications.
- No AT commands, modem resets, USB or power actions, and no deletion of SMS, journals, CDR, recordings or
  databases, unless the change is exactly about that.
- Raw traces and logs with IMSI, IMEI, phone numbers or SMS texts never go into Git. Use synthetic values
  (`+1555…`, `192.0.2.x`).
- SMS command keys, ciphertext and secret arguments (the Wi-Fi password) never reach logs or notifications.
- An SMS counts as received only after the driver has written and fsynced it, before it is deleted from the SIM.
  AMI is not a recovery journal.
- Outgoing SMS text reaches the driver as Base64 and is decoded after Asterisk has parsed the arguments. Never
  substitute `${MESSAGE(body)}` directly into `DongleSendSMS`.
- One database schema (`Database.SCHEMA_VERSION`), no migrations: a database of another schema stops the start.
- Asterisk templates substitute only `AST_*` variables; `${EXTEN}`, `${CALLERID}` and other dialplan variables
  stay for Asterisk.
- The six chan_dongle patches in `chan_dongle_patch/` apply in order. When you change one, check that
  `scripts/prepare-sources.sh NEW_DIR` applies them without offsets, run the driver tests (`-m driver`) and a full
  build with `scripts/build-asterisk.sh`.
- The service runs as a single worker.
- Install variants live in `targets/<name>/` (a README, an optional `profile.sh` with bootstrap hooks, their
  files). `scripts/`, `src/` and the generic docs name no variant beyond a short link to its README.

## Code map

- `main.py`: application, resources, managed startup and shutdown.
- `ami_client.py`: AMI login and reconnect; AMI only answers requests (`Events: off`).
- `ingress.py`: re-reads the immutable SMS fragments and CDR written by the driver and Asterisk.
- `db.py`: SQLite schema, source identifiers, SMS assembly, history and the transactional outbox.
- `notification.py`: the only path that claims and sends delivery jobs.
- `notifiers/`: notification channels (a class registry, settings, the events each accepts).
- `modem_status.py`: the modem monitor; current availability and the last good snapshot are kept apart.
- `cdr_events.py`: CDR contracts of the driver.
- `recording.py`, `scripts/convert-recording.py`: call recordings (pending, ready, failed, deleted), safe MP3 publishing
  and the optional retention.
- `disk_space.py`: the low disk space alert and its restore.
- `sms_commands/`: commands by encrypted SMS (`PC1` format in `envelope.py`, the loop in `worker.py`, one module
  per command in `commands/`; how to add one is in the `Command` docstring).
- `netplan_wifi.py`: replaces the Wi-Fi netplan file with rollback; external programs run through an injected runner.
- `api.py`: response models, protected history and readiness (`/health` liveness, `/ready` reception readiness).
- `logging_privacy.py`: keeps secrets and message contents out of the log.
- `asterisk/` and `chan_dongle_patch/`: pinned versions, configuration templates and the driver patches.
- `targets/`: install variants, optional bootstrap profiles and their supporting files.
- `install.sh`: on-device first installation, application updates and reconfiguration.
- `deploy.sh`, `scripts/`: configuration generation, release deploy with preflight and rollback, the host steps of
  first installation (`bootstrap.sh`).

## Checks

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
for s in deploy.sh install.sh scripts/*.sh targets/*/profile.sh; do bash -n "$s"; done
shellcheck -S warning deploy.sh install.sh scripts/*.sh targets/*/profile.sh
.venv/bin/ruff check .
```

The tests need `cc`, `patch` and, for the SMS command page test, `node`. Report what you ran and its result, and
what you did not verify. Passing tests do not prove modem, voice or network behaviour.

Maintainers keep internal process notes in `.dev/`, which is not part of this repository.
