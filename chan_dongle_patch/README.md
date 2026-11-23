# Phonelet patches for chan_dongle

These six patches target
[wdoekes/asterisk-chan-dongle](https://github.com/wdoekes/asterisk-chan-dongle), commit
`7db8e75bc2b7c81b173e1bb465e376e64a70319a`, pinned in
[sources.lock](../asterisk/sources.lock). They serve Phonelet's journal-based reception
contract. This patched driver serves only Phonelet: the incoming `sms` dialplan path
and new-SMS AMI events are removed, and Python owns multipart assembly. It is not a
drop-in replacement for installations relying on upstream SMS delivery hooks.

## Order and purpose

[series](series) is the authoritative apply order. Each line is the short name in
`chan_dongle_NAME.patch`. Run `bash scripts/prepare-sources.sh NEW_DIRECTORY` from the
repository root to fetch the pinned sources and apply the complete series. The
destination must not exist. For build instructions and test coverage, see
[building.md](../docs/building.md).

| Order | Patch | Why it exists |
|---|---|---|
| 1 | [ingress](chan_dongle_ingress.patch) | Journals each SMS fragment before deletion, fails a read whose `+CMGR` header lacks its fields, and exposes `SMSJournalVersion` / `SMSJournalStatus` through `DongleShowDevices`. Replaces transient delivery hooks with recoverable files. |
| 2 | [outgoing Base64](chan_dongle_outgoing_base64.patch) | Adds `DongleSendSMSBase64`; decodes UTF-8 text after Asterisk splits application arguments, so punctuation in the SMS cannot become argument syntax. |
| 3 | [unexpected ERROR](chan_dongle_unexpected_error.patch) | Guards an ERROR with no waiting command against a null dereference, and ignores late `+CMGS` without a matching SMS-send task in its post-prompt phase. |
| 4 | [command deadline](chan_dongle_command_deadline.patch) | Gives a command written by another thread during the monitor's wait its own remaining response time instead of expiring it at the older wait's timeout. |
| 5 | [readable deadline](chan_dongle_readable_deadline.patch) | Expires a sent command even if unsolicited notifications keep the data port continuously readable; a missing response cannot wait forever. |
| 6 | [ME storage](chan_dongle_me_storage.patch) | Receives into modem memory and recovers retained SMS from both SIM and modem memory on initialization and idle periodic scans. |

## Serial ports

Use the upstream two-port scheme: `data=` on PCUI (USB interface 02), `audio=` on
interface 01, and no `urc=` setting. Interface 00 (MDM) is not opened by this driver.
Stable symlinks may be chosen at installation; the example uses descriptive names:

```ini
[dongle0]
data=/dev/dongle0-at
audio=/dev/dongle0-audio
```

PCUI carries both AT replies and unsolicited notifications such as RING and `+CMTI`
in one byte stream. E1550 port and storage behaviour, including the `AT^PORTSEL` hazard
([commands never to send](../docs/e1550.md#commands-never-to-send)), is summarized in [e1550.md](../docs/e1550.md).

## SMS journal contract

The ingress producer writes to `/var/spool/asterisk/phonelet-sms`. The directory must
be trusted, writable by Asterisk and readable by Phonelet, not world-writable.
It creates a temporary file, sets mode 0640, checks writes and `fflush`, fsyncs and
closes the file, publishes a unique `.sms` name using `link`/`unlink` without replacing
an existing record, then fsyncs the directory. Only success permits `autodeletesms`
to delete the source slot. A write failure retains it and schedules a retry after
30 seconds; `SMSJournalStatus` stays `error` until inbox processing recovers.

Parsed records are ASCII, TAB-separated, LF-terminated version 1 records:

```text
1  device_hex  sim_hex  scts_hex  sender_hex  ref  total  part  text_hex  slot  pdu_hex
```

Spaces above stand for TABs. String fields are UTF-8 encoded as hex; numeric fields
are decimal. Part numbering is `1 <= part <= total <= 255`, with 8- or 16-bit
concatenation references preserved. The raw PDU is saved before in-place decoding.
An unparsed record has five fields: `U1 device_hex sim_hex slot pdu_hex`, with the
same separators. Both forms contain private data and need restricted storage.

Only a response to the driver's own inbox read can journal/delete a slot. Because
`+CMGR` has no slot number, an unexpected final reply to a read is followed by an
`AT` synchronization barrier before the next inbox read. This prevents a late PDU
from being attributed to the next slot. Extra replies to manual reads immediately
followed by inbox reads remain outside that protection.

After a parse failure the driver reads the slot once more. Two matching nonempty
PDUs without extraneous lines can be durably stored as `U1` before deletion; Python
then raises a health alert without the PDU. Invalid concatenation numbering also
takes this path. Different replies or interleaved notifications leave the SMS in
storage. Upstream multiline framing can swallow a notification between `+CMGR` and
its final `OK`; a later scan recovers retained SMS. With no final reply, the command
deadline causes reconnect instead.

## ME storage state machine

The ME patch avoids SIM write failures observed for some short final multipart
fragments; the modem acknowledges reception into its own storage instead. This is
a state machine designed around one SIM and one modem, tested on a PTY bench and
Huawei E1550 firmware **11.608.13.02.00 only**. The receive memory `"ME"` is fixed in
code, with no configuration option. Firmware conclusions and their limits are in
[e1550.md](../docs/e1550.md).

Initialization selects `AT+CPMS="ME","ME","ME"` for read, write and receive. It then
selects only the read/delete memory with `AT+CPMS="SM"`, scans the SIM, switches back
with `AT+CPMS="ME"` and scans ME. New SMS continue arriving in the receive memory
throughout. If the initial ME selection is rejected, the driver warns and attempts
`SM,SM,SM`, keeping SMS enabled rather than disabling reception.

A memory switch waits until no read is in flight and no inbox slots are pending;
FIFO command order puts it after previous reads and deletions. `+CMTI` for the current
memory reads its slot immediately. A notice naming the other memory schedules a
full read there when the inbox becomes idle, then a return to ME. Slot numbers never
cross memory boundaries. An unknown memory name produces a content-free warning and
is left untouched.

A rejected read-memory selection keeps the previous memory selected. If a startup
or periodic pass has not read anything yet, it scans that retained memory once.
The failed target is retried once for a later `+CMTI` naming it, on reinitialization,
**and on the next periodic pass**: each pass clears the failed-memory mask and tries
SM then ME afresh. It does not spin on a rejected selection. SMS in inaccessible
memory wait there until a later selection succeeds. The receive-memory fallback is
not automatically changed back by a read-only selection.

Full scans visit slots `0..total-1`, using the first `used,total` pair from the reply
to that same `AT+CPMS` selection. `used` is a count, not a slot bound. Missing or invalid
counts fall back to all 256 indices; counts reset on every selection. Explicit
`+CMTI` and retry slots are not limited by the scan bound. Empty scan slots are normal.

Periodic recovery starts 600 seconds after the previous pass finishes, only with
initialized SMS support, no channels, no RING in the last 60 seconds, an empty command
queue and no in-flight or pending inbox reads. Stale `ring`, `dialing` or `cwaiting`
flags without a channel do not indefinitely block it. Calls beginning during a pass
do not interrupt it. Failure to queue/send a selection stops the pass with a warning;
the next attempt waits another interval. A journal failure holds the current memory
until retry, and reconnect starts a fresh initialization scan. There are no catch-up
passes after a delay.

## Limits and licensing

File and directory fsync run under the device lock in the monitor thread. Slow
storage can therefore delay audio handling and may cause a click during a call.
The 30-second journal/read retry, periodic scans and the recent-RING window all use
`CLOCK_MONOTONIC`: a wall-clock step (a board without RTC, NTP) does not move them.

Incoming UCS-2 decoding preserves valid UTF-16 pairs and replaces isolated surrogates
with U+FFFD. A pair split across SMS fragments is replaced in each decoded fragment;
the original bytes remain in the raw PDU. Outgoing encoding remains strict.
PTY tests do not establish real USB hangup behaviour or storage survival under
physical power loss. General reception, assembly and delivery guarantees are in
[architecture.md](../docs/architecture.md).

The patches are derivative works of chan_dongle under GPL-2.0; see
[COPYING.GPL-2.0](../COPYING.GPL-2.0) and [NOTICE](../NOTICE). The unexpected-error and
command-deadline patches incorporate GPL-2.0 changes by Sheepsticked from
[aster](https://github.com/Sheepsticked/aster), commit `65e5a21`, as recorded in their
headers and NOTICE. Phonelet's MIT service does not link to the driver; it communicates
through the journal and AMI. The pinned upstream source plus this ordered series is
the build input; no separate Phonelet driver fork is required.
