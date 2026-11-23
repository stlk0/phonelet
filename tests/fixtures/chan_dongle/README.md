# Pinned chan_dongle test input

These unmodified upstream files are copied from
`https://github.com/wdoekes/asterisk-chan-dongle` revision
`7db8e75bc2b7c81b173e1bb465e376e64a70319a`, as declared in
`asterisk/sources.lock`. `SHA256.json` records their original bytes.
The upstream license and copyright files are included.

The PTY tests copy this small source subset into a temporary build directory,
verify its hashes, and apply all six repository patches with zero fuzz and zero
offsets before compilation. One PTY stands for `data=` on PCUI: URCs and command
replies are interleaved in it, as on the modem; voice is absent. They require no network, Asterisk installation,
serial hardware, or notification services. The test runner compiles the actual patched monitor,
TTY open routine, initialization command list, AT byte framing, response
classification, SMS inbox/retrieval and receive handlers (including one failed-read
retry and U1 quarantine), SMS memory selection
(`AT+CPMS`), the `+CPMS` total bound of full reads, idle periodic SM → ME recovery and `+CMTI`
memory rules, complete ERROR handlers, incoming RING handler, and complete PDU/character parsers. Command IDs are extracted from the
pinned header. The PTY modem keeps SIM (`"SM"`, 15 slots) and modem (`"ME"`,
23 slots) memories with their own 0-based slots across unplug. `AT+CPMS` takes
one to three memories and leaves the omitted ones; ATZ sets read and write to
SM, and new SMS go to the receive memory, which `+CMTI` names.

Asterisk channel creation, command queue storage, simulated time, the OK
dispatch of the commands the tests use, port locks, and reconnect scheduling use explicit adapters in
`../driver_runner.c`. Its queue keeps the upstream task order: an insert at the
head goes after the head task, and an unexpected result ends the rest of a task
unless the command ignores errors. The adapter close also drops the TIOCEXCL that upstream
`opentty` sets, as the last close of a real ttyUSB does; the harness keeps its
PTY slave open. The complete production `RES_CMGS` dispatch arm is also compiled
and exercised on late replies and a complete CMGS/prompt/PDU/CMGS/OK send using
the actual `at_enqueue_pdu` and prompt handler. The queue adapter retains each task's
first command and UID across command advancement. Queue allocation is adapted on the PTY stand; a separate C test
compiles `at_queue_insert_uid` itself with a failing allocation adapter.
The runner does not load chan_dongle as an Asterisk module.
It observes the RING callback but does not produce a real Asterisk CDR. Its
command deadlines are shortened for deterministic tests. `ADVANCE=n` advances the driver’s
`time(NULL)` and `CLOCK_MONOTONIC` by n seconds from a fixed baseline, while command deadlines use real time.
`STATE=` adapts channel count, ring/dialing/cwaiting, initialized/has_sms and
SMS-disabled flags under the monitor mutex; RING also exercises the actual handler.
`FAIL_CPMS=n`, `FAIL_CPMS_ME=n` and `FAIL_CMGR=n` make the next n `AT+CPMS` selections (any, or of ME) or `AT+CMGR` reads fail to queue. No real
voice channel or audio transport is created. The monitor and stdin
command writer share a real pthread mutex; the writer uses the same queue adapter
and actual at_queue_run path to write immediately. An idle-wait event lets tests
insert during an existing wait without waking the monitor. The adapters do not
replace byte framing, SMS parsing, spool persistence, or the monitor loop.
