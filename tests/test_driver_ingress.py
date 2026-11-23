"""Compile the exact new driver helper; no modem/Asterisk instance is used."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from phonelet.ingress import parse_fragment

pytestmark = pytest.mark.driver

PATCH = Path(__file__).resolve().parents[1]/'chan_dongle_patch/chan_dongle_ingress.patch'


@pytest.fixture(scope='module')
def producer(tmp_path_factory):
    if not shutil.which('cc'):
        pytest.skip('C compiler unavailable')
    root = tmp_path_factory.mktemp('driver')
    patch = PATCH.read_text()
    section = patch.split('+++ b/phonelet_ingress.h\n', 1)[1].split('\n--- ', 1)[0]
    header = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+')) + '\n'
    (root/'phonelet_ingress.h').write_text(header)
    (root/'producer.c').write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>
static int syncs;
static int inject_sync(int fd) {
    ++syncs;
    const char *fail = getenv("FAIL_SYNC"), *crash = getenv("CRASH_SYNC");
    if (fail && atoi(fail) == syncs) { errno = ENOSPC; return -1; }
    int rc = fsync(fd);
    if (crash && atoi(crash) == syncs) _exit(70);
    return rc;
}
static int inject_flush(FILE *f) {
    if (getenv("FAIL_WRITE")) { errno = ENOSPC; return EOF; }
    return fflush(f);
}
static int inject_link(const char *a, const char *b) {
    if (getenv("FAIL_LINK")) { errno = EIO; return -1; }
    return link(a,b);
}
static int inject_unlink(const char *path) {
    if (getenv("FAIL_UNLINK")) { errno = EACCES; return -1; }
    return unlink(path);
}
#define unlink inject_unlink
#define fsync inject_sync
#define fflush inject_flush
#define link inject_link
#include "phonelet_ingress.h"
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int result = phonelet_spool(argv[1], "dongle0", "sim1", "2026-09-25 12:00:00 +00:00",
                               "+7", 42, 2, 1, "hello,\nworld", 12, 4, "AABBCC");
    if (!result && syncs != 2) return 3;
    return result ? 1 : 0;
}
''')
    exe = root/'producer'
    subprocess.run(['cc','-Wall','-Wextra','-Werror','-std=gnu11',str(root/'producer.c'),'-o',str(exe)],check=True)
    return exe


@pytest.mark.parametrize('fault', ['FAIL_WRITE','FAIL_LINK','FAIL_SYNC=1','FAIL_SYNC=2'])
def test_no_delete_permission_on_durable_boundary_failure(producer, tmp_path, fault):
    key, _, value = fault.partition('=')
    result = subprocess.run([producer, tmp_path], env={**os.environ, key:value or '1'})
    assert result.returncode == 1  # caller must not enter SIM-delete path
    if fault != 'FAIL_SYNC=2':
        assert list(tmp_path.glob('*.sms')) == []
    # Retry while process/services remain otherwise live: subsequent write succeeds.
    assert subprocess.run([producer,tmp_path]).returncode == 0
    files = list(tmp_path.glob('*.sms'))
    assert files
    assert len({parse_fragment(p.read_bytes())['source_id'] for p in files}) == 1


@pytest.mark.parametrize('boundary', ['1','2'])
def test_crash_before_delete_leaves_replay_or_sim_retry(producer,tmp_path,boundary):
    result = subprocess.run([producer,tmp_path],env={**os.environ,'CRASH_SYNC':boundary})
    assert result.returncode == 70
    assert subprocess.run([producer,tmp_path]).returncode == 0
    records = [parse_fragment(p.read_bytes()) for p in tmp_path.glob('*.sms')]
    assert records and len({r['source_id'] for r in records}) == 1


def test_spool_tells_a_rejected_record_from_an_io_failure(producer, tmp_path):
    """Invalid numbering can never be published; a missing directory may be fixed and retried."""
    source = r'''
#include <assert.h>
#include "phonelet_ingress.h"
static int spool(const char *dir, int parts, int part) {
    return phonelet_spool(dir, "dongle0", "sim1", "2026-09-25 12:00:00 +00:00", "+7", 42, parts, part,
                          "x", 1, 4, "AABB");
}
int main(int argc, char **argv) {
    assert(argc == 3);
    assert(spool(argv[1], 2, 0) == PHONELET_SPOOL_REJECTED);
    assert(spool(argv[1], 2, 3) == PHONELET_SPOOL_REJECTED);
    assert(spool(argv[1], 0, 1) == PHONELET_SPOOL_REJECTED);
    assert(spool(argv[1], 256, 1) == PHONELET_SPOOL_REJECTED);
    assert(spool(argv[2], 2, 1) == PHONELET_SPOOL_IO_ERROR);  /* directory missing */
    assert(phonelet_spool(argv[1], "dongle0", "sim1", "", "", 0, 1, 1, NULL, 0, 4, "AABB") == PHONELET_SPOOL_OK);
    assert(spool(argv[1], 2, 2) == PHONELET_SPOOL_OK);
    return 0;
}
'''
    path = tmp_path/'results.c'
    path.write_text(source)
    exe = tmp_path/'results'
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-std=gnu11', '-I', str(producer.parent), str(path),
                    '-o', str(exe)], check=True)
    spool = tmp_path/'spool'
    spool.mkdir()
    subprocess.run([exe, spool, tmp_path/'missing'], check=True)
    records = sorted(parse_fragment(path.read_bytes())['source_id'].split(':')[0] for path in spool.glob('*.sms'))
    assert records == ['fragment', 'unparsed']  # the rejected calls published nothing


def test_driver_boundary_and_live_retry_are_before_sim_delete():
    patch = PATCH.read_text()
    added = '\n'.join(line[1:] for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++'))
    assert 'phonelet_spool(' in added
    assert 'pvt->phonelet_retry_at = phonelet_monotonic() + 30' in added
    assert 'time(NULL)' not in added  # retries never follow the wall clock
    storage = (PATCH.parent / 'chan_dongle_me_storage.patch').read_text()
    assert 'time(NULL)' not in '\n'.join(line for line in storage.splitlines() if line.startswith('+'))
    assert 'pvt->incoming_sms_index = -1U;\n    return 0;' in added
    assert 'at_retrieve_next_sms(&pvt->sys_chan, SUPPRESS_ERROR_DISABLED)' in added
    assert 'SMSJournalVersion: 1' in added
    assert 'SMSJournalStatus:' in added


def test_failed_driver_receive_retries_without_restart(producer,tmp_path):
    """Execute the actual added receive/retry branches with a fake SIM queue."""
    added = '\n'.join(line[1:] for line in PATCH.read_text().splitlines()
                      if line.startswith('+') and not line.startswith('+++'))
    start = added.index('                spooled = phonelet_spool(')
    end = added.index('goto journal_failed;', start) + len('goto journal_failed;')
    receive = added[start:end]
    start = added.index('        if (pvt->phonelet_retry_at &&')
    end = added.index('\n        }',start) + len('\n        }')
    retry = added[start:end]
    start = added.index('journal_failed:\n') + len('journal_failed:\n')
    failure = added[start:added.index('return 0;', start) + len('return 0;')]
    start = added.index('static inline time_t phonelet_monotonic(void)')
    monotonic = added[start:added.index('\n}', start) + 2]
    root = producer.parent
    source = r'''
#include <assert.h>
#include <time.h>
#include "phonelet_ingress.h"
#undef PHONELET_SMS_SPOOL
#define PHONELET_SMS_SPOOL spool_dir
#define PVT_ID(pvt) "dongle0"
#define ast_log(...) ((void)0)
#define SUPPRESS_ERROR_DISABLED 0
/* The wall clock must not matter: any time() use fails to compile. */
#define time(x) wall_clock_is_not_used
static time_t monotonic_now = 5000;
static int fake_clock_gettime(clockid_t clock, struct timespec *now) {
    assert(clock == CLOCK_MONOTONIC);
    *now = (struct timespec){monotonic_now, 0};
    return 0;
}
#define clock_gettime fake_clock_gettime
''' + monotonic + r'''
struct pvt { char imsi[20]; time_t phonelet_retry_at; unsigned phonelet_spool_failed;
             unsigned incoming_sms_index; int sys_chan; };
static int inbox = 1, deleted, rereads;
static struct pvt *device;
#define SMS_INDEX_MAX 256
static int is_sms_inbox_set(struct pvt *pvt, unsigned slot) { (void)pvt; return inbox && slot == 4; }
static void at_retrieve_next_sms(int *chan, int mode) {
    (void)chan; (void)mode;
    if (device->incoming_sms_index != -1U) {
        inbox = 0;
        device->incoming_sms_index = -1U;
    }
    if (inbox) { ++rereads; device->incoming_sms_index = 4; }
}
static int at_queue_run(struct pvt *pvt) { (void)pvt; return 0; }
static int order = 1;
static int receive(struct pvt *pvt, const char *spool_dir) {
    const char *scts = "2026-09-25 12:00:00 +00:00", *oa = "+7", *msg = "hello", *raw_pdu = "AABB";
    size_t msg_len = strlen(msg);
    struct { int parts,ref,order; } udh = {2,42,order};
    int spooled;
''' + receive + r'''
    ++deleted; /* normal SIM deletion path is reached only on producer success */
    at_retrieve_next_sms(&pvt->sys_chan, 0);
    return 0;
journal_rejected:
    return 2; /* the unparsed record path, not a retry */
journal_failed:
''' + failure + r'''
}
static int poll(struct pvt *pvt) {
''' + retry + r'''
    return 0;
e_cleanup:
    return -1;
}
int main(int argc, char **argv) {
    assert(argc == 2);
    struct pvt pvt = { .imsi = "sim1", .incoming_sms_index = 4 };
    device = &pvt;
    char missing[4096];
    snprintf(missing,sizeof(missing),"%s/missing",argv[1]);
    assert(!receive(&pvt,missing)); /* disk/path failure */
    assert(!deleted && inbox && pvt.phonelet_spool_failed);
    assert(pvt.incoming_sms_index == -1U && pvt.phonelet_retry_at == monotonic_now + 30);
    assert(!poll(&pvt) && !rereads); /* no tight retry loop */
    monotonic_now += 29;
    assert(!poll(&pvt) && !rereads);
    monotonic_now += 1;
    assert(!poll(&pvt) && rereads == 1 && pvt.incoming_sms_index == 4);
    assert(!receive(&pvt,argv[1])); /* same device, successful later receive */
    assert(deleted == 1 && !inbox);
    order = 3; /* part 3 of 2: rejected, never an I/O retry */
    pvt.incoming_sms_index = 4; pvt.phonelet_retry_at = 0; pvt.phonelet_spool_failed = 0;
    assert(receive(&pvt,argv[1]) == 2);
    assert(!pvt.phonelet_retry_at && !pvt.phonelet_spool_failed && deleted == 1);
    return 0;
}
'''
    path = tmp_path/'retry.c'
    path.write_text(source)
    exe = tmp_path/'retry'
    subprocess.run(['cc','-Wall','-Wextra','-Werror','-std=gnu11','-I',str(root),str(path),'-o',str(exe)],check=True)
    subprocess.run([exe,tmp_path],check=True)


def test_sms_memory_rules_of_the_storage_patch(tmp_path):
    """Run the exact AT+CPMS selection and +CMTI memory rules with a fake queue."""
    if not shutil.which('cc') or not shutil.which('patch'):
        pytest.skip('C compiler or patch unavailable')
    from tests.pty_modem import STORAGE_FUNCTIONS, extract_function, patch_sources

    build = tmp_path/'src'
    patch_sources(build)
    header = (build/'at_command.h').read_text()
    defines = '\n'.join(line for line in header.splitlines() if line.startswith('#define PHONELET_MEM'))
    functions = '\n'.join(extract_function((build/name).read_text(), function)
                          for name, names in STORAGE_FUNCTIONS.items() for function in names
                          if function != 'phonelet_storage_counts')
    source = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#define EXPORT_DEF static
#define LOG_ERROR 1
#define LOG_WARNING 2
#define LOG_NOTICE 3
#define SMS_INDEX_MAX 256
#define SUPPRESS_ERROR_DISABLED 0
#define CMD_AT_CPMS_SELECT 7
#define PVT_ID(pvt) "dongle0"
#define CONF_SHARED(pvt, field) ((pvt)->field)
#define ITEMS_OF(array) (sizeof(array) / sizeof((array)[0]))
#define ATQ_CMD_DECLARE_DYN(command) { .cmd = (command) }
typedef struct { int cmd; char *data; } at_queue_cmd_t;
struct pvt;
struct cpvt { struct pvt *pvt; };
typedef struct pvt {
    unsigned phonelet_mem, phonelet_mem_switch, phonelet_mem_pending, phonelet_mem_failed;
    unsigned incoming_sms_index;
    unsigned phonelet_counts_valid;
    unsigned phonelet_rescan_active, phonelet_rescan_polled;
    long phonelet_retry_at;
    time_t phonelet_rescan_end;
    int disablesms;
    int inbox[SMS_INDEX_MAX];
    struct cpvt sys_chan;
} pvt_t;
''' + defines + r'''
static char queued[8][64], logged[8][160];
static int queued_athead[8], queued_count, logged_count, polls, retrieved = -1, retrieve_result;
static void ast_log(int level, const char *format, ...) {
    va_list args;
    assert(logged_count < 8);
    int length = snprintf(logged[logged_count], 160, "%d ", level);
    va_start(args, format);
    vsnprintf(logged[logged_count++] + length, 160 - length, format, args);
    va_end(args);
}
static int at_fill_generic_cmd(at_queue_cmd_t *command, const char *format, ...) {
    va_list args;
    va_start(args, format);
    int length = vasprintf(&command->data, format, args);
    va_end(args);
    return length < 0;
}
static int at_queue_insert(struct cpvt *cpvt, at_queue_cmd_t *commands, unsigned count, int athead) {
    (void)cpvt;
    assert(count == 1 && commands[0].cmd == CMD_AT_CPMS_SELECT && queued_count < 8);
    queued_athead[queued_count] = athead;
    snprintf(queued[queued_count++], 64, "%s", commands[0].data);
    free(commands[0].data);
    return 0;
}
static int is_sms_inbox_set(struct pvt *pvt, unsigned slot) { return pvt->inbox[slot]; }
static int at_poll_sms(struct pvt *pvt) { (void)pvt; ++polls; return 0; }
static int at_enqueue_retrieve_sms(struct cpvt *cpvt, int slot, int mode) {
    (void)cpvt; (void)mode; retrieved = slot; return retrieve_result;
}
static void phonelet_storage_select(struct cpvt *cpvt, unsigned int mem, int athead);
static void phonelet_storage_next(struct cpvt *cpvt);
''' + functions + r'''
static struct pvt device;
static void reset(unsigned mem, unsigned failed) {
    memset(&device, 0, sizeof(device));
    device.sys_chan.pvt = &device;
    device.incoming_sms_index = -1U;
    device.phonelet_mem = mem;
    device.phonelet_mem_failed = failed;
    queued_count = logged_count = polls = retrieve_result = 0;
    retrieved = -1;
}
#define READ_ME "AT+CPMS=\"ME\"\r"
#define READ_SM "AT+CPMS=\"SM\"\r"
#define KEEP_SM "AT+CPMS=\"SM\",\"SM\",\"SM\"\r"
int main(void) {
    const unsigned SM = PHONELET_MEM_SM, ME = PHONELET_MEM_ME, ALL = PHONELET_MEM_ALL;
    /* +CMTI: <mem>,<index>, quoted or not; other memories are not ours. */
    assert(phonelet_storage_parse("+CMTI: \"ME\",3") == ME);
    assert(phonelet_storage_parse("+CMTI: \"SM\",3") == SM);
    assert(phonelet_storage_parse("+CMTI: ME,3") == ME);
    assert(phonelet_storage_parse("+CMTI:\"SM\",1") == SM);
    assert(phonelet_storage_parse("+CMTI: \"ME\" ,3") == ME);  /* as at_parse_cmti */
    assert(phonelet_storage_parse("+CMTI: me ,3") == ME);
    assert(phonelet_storage_parse("+CMTI: \"sm\",1") == SM);
    assert(!phonelet_storage_parse("+CMTI: \"SR\",1"));
    assert(!phonelet_storage_parse("+CMTI: \"MT\",1"));
    assert(!phonelet_storage_parse("+CMTI: \"SMX\",1"));
    assert(!phonelet_storage_parse("+CMTI: \"ME\""));
    assert(!phonelet_storage_parse("+CMTI \"ME\",1"));

    /* Init list OK: ME for all three. The SIM is read first, by <mem1> only,
     * queued after the init list and before the reads already queued. */
    reset(SM, 0);
    phonelet_storage_init(&device, 1);
    assert(device.phonelet_mem == ME && device.phonelet_mem_switch == SM && !polls);
    assert(queued_count == 1 && !strcmp(queued[0], READ_SM) && queued_athead[0] == 1);
    assert(!strcmp(logged[0], "3 [dongle0] SMS storage ME selected for read, write and receive\n"));
    reset(SM, 0);
    device.disablesms = 1;
    phonelet_storage_init(&device, 1);
    assert(device.phonelet_mem == ME && !queued_count && !device.phonelet_mem_switch);
    /* Init list error: the SIM for all three, ahead of the reads; ME is tried
     * again only for a +CMTI naming it. */
    reset(SM, 0);
    phonelet_storage_init(&device, 0);
    assert(device.phonelet_mem == SM && device.phonelet_mem_failed == ME);
    assert(queued_count == 1 && !strcmp(queued[0], KEEP_SM) && queued_athead[0] == 1);
    assert(device.phonelet_mem_switch == (SM | ALL));
    assert(!strcmp(logged[0], "2 [dongle0] Modem rejected SMS storage ME, keeping SM\n"));
    phonelet_storage_selected(&device, 1);
    assert(device.phonelet_mem == SM && !device.phonelet_mem_switch && polls == 1);
    assert(!strcmp(logged[1], "3 [dongle0] SMS storage SM selected for read, write and receive\n"));
    phonelet_storage_next(&device.sys_chan);
    assert(queued_count == 1);
    /* That one rejected too: the SIM stays the memory read and is not marked. */
    reset(SM, ME);
    device.phonelet_mem_switch = SM | ALL;
    phonelet_storage_selected(&device, 0);
    assert(device.phonelet_mem == SM && device.phonelet_mem_failed == ME && !polls && !queued_count);
    assert(!strcmp(logged[0], "2 [dongle0] Modem rejected SMS storage SM for read, write and receive, keeping SM\n"));

    /* The SIM read through with nothing waiting: reading returns to ME. */
    reset(SM, 0);
    phonelet_storage_next(&device.sys_chan);
    assert(queued_count == 1 && !strcmp(queued[0], READ_ME) && !queued_athead[0]);
    assert(device.phonelet_mem_switch == ME);
    phonelet_storage_next(&device.sys_chan);
    assert(queued_count == 1);  /* one selection at a time */
    /* No switch with a read in flight or a slot waiting. */
    reset(SM, 0);
    device.incoming_sms_index = 4;
    phonelet_storage_next(&device.sys_chan);
    device.incoming_sms_index = -1U;
    device.inbox[200] = 1;
    phonelet_storage_next(&device.sys_chan);
    assert(!queued_count && !device.phonelet_mem_switch);
    /* ME read, nothing pending; ME rejected: SM stays. */
    reset(ME, 0);
    phonelet_storage_next(&device.sys_chan);
    reset(SM, ME);
    phonelet_storage_next(&device.sys_chan);
    assert(!queued_count);

    /* OK: the selected memory is read in full. */
    reset(SM, 0);
    device.phonelet_mem_switch = ME;
    device.phonelet_mem_pending = ME;
    phonelet_storage_selected(&device, 1);
    assert(device.phonelet_mem == ME && !device.phonelet_mem_switch && !device.phonelet_mem_pending);
    assert(polls == 1 && logged_count == 1 && !queued_count);
    assert(!strcmp(logged[0], "3 [dongle0] SMS storage ME selected for reading\n"));
    reset(SM, 0);
    device.disablesms = 1;
    device.phonelet_mem_switch = ME;
    phonelet_storage_selected(&device, 1);
    assert(device.phonelet_mem == ME && !polls && logged_count == 1);
    /* Error: the memory before stays, nothing is read, no second try without a +CMTI. */
    reset(ME, 0);
    device.phonelet_mem_switch = SM;
    device.phonelet_mem_pending = SM;
    phonelet_storage_selected(&device, 0);
    assert(device.phonelet_mem == ME && device.phonelet_mem_failed == SM && !device.phonelet_mem_pending);
    assert(!polls && !queued_count && logged_count == 1);
    assert(!strcmp(logged[0], "2 [dongle0] Modem rejected SMS storage SM for reading, keeping ME\n"));
    reset(SM, 0);
    device.phonelet_mem_switch = ME;
    phonelet_storage_selected(&device, 0);
    assert(device.phonelet_mem == SM && device.phonelet_mem_failed == ME && !polls && !queued_count);
    /* Error after a +CMTI for the memory kept: that memory is read in full again. */
    reset(ME, 0);
    device.phonelet_mem_switch = SM;
    device.phonelet_mem_pending = SM | ME;
    phonelet_storage_selected(&device, 0);
    assert(polls == 1 && !device.phonelet_mem_pending && !queued_count && device.phonelet_mem == ME);
    /* A startup or periodic pass that has read nothing: the memory kept is
     * read in full once; after a read in this pass, nothing more. */
    reset(ME, 0);
    device.phonelet_rescan_active = 1;
    device.phonelet_mem_switch = SM;
    phonelet_storage_selected(&device, 0);
    assert(polls == 1 && device.phonelet_rescan_polled && device.phonelet_mem == ME && !queued_count);
    reset(SM, ME);
    device.phonelet_rescan_active = 1;
    device.phonelet_mem_switch = SM | ALL;
    phonelet_storage_selected(&device, 0);
    assert(polls == 1 && device.phonelet_mem == SM && !queued_count);
    reset(SM, 0);
    device.phonelet_rescan_active = device.phonelet_rescan_polled = 1;
    device.phonelet_mem_switch = ME;
    phonelet_storage_selected(&device, 0);
    assert(!polls && !queued_count && device.phonelet_mem_failed == ME);
    reset(SM, 0);
    device.phonelet_rescan_active = 1;
    device.phonelet_mem_switch = SM;
    phonelet_storage_selected(&device, 1);
    assert(polls == 1 && device.phonelet_rescan_polled);
    /* A reply with no selection queued changes nothing. */
    reset(ME, 0);
    phonelet_storage_selected(&device, 1);
    assert(device.phonelet_mem == ME && !polls && !logged_count);

    /* +CMTI for the memory read is read at once; a queueing error is returned. */
    reset(ME, 0);
    assert(!phonelet_storage_notice(&device, "+CMTI: \"ME\",5", 5) && retrieved == 5);
    retrieve_result = -1;
    assert(phonelet_storage_notice(&device, "+CMTI: \"ME\",6", 6) == -1);
    /* Another memory: its slot is never read here; it is selected when idle. */
    reset(ME, 0);
    device.incoming_sms_index = 9;
    assert(!phonelet_storage_notice(&device, "+CMTI: \"SM\",2", 2));
    assert(retrieved == -1 && device.phonelet_mem_pending == SM && !queued_count);
    device.incoming_sms_index = -1U;
    phonelet_storage_next(&device.sys_chan);
    assert(queued_count == 1 && !strcmp(queued[0], READ_SM) && device.phonelet_mem_switch == SM);
    /* The memory being selected is read in full anyway. */
    assert(!phonelet_storage_notice(&device, "+CMTI: \"SM\",3", 3));
    assert(retrieved == -1 && queued_count == 1);
    /* "ME" while the SIM is selected or read: ME waits and is read after. */
    assert(!phonelet_storage_notice(&device, "+CMTI: \"ME\",4", 4));
    assert(retrieved == -1 && queued_count == 1 && device.phonelet_mem_pending == (SM | ME));
    phonelet_storage_selected(&device, 1);
    assert(device.phonelet_mem == SM && device.phonelet_mem_pending == ME && polls == 1);
    device.inbox[0] = 1;
    assert(!phonelet_storage_notice(&device, "+CMTI: \"ME\",0", 0));
    assert(retrieved == -1 && queued_count == 1 && device.phonelet_mem_pending == ME);
    device.inbox[0] = 0;
    phonelet_storage_next(&device.sys_chan);
    assert(queued_count == 2 && !strcmp(queued[1], READ_ME));
    /* Unknown memory: a WARNING with slot only, nothing read. */
    reset(ME, 0);
    assert(!phonelet_storage_notice(&device, "+CMTI: \"SR\",1", 1));
    assert(retrieved == -1 && !queued_count && !device.phonelet_mem_pending && logged_count == 1);
    assert(!strcmp(logged[0], "2 [dongle0] New SMS in slot 1 of a memory that is not read; it stays there\n"));
    /* A rejected memory: each +CMTI naming it selects it once more. */
    reset(SM, ME);
    assert(!phonelet_storage_notice(&device, "+CMTI: \"ME\",7", 7));
    assert(retrieved == -1 && !device.phonelet_mem_failed && device.phonelet_mem_pending == ME);
    assert(queued_count == 1 && !strcmp(queued[0], READ_ME) && device.phonelet_mem_switch == ME && !logged_count);
    phonelet_storage_selected(&device, 0);
    assert(device.phonelet_mem == SM && device.phonelet_mem_failed == ME && !device.phonelet_mem_pending);
    assert(queued_count == 1 && !polls);
    assert(!phonelet_storage_notice(&device, "+CMTI: \"ME\",8", 8));
    assert(queued_count == 2 && !strcmp(queued[1], READ_ME) && retrieved == -1);
    phonelet_storage_selected(&device, 1);
    assert(device.phonelet_mem == ME && !device.phonelet_mem_failed && polls == 1);
    return 0;
}
'''
    path = tmp_path/'storage.c'
    path.write_text(source)
    exe = tmp_path/'storage'
    subprocess.run(['cc', '-D_GNU_SOURCE', '-Wall', '-Wextra', '-Werror', '-std=gnu11', str(path), '-o', str(exe)], check=True)
    subprocess.run([exe], check=True)


def test_production_ok_handler_makes_the_storage_calls(tmp_path):
    """driver_runner.c mirrors at_response_ok; the real handler makes the same calls."""
    if not shutil.which('patch'):
        pytest.skip('patch unavailable')
    from tests.pty_modem import extract_function, patch_sources

    build = tmp_path/'src'
    patch_sources(build)
    handler = extract_function((build/'at_response.c').read_text(), 'at_response_ok')
    for case, call in (('CMD_AT_CPMS', 'phonelet_storage_init(pvt, 1);'),
                       ('CMD_AT_CPMS_SELECT', 'phonelet_storage_selected(pvt, 1);'),
                       ('CMD_AT_CMGR', 'phonelet_sms_unanswered(pvt, at_cmd_suppress_error_mode(ecmd->flags));')):
        body = re.search(rf'case {case}:(.*?)break;', handler, re.S)
        assert body and call in body.group(1), case
        assert handler.index(call) < handler.rindex('at_queue_handle_result')


def test_spool_collision_never_replaces_published_record(producer, tmp_path):
    """mkstemp only protects the temporary name, not its published .sms name."""
    root = producer.parent
    source = r'''
#include <assert.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>
static int repeated_mkstemp(char *name) {
    strcpy(name + strlen(name) - 6, "REPEAT");
    return open(name, O_CREAT | O_EXCL | O_RDWR, 0600);
}
#define mkstemp repeated_mkstemp
#include "phonelet_ingress.h"
int main(int argc, char **argv) {
    assert(argc == 2);
    assert(!phonelet_spool(argv[1], "dev", "sim", "stamp", "sender", 0, 1, 1,
                          "first!", 6, 1, "AA"));
    int result = phonelet_spool(argv[1], "dev", "sim", "stamp", "sender", 0, 1, 1,
                               "second", 6, 2, "BB");
    return result ? 1 : 0;
}
'''
    path, exe = tmp_path/'collision.c', tmp_path/'collision'
    path.write_text(source)
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-std=gnu11', '-I', str(root),
                    str(path), '-o', str(exe)], check=True)
    result = subprocess.run([exe, tmp_path])
    files = list(tmp_path.glob('*.sms'))
    assert any(b'666972737421' in path.read_bytes() for path in files)
    assert result.returncode == 1 or len(files) == 2


def test_queue_uid_allocation_failure_is_safe(tmp_path):
    """Compile the actual queue insertion function; PTY queue storage is adapted."""
    from tests.pty_modem import extract_function, patch_sources

    build = tmp_path/'src'
    patch_sources(build)
    function = extract_function((build/'at_queue.c').read_text(), 'at_queue_insert_uid')
    source = r'''
#include <assert.h>
#include <stddef.h>
#define EXPORT_DEF static
struct pvt { int unused; };
struct cpvt { struct pvt *pvt; };
typedef struct { int unused; } at_queue_cmd_t;
typedef struct { int uid; } at_queue_task_t;
static at_queue_task_t allocated;
static int fail, frees;
static at_queue_task_t *at_queue_add(struct cpvt *c, at_queue_cmd_t *cmd, unsigned count, int head) {
    (void)c; (void)cmd; (void)count; (void)head;
    return fail ? NULL : &allocated;
}
static void at_queue_free_data(at_queue_cmd_t *cmd) { (void)cmd; ++frees; }
static int at_queue_run(struct pvt *pvt) { (void)pvt; return 0; }
''' + function + r'''
int main(void) {
    struct pvt pvt;
    struct cpvt cpvt = { &pvt };
    at_queue_cmd_t cmds[2];
    fail = 1;
    assert(at_queue_insert_uid(&cpvt, cmds, 2, 0, 42) == 1);
    assert(frees == 2);
    fail = 0;
    assert(at_queue_insert_uid(&cpvt, cmds, 2, 0, 42) == 0);
    assert(allocated.uid == 42 && frees == 2);
    return 0;
}
'''
    path, exe = tmp_path/'queue.c', tmp_path/'queue'
    path.write_text(source)
    subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-std=gnu11', str(path), '-o', str(exe)], check=True)
    subprocess.run([exe], check=True)


def test_published_record_survives_failed_temporary_unlink(producer, tmp_path):
    result = subprocess.run([producer, tmp_path], env={**os.environ, 'FAIL_UNLINK': '1'})
    assert result.returncode == 0  # producer also checks both fsync calls
    [record] = list(tmp_path.glob('*.sms'))
    assert parse_fragment(record.read_bytes())['text'] == 'hello,\nworld'
    [temporary] = [path for path in tmp_path.glob('.fragment-*') if path.suffix != '.sms']
    assert record.stat().st_ino == temporary.stat().st_ino
