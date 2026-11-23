# Commands by encrypted SMS

Phonelet can accept commands sent as SMS to the gateway SIM. The only current command changes
Wi-Fi. Commands are optional and disabled by an empty `PHONELET_SMS_COMMAND_KEY`. Configure the
host first using [installation](install.md). Keep another access path available for network changes.

The text starts with `PC1 ` followed by unpadded Base64url. AES-256-GCM encrypts and authenticates
the command and arguments. There is no sender-number allowlist: possession of the key authorizes
a command. Phonelet sends no SMS reply. Results go to configured Telegram or Home Assistant health
notifications; enable at least one to receive them. The SIP SMS channel does not receive results.

## Generate and install the key

From the checkout on your development computer:

```sh
PYTHONPATH=src .venv/bin/python -m phonelet.sms_commands key
```

On the device, as root, use the active release: its directory under `/root/phonelet-releases` is the
service's `WorkingDirectory` and has its own `.venv`:

```sh
release=$(systemctl show phonelet -p WorkingDirectory --value)
PYTHONPATH="$release/src" "$release/.venv/bin/python" -m phonelet.sms_commands key
```

Any computer with Python 3.11 or later can run it from the source tree once `cryptography` is
installed; nothing installs the package itself:

```sh
PYTHONPATH=src python3 -m phonelet.sms_commands key
```

The `wifi` subcommand below runs in any of these ways. Generating the key or a command on the
device exposes the passphrase to the device while you type it; a separate computer avoids that.
The CLI asks for the passphrase twice, prints a `PHONELET_SMS_COMMAND_KEY=...` line, and prints
its fingerprint to stderr. Put the setting in `/root/phonelet/.env` and your private configuration
copy, then [deploy](operations.md#application-updates). The host stores the key, not the passphrase.
A malformed key stops startup and preflight without printing the key. One key serves all commands.
To change the passphrase, generate and deploy a new key.

Use at least five random words or at least 16 random characters. Keep the key, passphrase,
generated SMS and fingerprint private. A fingerprint is the first four bytes of SHA-256 of the
key, shown as eight hex characters. Compare the generator's fingerprint with the one the service logs at start
(`journalctl -u phonelet | grep fingerprint`); a mismatch usually means a mistyped passphrase.

### Security limits

Key derivation uses PBKDF2-HMAC-SHA256, 600,000 iterations, a 32-byte output and the fixed salt
`phonelet-sms-command-v1`. **Every installation uses the same salt.** The same passphrase therefore
produces the same key across installations. Anyone with one command SMS or its key fingerprint
can try passphrase guesses offline and reuse that work across installations. Use a strong, unique
passphrase; encryption does not make a weak passphrase safe.

The minimum length is checked only by the CLI's `key` subcommand, which refuses fewer than
16 characters. The HTML page warns but does not enforce this minimum. The CLI command generator,
key-derivation function and host do not enforce passphrase length. The host receives only a key
and cannot assess the original passphrase.

With an empty key, new `PC1` texts are ordinary SMS: their original ciphertext is stored and can
appear in notifications, without a command row or rejection alert. Already queued commands end
as `rejected:disabled`. Restoring the key does not reclassify SMS already ingested while disabled.

## Make and send a command

The CLI asks for secrets using hidden terminal input; it does not take passwords as arguments.
Without a terminal that hides input it stops. To generate a Wi-Fi command:

```sh
PYTHONPATH=src .venv/bin/python -m phonelet.sms_commands wifi --ssid 'Example Wi-Fi'
```

For an open network:

```sh
PYTHONPATH=src .venv/bin/python -m phonelet.sms_commands wifi --ssid 'Example Wi-Fi' --open
```

Send the printed `PC1 ...` text to the gateway SIM as an ordinary SMS. The CLI prints its length,
SMS part count and key fingerprint to stderr. Generate a fresh text for every attempt.

On a phone, open [tools/sms-command.html](../tools/sms-command.html). This single file uses
WebCrypto, makes no network requests and stores no form data. Enter the passphrase, choose Wi-Fi
and enter its fields. Copy the resulting text to your SMS app. An optional gateway number, such
as `+15555550123`, enables an `sms:` link. Use a trusted copy of the page.

Android Chrome and Firefox should support opening the local file. An iPhone's Quick Look does
not run its JavaScript. Use the CLI or serve your private copy over HTTPS if local-file WebCrypto
is unavailable. The page needs a browser context that provides WebCrypto.

## Format and command handling

The version-1 envelope is:

```text
PC1 Base64url(version byte | 12-byte nonce | ciphertext | 16-byte GCM tag)
plaintext = 6-byte big-endian Unix timestamp in milliseconds | 1-byte command code | arguments
AAD = ASCII "PC1" | version byte (1)
```

Base64url has no `=` padding. The encrypted timestamp comes from the sender's clock.
Use the generators rather than editing ciphertext. The format is implemented in
[envelope.py](../src/phonelet/sms_commands/envelope.py).

With a key configured, history and notifications show `[command]` instead of the SMS text.
Incomplete or ambiguous multipart commands show `[command, incomplete]`. Incomplete commands
wait for their parts; ambiguous ones never run. If part 1 is missing, Phonelet cannot see the
prefix: parts that consist only of base64url characters also show `[command, incomplete]`, other
parts appear as an ordinary incomplete SMS. A late part 1 updates the text.

An invalid SCTS is replaced by the journal file's receipt time by the host clock. On a board
without a real-time clock that time can lag until NTP synchronises the clock. A command whose
SCTS was replaced is then refused as `future` if the lag exceeds one hour.

The command worker handles complete commands one at a time, oldest first. It checks in order:

1. No configured key: `disabled`.
2. Damaged or noncanonical envelope: `bad_format`.
3. Wrong key or modified authenticated bytes: `auth_failed`.
4. Timestamp not newer than the last accepted command: `replay`.
5. Sender timestamp more than one hour ahead of the SMSC timestamp: `future`.
6. Sender timestamp more than 24 hours behind the SMSC timestamp, or SMSC timestamp more than
   48 hours old by the host clock: `stale`.
7. Unknown command code: `unknown_command`.
8. Invalid command arguments: `bad_format`. Any unexpected validation failure: `error`.

Rejections change no configuration and raise a health notification with the reason. A newly
made command rejected as stale can mean delayed SMS delivery or a host clock over 48 hours ahead.
Correct the clocks and generate a new command. A host clock that is behind does not reject on
that age check.

An accepted command advances one global replay timestamp for all command types, even if execution
later fails. Earlier generated texts then become replays. Rejections do not advance it: someone
holding a rejected text can resend it while it still passes the checks. A sender clock accepted
far ahead of the correct time blocks later texts until the clock catches up. Keep clocks accurate.

Before execution, Phonelet commits the timestamp and `running` state. After a restart, a command
left running ends as `interrupted` and is not repeated. If saving a result fails, the worker retries
saving it rather than executing again. Unexpected execution errors end as `failed:error`.
Acceptance, rejection, result and duration are logged without keys, ciphertext or secret arguments.
The durable driver journals and SMS fragment storage still retain the original SMS privately.

Restoring an older database can run recent commands again. An empty database's first replay
inventory suppresses historical command execution. Read [database recreation](operations.md#recreating-the-database)
and [backup recovery](operations.md#recovery-from-backup) before restoring data.

## Wi-Fi command

The host needs **netplan, systemd and wpa_supplicant**, with netplan-generated
`netplan-wpa-INTERFACE.service`. This command is not a generic NetworkManager or iwd configuration
path. Install the requirements or select `PHONELET_BOOTSTRAP_WIFI=1` at bootstrap:

```sh
apt-get install -y --no-install-recommends netplan.io wpasupplicant iw
```

At startup, the Wi-Fi command is available only when both `netplan` and `wpa_cli` are
installed and on `PATH`. `PHONELET_BOOTSTRAP_WIFI=1` installs them. Otherwise Wi-Fi SMS
commands are rejected as `rejected:unknown_command`; the CLI and HTML generator still work.
`/etc/netplan` must exist before Phonelet starts: its sandbox makes `/etc` read-only except that directory.
Restart Phonelet after installing `netplan.io`.

The host also needs `ip` and a working Wi-Fi interface. Confirm that netplan manages the interface
before enabling commands. Settings and defaults are in the
[configuration reference](install.md#configuration-reference). The defaults target `wlan0`,
`/etc/netplan/30-wifis-dhcp.yaml` and private rollback state at `/var/lib/phonelet/wifi` (0700).
Ensure other netplan files do not conflict with the Wi-Fi definition. Keep Ethernet access where possible.

SSID length is 1–32 UTF-8 bytes. Leading and trailing spaces are kept. Hidden networks are not
supported. Passwords must be 8–63 printable ASCII characters, or empty for an explicitly open
network. The CLI asks for the Wi-Fi password twice. SSID and password totalling up to 80 bytes
fit in one SMS; longer supported values take two. Logs and notifications name the SSID, never
the password.

An accepted command saves the previous netplan file and a durable rollback marker, then replaces
that file with the new network using DHCP for IPv4 and IPv6. The file and backup have mode 0600.
It runs `netplan apply` (90-second timeout), then restarts the interface's generated supplicant
unit (30-second timeout). The restart ensures the old supplicant does not remain on the old network.

Every two seconds, it checks the requested SSID and `wpa_state=COMPLETED` through `wpa_cli`,
an IPv4 address and a default route on the interface. Two consecutive successful polls count
as connected, within `PHONELET_WIFI_CONNECT_TIMEOUT` (120 seconds by default). A captive portal
or network without internet access can pass: this checks association and local network configuration.

On success, the saved file and marker are removed and a health notification reports connection.
On failure, Phonelet restores the previous file, applies netplan, restarts the supplicant and checks
connection again without requiring the new SSID. Without a previous file it removes the new one,
applies netplan and stops the supplicant, unless another netplan file still configures that
interface. The result reports the failure reason, whether configuration was restored and whether
the previous network connected. If apply or supplicant restart fails during rollback, the file can
be restored without network recovery; inspect the journal and use another access path.

A crash leaves the marker for startup recovery, which rolls back before the database opens.
The interrupted command is not repeated. If restoring the file itself fails, the marker stays and
rollback retries each poll. New changes end as `rollback_pending` until recovery succeeds, preserving
the good backup. A netplan apply or supplicant restart failure alone is not retried automatically.

Do not deploy or restart Phonelet during a Wi-Fi change. A normal attempt takes about two to five
minutes; with default timeouts, a change and rollback can take about eight minutes. Startup waits
for recovery to finish, so deployment (and `--check`) refuses while the marker exists: wait for
the running change to finish (the marker disappears); if Phonelet is not running, start it and
wait for `/health`; or follow the manual rollback below.

### Manual Wi-Fi rollback

Use another access path if automatic recovery cannot restore the configuration. Commands below
use default paths and `wlan0`; substitute your configured paths and interface. Run as root on the
host. Stop Phonelet first so it cannot change the same files:

```sh
systemctl stop phonelet
```

Inspect `/var/lib/phonelet/wifi/pending.json` locally; it records whether the prior file existed.
Keep private copies of the state and current configuration before changing them.

- If `"existed": true`, restore the saved file:

  ```sh
  install -m 0600 /var/lib/phonelet/wifi/previous.yaml /etc/netplan/30-wifis-dhcp.yaml
  ```

- If `"existed": false`, remove only the file created by the failed change:

  ```sh
  rm /etc/netplan/30-wifis-dhcp.yaml
  ```

- Without `pending.json` there is nothing to roll back. A lone `previous.yaml` is leftover state.

Then apply the restored configuration:

```sh
netplan apply
systemctl restart netplan-wpa-wlan0.service
wpa_cli -i wlan0 status
```

If you removed the file, stop the supplicant instead of restarting it, unless
`/run/systemd/system/netplan-wpa-wlan0.service` still exists because another netplan file defines
that interface. Confirm the network configuration is restored before removing the two rollback
state files and starting Phonelet:

```sh
rm /var/lib/phonelet/wifi/pending.json /var/lib/phonelet/wifi/previous.yaml
systemctl start phonelet
```

Only remove state files that exist and belong to this recovered change. Other netplan files,
including Ethernet configuration, are not changed by the command.
