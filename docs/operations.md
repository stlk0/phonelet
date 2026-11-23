# Operating Phonelet

Use [installation and configuration](install.md) for a new host, [SMS commands](sms-commands.md)
for remote Wi-Fi changes, [architecture](architecture.md) for the data flow and
[modem notes](e1550.md) for modem details. Commands below run as root on the Debian host
unless marked as development-computer commands; installer examples run from the source directory.
Use a maintenance window for service interruptions.

**Contents:** [Limitations](#limitations) · [Running and upgrading](#running-and-upgrading) ·
[Asterisk service](#asterisk-service) · [Modem power](#modem-power) ·
[Space and backup](#space-and-backup) · [Removing Phonelet](#removing-phonelet) ·
[Health and readiness](#health-and-readiness) · [Troubleshooting](#troubleshooting)

## Limitations

- Asterisk and Phonelet run as root; this is the only supported mode.
  Root allows netplan and `wpa_cli` Wi-Fi changes and access to private Asterisk SMS/CDR journals;
  deployment also writes root-owned `/etc/asterisk` configuration.
  The release drop-in sandboxes Phonelet: `/usr`, `/boot`, `/efi`, `/etc` (except `/etc/netplan`), `/root`, `/home`
  and `/run/user` are read-only to it, so deployment refuses a database, recordings or Wi-Fi path there.
- SIP listens on UDP 5060 on all interfaces unless `AST_SIP_BIND` names one address. Restrict access with a
  firewall to trusted peers.
- One modem is supported.
- Outgoing SMS can only be sent from the SIP phone; there is no REST endpoint for sending SMS and outgoing SMS are not stored.
- Storage grows without automatic cleanup: SMS spool, CDR journals, outbox rows and database; recordings
  only unless [retention](#space-and-backup) is set. Monitor space and keep private backups off the host.
- The patched chan_dongle serves Phonelet only. It has no dialplan `sms` branch or AMI new-SMS events.
- Writing an incoming SMS during a call can cause an audible click. The driver fsyncs the journal
  while holding the device lock.

## Running and upgrading

The application runs as a single worker under `phonelet.service`. Asterisk runs under
`asterisk.service`. The modem power unit is optional and specific to compatible hardware.

```sh
systemctl status phonelet asterisk
journalctl -u phonelet -n 100 --no-pager
```

### Application updates

On the device, unpack the newer source tree (see [installation](install.md)), enter it and run as root:

```sh
bash install.sh --check
bash install.sh
```

To reconfigure, edit `/root/phonelet/.env` and run the same commands. They stage a fresh virtual
environment under `/root/phonelet-releases`, validate `.env` (never overwrite it with
`.env.example`), and deploy the new release. Application updates do not upgrade Asterisk (use
[Upgrading Asterisk](#upgrading-asterisk)), and `PHONELET_BOOTSTRAP_*` variables are ignored after
first installation.

Developers can also deploy from a checkout over root SSH with `bash deploy.sh HOST --check` and
`bash deploy.sh HOST`.

What happens during a deploy:

- **`--check`** runs preflight and shows a masked configuration diff without changing running
  services or files (it stages a temporary release and removes it on exit).
- **Preflight** checks configuration, active Asterisk and patched `chan_dongle`
  (`SMSJournalVersion: 1`, `SMSJournalStatus: ok`), modem character devices, free space, live
  SQLite schema compatibility, and **refuses while a call is active**.
- **Switch and smoke check** stops Phonelet, snapshots SQLite, updates `/etc/asterisk` (reloading
  Asterisk unless a restart is required), starts the candidate release, and verifies `/ready`
  (`software_ready: true`, waiting up to 30 minutes if a new database is doing its first journal
  pass) and AMI/SIP state. Any failure rolls back automatically.
- **Retention:** a successful deploy keeps the current and previous releases (private: they hold
  secrets and DB snapshots) and cleans up older `release.*` directories after an hour. After
  verification it prints a deployment summary without printing secrets.

If `--check` reports a SIP transport change, use `--allow-asterisk-restart`; if `cdr_csv` has an
active CDR, use `--restart-asterisk`. Run with zero active calls and no queued outgoing SMS:

```sh
bash install.sh --allow-asterisk-restart
# For cdr_csv with an active CDR, or an unconditional restart:
bash install.sh --restart-asterisk
```

The same flags work with `deploy.sh HOST`. A restart can flush the outgoing SMS queue; do not
treat SIP `202 Accepted` as proof of delivery. After an update, verify readiness, notifications,
an incoming call, an incoming SMS and an outgoing SIP SMS on your hardware.

### Rollback and interrupted deployment

A failed deploy restores the previous application unit, Asterisk configs and recording converter,
leaving `.env`, Asterisk binaries and `chan_dongle.so` untouched. It keeps the current database
when its schema is compatible (saving the candidate database as `rollback/failed-release.db`) so
accepted history is never overwritten with an older snapshot. Notifications may still repeat if an
external send succeeded before its acknowledgement was committed.

If automatic rollback cannot establish a consistent state, it stops Phonelet with
`ROLLBACK INCOMPLETE`. Preserve the current database and snapshots and install a compatible release
rather than copying an old database over new history.

For an interrupted deploy (for example, `SIGKILL` or power loss), inspect the services and the
release's `rollback/manifest.json`:

```sh
systemctl status phonelet asterisk
systemctl cat phonelet
journalctl -u phonelet -u asterisk -n 100 --no-pager
```

Stop Phonelet; restore the unit drop-in, configs and recording helper from the manifest's saved files (remove targets
marked absent), run `systemctl daemon-reload`, reload/restart Asterisk, start Phonelet and check `/ready`. This is not an atomic transaction.

### Returning to an earlier release

Select the earlier revision on the development computer (or unpack its release archive into a fresh
`phonelet-src` on the device) and run `--check` before deploying:

```sh
git checkout <rev>
bash deploy.sh HOST --check
bash deploy.sh HOST
# Or on the device, inside that release's source directory:
bash install.sh --check
bash install.sh
```

Find the active release directory with `systemctl show phonelet -p WorkingDirectory`; its
`REVISION` file records the deployed Git revision (a `-dirty` suffix means
uncommitted local changes). Deployment uses the current host `/root/phonelet/.env` and installed
Asterisk/`chan_dongle`; confirm compatibility before rolling back.

Because there are no schema migrations, an earlier release with a different
`Database.SCHEMA_VERSION` refuses startup and rolls back. Check the live database schema read-only:

```sh
python3 - <<'PY'
import sqlite3
with sqlite3.connect('file:/var/lib/phonelet/phonelet.db?mode=ro', uri=True) as db:
    print(db.execute('PRAGMA user_version').fetchone()[0])
PY
```

If switching to an earlier schema is intentional, back up first and follow
[Recreating the database](#recreating-the-database). Never overwrite newer history with an old
snapshot. This procedure has not been run on hardware.

### Recreating the database

Phonelet uses a single SQLite schema (`Database.SCHEMA_VERSION`) with no migrations. A missing
database is recreated automatically and rebuilt from retained SMS journals and CDR; the first
replay records an inventory so historical messages are stored silently without re-sending
notifications or commands (see
[SMS assembly and the first inventory](architecture.md#sms-assembly-and-the-first-inventory)).
Old outbox state and delivery acknowledgements are not reconstructed.

To recreate the database deliberately:

1. Run `bash install.sh --check` (or `bash deploy.sh HOST --check`) with the target
   release and resolve everything except an expected schema refusal. Check `/api/stats` and let
   pending deliveries finish where possible.
2. Note the maintenance start time, stop both services, and make a complete off-host
   [backup](#space-and-backup).
3. Move the database and its `-wal`/`-shm` files into a new private directory (preserving all SMS
   and CDR journals), then start only Asterisk:

   ```sh
   saved_db=$(mktemp -d /var/lib/phonelet/saved-db.XXXXXXXX)
   for file in /var/lib/phonelet/phonelet.db /var/lib/phonelet/phonelet.db-wal /var/lib/phonelet/phonelet.db-shm; do
     if [ -e "$file" ]; then mv -- "$file" "$saved_db/"; fi
   done
   systemctl start asterisk
   ```

4. Re-run `--check` (now that the old database is moved aside), then run
   `bash install.sh` (or `bash deploy.sh HOST`). If smoke times out on a very large
   replay, inspect logs and re-run the deploy; the new database and inventory remain in place.
   If rollback leaves Phonelet stopped, keep the new database and deploy a compatible release, or stop both
   services and restore the saved DB, WAL and SHM together. Command SMS in the initial inventory did not run.
5. Compare `/api/stats` counts with the saved database, verify `metadata.ingress_complete` is `1`,
   and review any SMS/calls that arrived during the maintenance gap (they are stored silently):

   ```sh
   python3 - <<'PY'
   import sqlite3
   with sqlite3.connect('file:/var/lib/phonelet/phonelet.db?mode=ro', uri=True) as db:
       print(db.execute("SELECT value FROM metadata WHERE key='ingress_complete'").fetchone())
   PY
   ```

Do not move, compress or truncate SMS/CDR journals until `ingress_complete` reaches `('1',)`.

### Changing the AMI password

Preflight logs in to running Asterisk with the secret from `.env`, so update the running AMI
account first:

1. Save private copies of `/etc/asterisk/manager_custom.conf` and `/root/phonelet/.env` with `cp -p`.
2. Update `secret=` for `[phonelet]` in `/etc/asterisk/manager_custom.conf` and run
   `asterisk -rx 'manager reload'`.
3. Update `PHONELET_AMI_SECRET` in `/root/phonelet/.env` (no `;`, `#` or line breaks).
4. Run `bash install.sh --check` then `bash install.sh` (or
   `deploy.sh HOST`). On failure, restore both saved files and run `manager reload` again.

### Upgrading Asterisk

`install.sh --asterisk-build DIR` or `--asterisk-archive FILE` on an installed host replaces the
bootstrapped Asterisk and patched `chan_dongle` together, then deploys Phonelet from the same
source tree. Run from that source directory on the device as root, inside `tmux` or `screen` when over SSH:

1. In a maintenance window with zero active calls and no queued outgoing SMS, close interactive
   `asterisk -r` consoles and take a [backup](#space-and-backup) (plus `/etc/asterisk`,
   `/root/phonelet/.env`, service units and the currently installed Asterisk archive for rollback).
2. Download the pinned build into a directory that does not exist (use a new path on a rerun),
   or copy a local build from [building](building.md):

   ```sh
   bash scripts/fetch-asterisk.sh "$(dpkg --print-architecture)" /root/asterisk-build
   ```

3. Run the upgrade (`--allow-asterisk-restart` and `--restart-asterisk` apply; `--check` does not):

   ```sh
   bash install.sh --asterisk-build /root/asterisk-build
   # Or directly from a pinned archive file:
   bash install.sh --asterisk-archive /root/asterisk-20.22.0-ARCH.tar.gz
   ```

   Upgrades require the `/var/lib/phonelet/asterisk-bootstrap` marker written during installation
   and refuse hosts without it. The script checks the archive against `artifacts.sha256`, stops Asterisk
   (waiting up to 60 s), unpacks while keeping `/etc/asterisk`, starts `asterisk.service` and deploys Phonelet.
   It refuses active calls or Asterisk outside `asterisk.service`. A failure after the stop starts
   `asterisk.service` again and exits before the Phonelet deploy.
4. Verify `systemctl is-enabled asterisk`, `asterisk -rx 'core show application DongleSendSMSBase64'`,
   `asterisk -rx 'dongle show devices'` (`SMSJournalVersion: 1`, `SMSJournalStatus: ok`),
   `/ready`, `/api/modem`, and a test call + SMS. `UMask=0077` applies to new files only: after an upgrade
   from an earlier build, run `find /var/log/asterisk -maxdepth 1 -type f -exec chmod 0600 {} +`
   (`messages.log`, its rotated copies and `queue_log`).
5. To roll back to an earlier Asterisk build, run step 3 from the earlier revision's source tree
   with its archive (save the newer `/usr/share/phonelet/asterisk.files` first if you also want to
   remove files added only by the newer build).

**Replacing `chan_dongle.so` alone (own driver build):** build the module against the exact
installed Asterisk headers ([building](building.md)), back up `/usr/lib/asterisk/modules/chan_dongle.so`,
stop Asterisk and wait up to 60 s for `pidof asterisk` to become empty; replace nothing if it remains.
Always start Asterisk on exit; if installation or startup fails, restore the saved module and start it again.
Run step 4's checks, including `DataSetting: /dev/dongle0-at` in the AMI `DongleShowDevices` response.

## Asterisk service

The Asterisk archive installs the native unit
`/usr/lib/systemd/system/asterisk.service`: the upstream `contrib/systemd/asterisk.service` of the
pinned Asterisk, unchanged, with Phonelet's differences in the drop-in
[10-phonelet.conf](../asterisk/systemd/asterisk.service.d/10-phonelet.conf) next to it. Asterisk
runs in the foreground as root (`asterisk -mqf`) and systemd tracks the process itself, without a
PID file. Any exit restarts it after four seconds (upstream `Restart=always`), at most five starts in
ten minutes; a stop that hangs is killed after 45 seconds. It waits for `network-online.target`, so
an `AST_SIP_BIND` address exists at start, and for `time-set.target`, so a board without a real-time
clock does not reuse call UniqueIDs from before a reboot. A stop
or restart kills only the Asterisk process (`KillMode=process`), not a recording conversion still
running; an encoder that hangs is not killed by a stop either (find it with
`pgrep -f convert-recording.py`). Asterisk writes no core files, creates its files readable by root
only (`UMask=0077`), and its early errors on stderr go to the journal. The unit makes `/usr`, `/etc`, `/boot`, `/efi`, `/home`, `/root`
and `/run/user` read-only for Asterisk and the programs it runs and gives it a private `/tmp` and
`/var/tmp`, so deployment refuses a recordings directory in any of them (also through a symlink).

Use `systemctl restart asterisk` for restarts; `systemctl stop asterisk` keeps it stopped. After five
starts within ten minutes, fix the cause, then run `systemctl reset-failed asterisk` and
`systemctl start asterisk`; clear the counter at the end of maintenance too. Put local changes in a
drop-in under `/etc/systemd/system/asterisk.service.d/`, never in the files under `/usr/lib`: an
Asterisk upgrade replaces them. Check that systemd tracks the running daemon:

```sh
systemctl show -p FragmentPath --value asterisk   # /usr/lib/systemd/system/asterisk.service
systemctl show -p MainPID --value asterisk
pidof asterisk                                    # the same single PID
```

## Modem power

Most hosts use a powered USB connection and need no power control. See the [modem notes](e1550.md)
for power requirements. If an [install target](../targets/README.md) switches modem power, its guide
describes the circuit, the service and manual takeover (for example
[Orange Pi Zero 2W](../targets/orangepi-zero2w/README.md#modem-power)).

## Space and backup

Monitor all data filesystems above `PHONELET_MIN_FREE_BYTES` (100 MiB by default).
Phonelet checks the filesystems of the recordings, database and SMS spool directories every few
seconds and raises one health alert when one drops below the reserve, and one "Disk space restored"
alert once all are again above the reserve plus 10 %; the announced state survives restarts.
New recordings are skipped below that reserve while calls continue. While low space is announced and
`AST_RECORD_CALLS=yes`, an answered call without a recording is notified with
"(not recorded: low disk space)". `/ready` does not depend on free space.

`PHONELET_RECORDING_RETENTION_DAYS` (0, the default, keeps everything) deletes the MP3, its status
sidecar and any leftover WAV of calls received more than that many days ago, checked
hourly; the call stays in history with `recording_state=deleted`. Only regular files beneath the
recordings directory are deleted: a recording outside it, behind a symlink, in a missing directory
or with a file newer than the limit is kept and logged once. This setting never deletes SMS journals or CDR. Conversion needs the reserve
plus space up to the WAV input size. An ongoing recording can still consume the reserve: this is
an admission check, not a quota. Use a separate recording filesystem or quota for a hard limit.
A full SMS spool causes journal failure and retains SMS in the modem's finite storage.

The Asterisk message log has scheduled rotation: bootstrap installs
[phonelet-asterisk](../asterisk/logrotate/phonelet-asterisk) as `/etc/logrotate.d/phonelet-asterisk`.
It rotates Asterisk's `messages.log` monthly or above 10 MB, keeps twelve files and reopens the log
with `logger reload`. CDR and SMS journals are recovery sources; log rotation is not their retention policy.

For a consistent backup, stop both services in a maintenance window. Run the helper from a checkout
or installed release. Use a new destination on storage with enough free space, outside the input trees:

```sh
systemctl stop phonelet asterisk
python3 scripts/backup.py backup --services-stopped \
  --destination /root/phonelet-backup-NEW \
  --db /var/lib/phonelet/phonelet.db \
  --recordings /var/lib/phonelet/recordings \
  --ingress /var/spool/asterisk/phonelet-sms \
  --cdr /var/log/asterisk/cdr-custom \
  --env /root/phonelet/.env
systemctl start asterisk phonelet
```

Substitute configured paths. Restart both services even if backup fails and check readiness.
The helper uses SQLite's backup API including WAL, verifies integrity and free space, rejects
symlinks and hashes every saved file. It publishes the private backup directory atomically.
Backups contain credentials and message/call history. Copy them off the host securely. Keep the
application Git revision, matching build artifacts, `/etc/asterisk`, units and drop-ins separately;
`backup.py` does not save AstDB or Wi-Fi state. A database needs a release of the same schema.

### Recovery from backup

Verify the backup in place first:

```sh
python3 scripts/backup.py verify /root/phonelet-backup-NEW
```

The helper checks hashes and SQLite integrity without copying files or requiring extra free space.
With both services stopped, preserve the current data first, then manually copy the backed-up DB,
spool, retained CDR, recordings and environment at their configured paths. Preserve any current
WAL/SHM before moving them aside; do not attach stale WAL/SHM to the restored database.
Restore private ownership and permissions, then start the matching application and driver release.
Check `/ready`, counts, pending outbox jobs and a ready recording if recording is enabled.

Journal replay deduplicates known IDs. Restoring an older database can repeat notifications that
were delivered after the backup. It also forgets commands accepted after that backup. Recent
commands outside its historical inventory can run again; commands older than 48 hours by the
host's clock are rejected as stale.

To prevent command replay, clear `PHONELET_SMS_COMMAND_KEY` before the first start and wait for
all retained journals to be consumed. The service reads the `runtime.env` of its release, which a
deploy writes from `/root/phonelet/.env`: editing that file alone changes nothing. Set
`PHONELET_SMS_COMMAND_KEY=` (empty) in `/root/phonelet/.env`, start Asterisk (deploy preflight requires
it) and start the service with `deploy.sh HOST`, or on the device with `bash install.sh`
from the source tree of the restored database's release.
New `PC1` texts then become ordinary SMS; their ciphertext can appear in history and ordinary
notifications. Already queued commands are rejected as disabled. After replay, put the key back in
`/root/phonelet/.env` and run `deploy.sh HOST` (or `bash install.sh`) again.
Already ingested SMS are not reclassified as commands.
An empty database instead uses the [first replay inventory](#recreating-the-database).
Keep journals until a verified restore proves the history you need is recoverable.

### Recording recovery

The converter publishes MP3 atomically, then `<file>.mp3.status.json`. A `ready` sidecar and the
MP3 must both exist. No sidecar means pending; a failed conversion preserves the WAV for retry.
Inspect space, ownership and the converter's error before retrying on the host:

```sh
python3 /usr/local/lib/phonelet/convert-recording.py /var/lib/phonelet/recordings/FILE.wav --root /var/lib/phonelet/recordings
```

Replace `FILE.wav` with the affected retained recording. Never use a partial encoder output as
the published MP3. The authenticated recording endpoint serves only ready files under the configured root.

## Removing Phonelet

This procedure has not been run on hardware. Adjust paths if you changed defaults in `.env`, and
keep any files or packages shared with other software.

1. **Stop services and back up.** With zero active calls and no queued outgoing SMS, stop and disable
   both services and wait for `pidof asterisk` and `pgrep -f convert-recording.py` to exit:

   ```sh
   systemctl stop phonelet asterisk
   systemctl disable phonelet asterisk
   ```

   Take a private off-host [backup](#space-and-backup) (`backup.py backup --services-stopped`),
   plus `/root/phonelet/.env` and `/etc/asterisk`. If a Wi-Fi SMS change is in progress, finish or
   recover it before deleting `/var/lib/phonelet/wifi`.
2. **Remove Phonelet files and units:**
   - `/etc/systemd/system/phonelet.service` and `/etc/systemd/system/phonelet.service.d/`
   - `/etc/tmpfiles.d/phonelet-serial-locks.conf`, `/etc/logrotate.d/phonelet-asterisk`,
     `/usr/local/lib/phonelet/convert-recording.py`
   - `/etc/udev/rules.d/99-phonelet-modem.rules` (then `udevadm control --reload-rules`)
   - `/root/phonelet` and interrupted `/root/phonelet-install.*`
     directories (private secrets); keep `/root/phonelet-releases` until the data decision in step 4
3. **Remove the Asterisk archive files.** Remove `/var/lib/phonelet/asterisk-bootstrap`, review
   `/usr/share/phonelet/asterisk.files` and copy it to a temporary file first (it lists itself).
   Exclude shared entries, remove listed files one by one without recursion, then leftover Phonelet
   `/etc/asterisk/*_custom.conf` and empty Asterisk/Phonelet directories; never `rmdir` `/`, `/usr`, `/etc`, `/var` or `/run`.
   Finish with `ldconfig` and `systemctl daemon-reload`.
4. **Decide separately whether to erase data (irreversible):** `/var/lib/phonelet` (SQLite DB,
   recordings, Wi-Fi state), `/var/spool/asterisk/phonelet-sms`, `/var/log/asterisk`,
   `/var/lib/asterisk` (including `astdb.sqlite3`), `/var/cache/asterisk`, `/run/asterisk`, and any
   `/root/phonelet-backup-*` directories and `/root/phonelet-releases` (DB snapshots).
5. **Undo optional settings if enabled:**
   - [Orange Pi Zero 2W](../targets/orangepi-zero2w/README.md): stop/disable and remove
     `phonelet-modem-power.service`, remove `/etc/systemd/system.conf.d/90-phonelet-watchdog.conf`
     (`systemctl daemon-reexec && systemctl daemon-reload`), review `/etc/default/armbian-ramlog` and the profile's `gpiod` package.
   - Wi-Fi command: review `/etc/netplan/30-wifis-dhcp.yaml` (contains Wi-Fi credentials; keep if
     it is your active connection) and optional packages (`netplan.io`, `wpasupplicant`, `iw`).
     Runtime packages are listed in [Manual installation](install.md#manual-installation).

## Health and readiness

The HTTP API binds to `127.0.0.1:8080` by default because it has only API-key authentication over
HTTP, without TLS. Expose it only through a protected reverse proxy with TLS and access controls.
`/health` is unauthenticated liveness only:

```sh
curl -sS http://127.0.0.1:8080/health
```

It returns `{"status":"ok"}` when the HTTP application responds. It does not prove reception.
For `/ready` and every `/api/` route, send the configured API key in `X-API-Key`.
Use your private HTTP client configuration or replace the placeholder locally:

```sh
curl -sS -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8080/ready
curl -sS -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8080/api/stats
curl -sS -H 'X-API-Key: YOUR_API_KEY' http://127.0.0.1:8080/api/modem
```

`/ready` returns 200 only when software and modem are ready; otherwise 503. Missing or wrong
credentials return 401. `software_ready` requires authenticated AMI, healthy driver journaling,
available SMS/CDR journals, fresh ingress polling and zero ingress errors. `ingress_first_pass` is
true until the first journal pass of a new database is complete; `ingress_records` counts the SMS
journal files and CDR lines this process has consumed. `modem_ready` describes
modem reception availability, including network registration. Deployment accepts 200 or 503 with
`software_ready: true`; monitor full readiness separately.
`device_not_connected` means the driver lists the device but its ports are not connected
(`State: Not connected` or `Stopped`); `/ready` returns 503.

`/api/stats` includes SMS/call totals, delivery queue counts, ingress state and recording disk space.
`/api/modem` separates current availability from the last good sample; old signal values do not
prove the modem is available now. `/api/sms` and `/api/calls` expose stored history. A ready recording
is available at `/api/calls/{call_id}/recording`. Keep this private data and the key off public networks.

## Troubleshooting

| Symptom | Check and action |
| --- | --- |
| `/health` responds but `/ready` is 503 | Inspect `software_ready`, `modem_ready`, `driver_ingress` and ingress errors. Then check AMI, journal permissions and modem registration. |
| AMI authentication fails | Confirm the running account and both matching secrets. Follow [password rotation](#changing-the-ami-password). AMI listens on loopback. |
| No modem in Asterisk | Check USB presence, power and PCUI/audio character devices. Ensure ModemManager is absent. Do not send arbitrary AT commands or delete modem messages to diagnose it. |
| Modem port is "already used by process N" after reboot | A persistent `/var/lock` can retain `LCK..ttyUSB*` files whose PIDs have been reused. Bootstrap installs `/etc/tmpfiles.d/phonelet-serial-locks.conf` (`r! /var/lock/LCK..ttyUSB*`) for boot-only cleanup; see [manual installation](install.md#manual-installation). For a current occurrence, confirm with `ps -p N` that the process is not using the modem, then reboot in a maintenance window. Do not remove live locks or invoke boot-only cleanup on a running gateway. |
| `/ready` stays 503 after a power cut, with `CDR journal ... blocked at byte offset` in the log | The power cut tore the last CDR line. Once the next call appends its row, the torn line and that next row, now one line, are set aside together and an alert names the file (that call may be missing from history); `/ready` recovers then. Do not edit the CDR journal. |
| Calls work but no SMS history | Check `SMSJournalStatus`, spool space and read errors. AMI is not the SMS recovery journal. |
| SMS arrive partly | Wait for the reassembly timeout. Missing parts become placeholders; late parts update the same SMS. See [modem notes](e1550.md) for modem limits. |
| Notifications stay pending | Check channel credentials and reachability. Disabled channels retain pending jobs. Retry backoff has no attempt limit; a permanently failing ordered alert can block later alerts for that channel. |
| Outgoing SMS accepted by SIP but not received | `202 Accepted` is not a delivery receipt. Check Asterisk reports and health alerts. Success leaves no CDR or outgoing history; `r` is pre-queue rejection, `i` a send error, `t` an expired unknown result, `e` a negative delivery report. A failure may mean partial sending. |
| Asterisk is active but has no daemon | Compare MainPID and `pidof`, and the unit's FragmentPath ([Asterisk service](#asterisk-service)). Fix the cause before resetting the start limit. |
| Recording pending or failed | Check disk reserve, converter, WAV and sidecar together. See [recording recovery](#recording-recovery). |
| Wi-Fi command fails | Check netplan, systemd, wpa_supplicant and private rollback state. Follow [Wi-Fi recovery](sms-commands.md#manual-wi-fi-rollback). |

An unreadable SMS file holds that file and later SMS; listing or stat errors hold unconsumed SMS.
CDR ingestion continues. A blocker lasting 15 minutes raises a health alert, at most once per file
or directory per UTC day. The timer restarts with Phonelet. Fix permissions, storage or the file.
If it cannot be repaired, preserve it outside the spool; its missing part can affect multipart assembly.
Do not delete it as routine cleanup.

An undecodable SMS retained as a private `U1` journal record raises a health alert identifying
its journal file. Keep that file for recovery. Records that can never be stored are quarantined:
one alert per record and channel, counted in `ingress_quarantined`. The file remains in place and
later records continue. Quarantine does not count as an ingress error or hold readiness.
Repeated driver journal failures do hold software readiness; three consecutive monitor polls with
journal status `error` also raise a health alert.

Inspect `/var/log/asterisk/messages.log` locally when needed. Raw driver traces and journals can
contain phone numbers, SIM identities and message contents. Do not publish them; use synthetic
values and redact secrets when asking for help.
