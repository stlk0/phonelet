# Architecture

Phonelet is a self-hosted, single-SIM GSM-to-SIP gateway and SMS forwarder. Asterisk
handles voice calls and outgoing SMS between a `chan_dongle`-compatible Huawei 3G USB
modem (reference hardware: Huawei E1550) and a SIP phone. A separate Python service
records incoming SMS and call history in SQLite and delivers notifications. Both
processes run on the gateway host; the SIP phone, Telegram and Home Assistant are
clients or external services. Installation and day-to-day procedures are in
[install.md](install.md) and [operations.md](operations.md).

```text
Huawei 3G USB modem (chan_dongle)
    |
patched chan_dongle (loaded into Asterisk) <--> Asterisk <--> SIP phone
    |                                            |
    | SMS fragment files                         | CDR CSV, call recordings
    +-----------------------+--------------------+
                            v
                  Python journal replay
                            |
                  SQLite history + outbox
                            |
             per-channel delivery workers
                 /          |          \
            Telegram   Home Assistant   SIP MESSAGE via Asterisk

Python -- AMI requests --> Asterisk (status polling and SIP MESSAGE)
Python -- HTTP API --> authenticated history and status clients
```

The service uses one application process and one worker. Its resources are created
in the application lifespan, rather than at ASGI import: database, channel clients,
AMI connection and background loops. Shutdown stops producers and delivery workers
before closing AMI, clients and SQLite. An unexpected background task failure ends
the process so the service manager can restart it. The entry point is
[main.py](../src/phonelet/main.py).

## Reception and replay

The durable SMS boundary is in the driver. Each fragment must be written to a new
journal file, with both file contents and directory entry fsynced, before the driver
may delete it from the SIM or modem memory. A write failure leaves the SMS there for
retry. The six driver patches, serial port arrangement, journal format and recovery
limits are described in [the patch README](../chan_dongle_patch/README.md).

[IngressReplay](../src/phonelet/ingress.py) polls the immutable SMS journal and the
Asterisk CDR journal, including uncompressed rotations. It does not delete sources.
After a restart it reads retained journals again; database source identifiers prevent
the same input from creating duplicate history or jobs. SMS receipt time comes from
the file mtime, while the message timestamp comes from the SMS service-centre timestamp
(SCTS). Preserve contents and mtimes when restoring journals.

AMI is a request channel, not a reception or recovery journal. The client logs in
with `Events: off`; `DongleShowDevices` still returns its requested device list, and
`MessageSend` sends SIP messages. SMS and call ingestion never depends on unsolicited
AMI events. Transport connection and successful authentication are tracked separately.

An unreadable SMS file holds later files so an unknown earlier fragment cannot be
assigned to a newer message generation. Files in the initial inventory are processed
first, then arrivals are ordered by mtime and filename. A failed fragment transaction
defers its assembly group; independent groups can still publish. Persistently blocked
SMS replay produces a durable health alert after 900 seconds of observed blockage,
at most once per blocker per UTC day. Invalid records are retained and quarantined
with a deduplicated health alert; their contents are never included in the alert.
An invalid SCTS alone uses the journal file's mtime in UTC instead, with a warning
containing only the filename.

CDR records identify incoming calls by Asterisk UniqueID on a `Dongle/` channel. A call that
reached `Dial` has a `Dial` row and then a `Hangup` row; the first row of a UniqueID wins, so
the `Dial` row's disposition counts. A call that never entered `Dial` (no reachable phone
contact, or the caller hung up before it) has only the `Hangup` row and is stored as missed. The CSV stores call start time separately from the
service's receipt time with an explicit UTC offset. A timestamp without an offset is read as UTC.
Replay follows file identity and byte offsets during a run;
an unfinished CSV append waits for completion, and a storage failure retries the same
record. A malformed record is quarantined once a complete line shows it is broken; a
record whose quoted field reaches the end of the written data waits as an unfinished
append. Unlike the SMS producer,
`cdr_custom` does not promise fsync on every record: replay cannot recover a call
record that never reached durable storage.

Tagged CDR records also carry outgoing SMS failures to the notification queue.
Outgoing text enters the driver through `DongleSendSMSBase64`, decoded only after
Asterisk parses application arguments. There is no REST send endpoint or outgoing
SMS history. Sending and failure-report limits belong to [operations.md](operations.md).

## SMS assembly and the first inventory

[Database](../src/phonelet/db.py) owns multipart assembly. A fragment's source ID is
the SHA-256 of the complete journal record, including its slot and raw PDU. Identical
replay is deduplicated; the same text in a different source record may be a new SMS.
The journal has no memory-name field, so byte-identical SM and ME records in the same
slot are indistinguishable, as is reuse of that slot with an identical PDU.

Parts are grouped by device, SIM identity, sender, concatenation reference and total
part count. Candidates must be incomplete and within a 24-hour receipt-time window.
SCTS is a tie-breaker between candidates, not part of the group key: retransmitted
parts can have different timestamps. A single free candidate is joined. Between several,
only a unique equal usable SCTS of the first accepted part decides; otherwise all candidates
are marked ambiguous and nothing is joined. A multipart fragment with a substituted timestamp
(invalid SCTS) joins like any other, but neither it nor an assembly started by one takes part
in that tie-break. Recognition by timestamp equal to receipt time survives a restart without
extra stored state; a genuine SCTS equal to receipt time is treated the same way. The SMS time
is the first accepted part's timestamp. If ownership cannot be decided, assemblies are marked
ambiguous rather than silently merged. A reused reference can still fool a single-candidate
match; SMS does not supply a globally unique message ID.

A complete assembly publishes immediately. After `sms_reassembly_timeout` (600 seconds
by default), an incomplete one publishes with `[missing part i/n]` placeholders.
Ambiguous assemblies have a visible prefix and `partial=true`. A later accepted part
updates the same history row and queues a separate notification with the full current
text. Existing notifications are not withdrawn or edited at the provider.

On a database's first replay, the service persists an inventory of existing SMS
filenames and CDR file identities and byte boundaries before consuming them. These
records rebuild history without notifications or command execution, regardless of
wall-clock dates. The inventory survives interruption and completes only after its
sources have been consumed. New files and appended CDR bytes beyond the saved boundary
remain live arrivals while rebuilding continues. If the SMS directory is unavailable,
its inventory is captured on the first successful listing.

An assembly containing an inventory fragment remains historical for subsequent
publications, including late parts. Suppressed historical alerts are recorded in
`alert_events` so restart cannot turn them into new alerts. A replay blockage alert itself is
live. This inventory policy applies to a new database, not to every service restart.

## Transactions and notification delivery

SQLite uses WAL, `synchronous=FULL` and explicit transactions. Fragment acceptance,
assembly changes, history publication and notification jobs commit together. There is
one supported schema, identified by `Database.SCHEMA_VERSION`; a different schema
stops startup without migration.

Missed and answered calls create notification jobs. A phone that answers busy (486), temporarily
unavailable or Do Not Disturb (480), or decline (600/603) gives a `rejected` call: it remains in
history and the API, but creates no jobs for any channel; this is not configurable. A ring-out or an
unreachable phone gives a `missed` call with a notification.

The transactional outbox stores a separate job for each accepting channel. Its unique
`(event_key, backend)` pair prevents duplicate enqueue of the same event to that
channel. A job has attempts, next retry time, a lease and a random owner. Claiming is
atomic; completion and checkpoint updates check ownership, and checkpoints also require
a live lease. An expired job can be reclaimed without letting its former owner update
the new worker's result.

For alerts with stable event keys, `alert_events` records the first encounter in the
same transaction as their jobs, including historical alerts and alerts no channel
accepts. Only channels accepting the alert at that first encounter receive jobs;
replay never queues a previously encountered alert for a newly enabled channel.
Modem monitor alerts use fresh random keys and do not use this registry.

[notification.py](../src/phonelet/notification.py) is the production send path. One
delivery loop per channel keeps a slow provider from delaying other channels. Failed
attempts retry with exponential backoff capped at one hour, without an attempt limit.
Lost, restored, unavailable and health alerts keep insertion order within a channel;
a persistently failing alert holds later alerts there. SMS and call jobs are independent
of that alert order. Disabled channels' existing jobs remain stored.

Telegram and SIP split long SMS notifications and durably checkpoint `_next_chunk`
after each acknowledged chunk, before sending the next. Normal retry resumes at the
first unacknowledged chunk. A provider may accept a chunk just before a crash or lost
response prevents the local checkpoint: delivery is at-least-once, with a possible
duplicate in this gap. An acknowledgment proves only the channel's acceptance contract:
Telegram requires HTTP success and `ok=true`, Home Assistant requires HTTP 2xx, and SIP
requires AMI success from Asterisk. These do not prove a person read the message or an
automation ran.

## Channels as plugins

[notifiers/registry.py](../src/phonelet/notifiers/registry.py) lists channel classes.
Each `from_settings(config, *, ami)` builds a configured instance or returns `None`
to disable it. The shared [Notifier protocol](../src/phonelet/notifiers/protocol.py)
declares lifecycle and delivery hooks; runtime wiring does not branch on concrete
channel classes.

| Channel ID | Accepted events | Optional current-status receiver |
|---|---|---|
| `telegram` | All `NotificationMethod` events | No |
| `homeassistant` | All `NotificationMethod` events | `push_modem_sensor(status)` |
| `sip` | Incoming SMS only | No |

To add a channel:

1. Implement the protocol with a unique, stable `backend_id`, `from_settings`,
   `start`, `close` and `deliver(method, payload, checkpoint)`. Declare
   `accepted_methods` using [NotificationMethod](../src/phonelet/constants.py).
   `BaseNotifier` supplies HTTP lifecycle and ordinary method dispatch if useful.
2. Add configuration fields and list credential field names in `secret_settings`,
   so logging redacts them before constructing clients. Document configuration in
   the relevant setup instructions.
3. Add the class to `registry.CHANNELS`. Implement `push_modem_sensor(status)` only
   if it also receives current modem snapshots; the runtime detects the
   `ModemStatusReceiver` protocol automatically.
4. Test every accepted event with a fake transport, including failure/retry and
   checkpoint recovery for chunked delivery. Return success only after acknowledgment;
   propagate checkpoint failures rather than sending subsequent chunks.

## Modem monitoring

[ModemStatusMonitor](../src/phonelet/modem_status.py) polls the configured device
through AMI, every 30 seconds by default. `availability` is `unknown`, `available`
or `unavailable`. Available means a fresh successful poll of a connected device, not
network registration. Readiness additionally requires confirmed registration. Unknown registration remains
null; an unknown RSSI has no derived dBm value.

Disconnection, failed authentication, a missing device, poll failure/timeout or a stale
sample clears current network fields. `last_sample` retains the last good snapshot
with its original timestamp, explicitly separate from current availability. Status
reads check transport/authentication between polls; a sample older than
`max(2 * poll_interval, poll_interval + 15 seconds)` becomes stale.
`State: Not connected` or `Stopped` means `unavailable` with reason
`device_not_connected`, even though the device is present.

Lost, restored and unavailable transitions enter the same outbox as other events.
The announced `(availability, network_registered)` state commits with its alert and
is loaded on restart. This avoids repeating a committed lost alert and can report
restoration after downtime. A newly created database does not emit a spurious restored
alert for an already registered modem. Enqueue failure keeps a transition pending for
retry, but a crash before enqueue can lose intermediate transitions: this is not a
complete durable journal of every modem state change.

Three answered polls with a failing driver journal status produce a health alert;
a later `ok` rearms it. Optional status receivers get each current snapshot separately
from the outbox. Home Assistant uses this for `sensor.phonelet_modem`; failure retries
at the next poll without invalidating the sample or blocking durable alerts.

## Recordings

Asterisk records calls to WAV and invokes
[convert-recording.py](../scripts/convert-recording.py). The converter atomically
publishes a nonempty MP3 followed by a `<mp3>.status.json` result marker. Failure
retains the WAV. [recording.py](../src/phonelet/recording.py) reconciles `none`,
`pending`, `ready` and `failed` states. Only a `ready` marker and a nonempty MP3
inside the configured recording directory make a recording ready. Missing/pending
results time out after an hour; a late valid result can restore readiness.

A CDR path or a call notification does not promise a saved recording. Download checks
the filesystem contract again, even if the database still says ready.

## HTTP API

The [API](../src/phonelet/api.py) provides these read-only routes. Except for
`/health`, all require `X-API-Key`; interactive API documentation is disabled.

| GET route | Purpose |
|---|---|
| `/health` | HTTP process liveness; always independent of reception readiness |
| `/ready` | Reception readiness and its software/modem diagnostics |
| `/api/modem` | Current availability, registration and separate last good sample |
| `/api/sms` | Incoming SMS history; pagination, sender and time filters |
| `/api/calls` | Incoming call history with recording state and pagination |
| `/api/calls/{call_id}/recording` | Download a verified ready MP3 |
| `/api/stats` | History counts, per-channel queue, ingress status and disk space |

`/ready` returns 200 only when both `software_ready` and `modem_ready` are true;
otherwise it returns 503. Software readiness requires authenticated AMI, driver
`SMSJournalVersion=1` and `SMSJournalStatus=ok`, available SMS/CDR paths and a fresh,
error-free replay pass. Quarantined records are counted separately and do not make
readiness fail. Path existence and the driver's status are not a disk write/fsync
probe. History remains accessible when AMI or the modem is unavailable.

## Privacy and storage limits

[logging_privacy.py](../src/phonelet/logging_privacy.py) installs the process logging
policy before clients are created. It suppresses protocol-library wire data, redacts
registered secrets and retains safe exception types and code locations instead of
exception text, locals or raw tracebacks. Access logs strip query strings and fragments;
URL paths remain visible. Application logs must use safe identifiers and counters,
never phone numbers, SIM identities, SMS bodies, ciphertext or secret command arguments.
This is not a general-purpose detector of private data in arbitrary strings.

The Python policy does not filter separate Asterisk logs or manual diagnostic output.
SMS journals, SQLite/WAL, CDR and recordings intentionally contain private data and
need restricted permissions and coordinated backups. They must not enter Git. Apart from
the optional recording retention, no automatic retention policy bounds these stores; disk usage and replay cost grow with
retained history. See [operations.md](operations.md) for backup and space management,
and [building.md](building.md) for what software checks can establish.

## SMS commands

When configured with a command key, Phonelet recognizes encrypted `PC1` SMS, hides
recognized command text in history/notifications and queues complete, unambiguous live
commands for serial execution. Authentication, freshness and replay checks precede
execution; results use health notifications without secret arguments. With no key,
such messages are ordinary SMS. The current Wi-Fi command requires netplan, systemd
and wpa_supplicant and rolls back a failed change. Runtime builds commands from the
`commands/registry.py` list; each class's `from_settings` returns an instance or `None`
when unavailable on the host. Wi-Fi is available only with `netplan` and `wpa_cli` on
`PATH`; unavailable codes are rejected as unknown commands. Each built command's
`recover_at_start` hook runs before the database opens, including with the key disabled,
so database failures cannot prevent recovery. The format, generators and usage
are documented in [sms-commands.md](sms-commands.md).
