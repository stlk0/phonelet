/* Isolated adapters around exact pinned/patched chan_dongle functions.
 * There is no Asterisk instance, real modem, channel, or notification backend.
 * All open paths are checked to resolve to PTYs, including after replacement.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>
#include <errno.h>
#include <stdarg.h>
#include <limits.h>
#include <fcntl.h>
#include <signal.h>
#include <termios.h>
#include <sys/ioctl.h>
#include <sys/uio.h>
#include <sys/time.h>
#include <poll.h>
#include <time.h>
#include <iconv.h>
#include <pthread.h>

#include "export.h"
#include "mutils.h"
#include "at_response.h"
#include "ringbuffer.h"
#include "char_conv.h"
#include "pdu.h"
#include "error.h"
#include "at_parse.h"
#include "gsm7_luts.h"

static const char *spool_directory;
#define PHONELET_SMS_SPOOL spool_directory
#include "phonelet_ingress.h"

#define ICONV_CONST
#define ICONV_T iconv_t
#define DATA_READ_TIMEOUT 100
#define SMS_INDEX_MAX 256
#define SMSDB_PAYLOAD_MAX_LEN 1024
#define SMSDB_DST_MAX_LEN 128
#define VOLUME_SYNC_BEGIN 0
#define VOLUME_SYNC_DONE 4
#define LOG_ERROR 1
#define LOG_WARNING 2
#define LOG_NOTICE 3
#define ast_mutex_lock pthread_mutex_lock
#define ast_mutex_unlock pthread_mutex_unlock
#define DEBUG_ATLEAST(level) 0
#define AST_LOG_DEBUG 4
#define ast_debug(...) ((void)0)
#define ast_verb(...) ((void)0)
#define ast_copy_string(dst,src,len) snprintf(dst,len,"%s",src)
#define CONF_SHARED(pvt,field) ((pvt)->field)
#define PVT_ID(pvt) ((pvt)->id)
#define PVT_STAT(pvt,field) ((pvt)->field)
#define PVT_NO_CHANS(pvt) (!(pvt)->chansno)
#define at_cmd2str(cmd) "test command"
#define dev_state2str(state) "test state"
#define ATQ_CMD_FLAG_SUPPRESS_ERROR 4
#define CALL_FLAG_NEED_HANGUP 1
#define CALL_FLAG_HOLD_OTHER 2
#define CALL_STATE_INIT 0
#define CPVT_TEST_FLAG(cpvt,flag) ((cpvt)->call_flags & (flag))
#define CPVT_SET_FLAGS(cpvt,flags) ((cpvt)->call_flags |= (flags))
#include "at_command_ids.inc"
#define ATQ_CMD_TIMEOUT_MEDIUM 5
#define ATQ_CMD_TIMEOUT_LONG 40
#define ATQ_CMD_FLAG_DEFAULT 0
#define ATQ_CMD_FLAG_STATIC 1
#define ATQ_CMD_FLAG_IGNORE 2
#define ATQ_CMD_DECLARE_DYN2(c,r) {.cmd=(c),.res=(r)}
#define ATQ_CMD_DECLARE_DYN(c) ATQ_CMD_DECLARE_DYN2(c,RES_OK)
/* The static init list keeps its data; its commands get the runner deadline. */
#define ATQ_CMD_DECLARE_STF(c,r,d,f) {.cmd=(c),.res=(r),.flags=ATQ_CMD_FLAG_STATIC|(f),.data=(char*)(d),.length=STRLEN(d)}
#define ATQ_CMD_DECLARE_ST(c,d) ATQ_CMD_DECLARE_STF(c,RES_OK,d,ATQ_CMD_FLAG_DEFAULT)
#define ATQ_CMD_DECLARE_STI(c,d) ATQ_CMD_DECLARE_STF(c,RES_OK,d,ATQ_CMD_FLAG_IGNORE)
#define ast_free free
#define ast_malloc malloc
#define ast_strdup strdup

typedef struct at_queue_cmd {
    int cmd;
    at_res_t res;
    unsigned flags;
    struct timeval timeout;
    char *data;
    unsigned length;
} at_queue_cmd_t;
struct queue_node { at_queue_cmd_t command; unsigned task; int first_cmd, uid; struct queue_node *next; };
struct pvt;
struct cpvt { struct pvt *pvt; unsigned call_flags; int call_idx, state; void *channel; };
typedef struct at_queue_task {
    struct cpvt *cpvt;
    const at_queue_cmd_t *command;
    int uid;
    at_queue_cmd_t cmds[1];
} at_queue_task_t;
enum ast_control_frame_type { AST_CONTROL_BUSY = 5, AST_CONTROL_CONGESTION = 8 };
typedef struct pvt {
    char id[32], imsi[20];
    pthread_mutex_t lock;
    int data_fd, audio_fd, timeout;
    int terminate_monitor, desired_state, initialized;
    int ring, dialing, cwaiting, rings, volume_sync_step, chansno;
    int autodeletesms, disablesms, resetdongle, u2diag;
    int has_voice, has_voice_quectel, has_sms, outgoing_sms;
    unsigned incoming_sms_index, phonelet_spool_failed, phonelet_parse_retry_slot;
    char phonelet_bad_pdu[4096];
    unsigned phonelet_mem, phonelet_mem_switch, phonelet_mem_pending, phonelet_mem_failed;
    unsigned phonelet_counts_valid, phonelet_total;
    unsigned phonelet_poll_active, phonelet_poll_cursor;
    unsigned char phonelet_poll_seen[SMS_INDEX_MAX];
    unsigned phonelet_rescan_active, phonelet_rescan_polled, phonelet_rescan_sm, phonelet_rescan_me;
    time_t phonelet_rescan_end, phonelet_ring_at;
    unsigned char inbox[SMS_INDEX_MAX];
    time_t phonelet_retry_at;
    unsigned long d_read_bytes, at_responses;
    struct cpvt sys_chan;
    struct cpvt *last_dialed_cpvt;
    struct queue_node *head, *tail;
    int ready_announced, idle_wait;
} pvt_t;
static struct pvt *monitor_pvt;
typedef struct { const char *name, *value; } channel_var_t;
static int command_timeout_ms;
__thread int chan_dongle_err;
static const at_response_t responses[] = { AT_RESPONSES_TABLE(AT_RES_AS_STRUCTLIST) };
const at_responses_t at_responses = {responses, 2, ITEMS_OF(responses), RES_MIN, RES_MAX};

static void ast_log(int level, const char *format, ...) {
    (void)level;
    char *message;
    va_list args;
    va_start(args, format);
    int length = vasprintf(&message, format, args);
    va_end(args);
    if (length < 0) abort();
    printf("LOG\t%s", message);
    free(message);
}
static long long monotonic_ms(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) abort();
    return (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}
/* Test time advances only on stdin, leaving command deadlines on real time:
 * the driver's time() and CLOCK_MONOTONIC (monotonic_ms above stays real).
 * ADVANCE= moves both; WALL= steps only the wall clock, as NTP or a board without RTC does. */
static time_t test_time_offset, test_wall_offset;
static time_t test_time(time_t *result) {
    time_t now = 1000000 + test_time_offset + test_wall_offset;
    if (result) *result = now;
    return now;
}
#define time test_time
static int test_clock_gettime(clockid_t clock, struct timespec *now) {
    if (clock != CLOCK_MONOTONIC) abort();
    *now = (struct timespec){1000000 + test_time_offset, 0};
    return 0;
}
#define clock_gettime test_clock_gettime

const char *at_res2str(at_res_t result) {
    return result >= RES_MIN && result <= RES_MAX ? responses[result-RES_MIN].name : "UNKNOWN";
}
static struct timeval ast_tvnow(void) {
    struct timeval result;
    gettimeofday(&result, NULL);
    return result;
}
static struct timeval ast_tvadd(struct timeval a, struct timeval b) {
    a.tv_sec += b.tv_sec;
    a.tv_usec += b.tv_usec;
    if (a.tv_usec >= 1000000) { ++a.tv_sec; a.tv_usec -= 1000000; }
    return a;
}
static int ast_tvdiff_ms(struct timeval a, struct timeval b) {
    return (a.tv_sec-b.tv_sec)*1000 + (a.tv_usec-b.tv_usec)/1000;
}
/* The wait of the actual at_wait. Like Asterisk, any revents (also POLLHUP or
 * POLLERR) make the fd the winner. It also observes an idle wait before asking
 * the writer to insert. No wakeup is added: production at_wait must finish its
 * original wait with the newly written head. */
static int ast_waitfor_n_fd(int *fd, int count, int *ms, int *exception) {
    (void)count; (void)exception;
    ast_mutex_lock(&monitor_pvt->lock);
    monitor_pvt->idle_wait = !monitor_pvt->head;
    if (monitor_pvt->idle_wait) puts("IDLE_WAIT");
    ast_mutex_unlock(&monitor_pvt->lock);
    struct pollfd pollfd = {*fd, POLLIN | POLLPRI, 0};
    int ready = poll(&pollfd, 1, *ms);
    ast_mutex_lock(&monitor_pvt->lock);
    if (monitor_pvt->idle_wait && ready == 0 && monitor_pvt->head &&
        monitor_pvt->head->command.length == 0)
        puts("IDLE_EXPIRED_WITH_COMMAND");
    monitor_pvt->idle_wait = 0;
    ast_mutex_unlock(&monitor_pvt->lock);
    return ready > 0 ? *fd : -1;
}

/* Queue storage and Asterisk task ownership adapters. The monitor uses actual
 * upstream at_queue_run/timeout and the real SMS inbox helper functions. */
static const at_queue_cmd_t *at_queue_head_cmd(const struct pvt *pvt) {
    return pvt->head ? &pvt->head->command : NULL;
}
static const struct at_queue_task *at_queue_head_task(struct pvt *pvt) {
    static struct at_queue_task task;
    if (!pvt->head) return NULL;
    task.cpvt = &pvt->sys_chan;
    task.command = &pvt->head->command;
    task.cmds[0].cmd = pvt->head->first_cmd;
    task.uid = pvt->head->uid;
    return &task;
}
static const at_queue_cmd_t *at_queue_task_cmd(const at_queue_task_t *task) {
    return task ? task->command : NULL;
}
static void queue_control_channel(struct cpvt *cpvt, enum ast_control_frame_type control) {
    printf("CONTROL\t%d\t%u\n", control, cpvt->call_flags);
}
static at_queue_cmd_t *at_queue_head_cmd_nc(struct pvt *pvt) {
    return pvt->head ? &pvt->head->command : NULL;
}
/* As upstream at_queue_remove_cmd: a result other than the expected one ends
 * the rest of the command's task, unless the command ignores it. */
static void at_queue_remove_cmd(struct pvt *pvt, at_res_t result) {
    struct queue_node *old = pvt->head;
    if (!old) return;
    unsigned task = old->task;
    int end = old->command.res != result && !(old->command.flags & ATQ_CMD_FLAG_IGNORE);
    do {
        old = pvt->head;
        pvt->head = old->next;
        if (!pvt->head) pvt->tail = NULL;
        if (!(old->command.flags & ATQ_CMD_FLAG_STATIC)) free(old->command.data);
        free(old);
    } while (end && pvt->head && pvt->head->task == task);
}
static void at_queue_handle_result(struct pvt *pvt, at_res_t result) {
    at_queue_remove_cmd(pvt, result);
}
static void at_queue_free_data(at_queue_cmd_t *command) {
    if (!(command->flags & ATQ_CMD_FLAG_STATIC)) {
        free(command->data);
        command->data = NULL;
    }
    command->length = 0;
}
static int at_write(struct pvt *pvt, const char *data, size_t length) {
    return write(pvt->data_fd, data, length) == (ssize_t)length ? 0 : -1;
}
static int at_queue_run(struct pvt *pvt);
/* FAIL_CPMS=n / FAIL_CPMS_ME=n / FAIL_CMGR=n on stdin: the next n AT+CPMS
 * selections (of any memory or of ME) or AT+CMGR reads fail to queue. */
static int fail_cpms_select, fail_cpms_me, fail_cmgr;
/* The stdin FAIL_* counter that refuses this insert, or NULL. */
static int *injected_failure(const at_queue_cmd_t *first) {
    if (fail_cpms_select && first->cmd == CMD_AT_CPMS_SELECT) return &fail_cpms_select;
    if (fail_cpms_me && first->cmd == CMD_AT_CPMS_SELECT && strstr(first->data, "\"ME\"")) return &fail_cpms_me;
    if (fail_cmgr && first->cmd == CMD_AT_CMGR) return &fail_cmgr;
    return NULL;
}
/* As upstream at_queue_add: one insert is one task, at the tail or, with
 * athead, right after the head task. */
static int at_queue_insert_uid(struct cpvt *cpvt, at_queue_cmd_t *commands, unsigned count, int athead, int uid) {
    static unsigned tasks;
    struct pvt *pvt = cpvt->pvt;
    struct queue_node *after = athead ? pvt->head : pvt->tail;
    int *failure = injected_failure(&commands[0]);
    if (failure) {
        --*failure;
        for (unsigned i=0; i<count; ++i) at_queue_free_data(&commands[i]);
        return -1;
    }
    while (athead && after && after->next && after->next->task == after->task) after = after->next;
    ++tasks;
    for (unsigned i=0; i<count; ++i) {
        struct queue_node *node = calloc(1,sizeof(*node));
        if (!node) abort();
        node->command = commands[i];
        node->task = tasks;
        node->first_cmd = commands[0].cmd;
        node->uid = uid;
        if (!node->command.timeout.tv_sec && !node->command.timeout.tv_usec)
            node->command.timeout = (struct timeval){command_timeout_ms/1000, (command_timeout_ms%1000)*1000};
        if (after) {
            node->next = after->next;
            after->next = node;
        } else {
            node->next = pvt->head;
            pvt->head = node;
        }
        if (pvt->tail == after) pvt->tail = node;
        after = node;
    }
    return at_queue_run(pvt);
}
static int at_queue_insert(struct cpvt *cpvt, at_queue_cmd_t *commands, unsigned count, int athead) {
    return at_queue_insert_uid(cpvt, commands, count, athead, 0);
}
/* As upstream: the queue takes copies of static commands. */
static int at_queue_insert_const(struct cpvt *cpvt, const at_queue_cmd_t *commands, unsigned count, int athead) {
    at_queue_cmd_t copies[count];
    memcpy(copies, commands, sizeof(copies));
    return at_queue_insert(cpvt, copies, count, athead);
}
static int at_fill_generic_cmd(at_queue_cmd_t *command, const char *format, ...) {
    va_list args;
    va_start(args,format);
    int count = vasprintf(&command->data,format,args);
    va_end(args);
    command->length = count > 0 ? (unsigned)count : 0;
    command->flags &= ~ATQ_CMD_FLAG_STATIC;
    return count < 0;
}
static const char cmd_at[] = "AT\r";
static int at_enqueue_ping(struct cpvt *cpvt) {
    at_queue_cmd_t command = ATQ_CMD_DECLARE_DYN(CMD_AT);
    if (at_fill_generic_cmd(&command,"AT\r")) return -1;
    return at_queue_insert(cpvt,&command,1,1);
}
static int sms_inbox_set(struct pvt *pvt, unsigned index) {
    if (index >= SMS_INDEX_MAX) return 0;
    pvt->inbox[index] = 1;
    return 1;
}
static void sms_inbox_clear(struct pvt *pvt, unsigned index) { pvt->inbox[index] = 0; }
static int is_sms_inbox_set(struct pvt *pvt, unsigned index) { return pvt->inbox[index]; }
static void pvt_try_restate(struct pvt *pvt) { (void)pvt; }
static int at_cmd_suppress_error_mode(unsigned flags) { return !!(flags & ATQ_CMD_FLAG_SUPPRESS_ERROR); }
static int at_enqueue_retrieve_sms(struct cpvt *, int, at_cmd_suppress_error_t);
static int at_enqueue_delete_sms(struct cpvt *, int);
static void phonelet_storage_select(struct cpvt *, unsigned, int);
static void phonelet_storage_next(struct cpvt *);
static void request_clcc(struct pvt *pvt) { printf("RING\t%d\n",pvt->rings); }
static int at_enqueue_volsync(struct cpvt *cpvt) { (void)cpvt; return 0; }
static void queue_hangup(void *channel, int cause) { (void)channel; (void)cause; }
static ssize_t smsdb_outgoing_part_put(int uid, int ref, char *dst, char *payload) {
    (void)dst; (void)payload;
    printf("CMGS_STORED\t%d\t%d\n", uid, ref);
    return -1;
}
static ssize_t smsdb_outgoing_clear(int uid, char *dst, char *payload) {
    (void)uid; (void)dst; (void)payload; return -1;
}
static void manager_event_message(const char *a, const char *b, const char *c) { (void)a;(void)b;(void)c; }
static void manager_event_message_raw(const char *a, const char *b, const char *c) { (void)a;(void)b;(void)c; }
static ssize_t smsdb_outgoing_part_status(const char *a,const char *b,int c,int d,int *e,char *f) {
    (void)a;(void)b;(void)c;(void)d;(void)e;(void)f;return -1;
}
static void start_local_channel(struct pvt *a,const char *b,const char *c,channel_var_t *d) {
    (void)a;(void)b;(void)c;(void)d;
}
static void manager_event_report(const char *a,const char *b,ssize_t c,const char *d,const char *e,int f,int g,const char *h) {
    (void)a;(void)b;(void)c;(void)d;(void)e;(void)f;(void)g;(void)h;
}
static void handle_expired_reports(struct pvt *pvt) { (void)pvt; }
static void clean_read_data(const char *name, int fd) {
    (void)name;
    tcflush(fd,TCIFLUSH);
}
static int lock_try(const char *dev, char **lockfile) { (void)dev;*lockfile=NULL;return 0; }
/* Upstream opentty sets TIOCEXCL. A real ttyUSB loses it on its last close;
 * the harness keeps its PTY slave open, so release it before closing. */
static void closetty(int fd,char **lockfile) {
    (void)lockfile;
    if(fd<0) return;
    ioctl(fd,TIOCNXCL);
    close(fd);
}
static void disconnect_dongle(struct pvt *pvt) {
    closetty(pvt->data_fd,NULL);
    pvt->data_fd = pvt->audio_fd = -1;
    while(pvt->head) at_queue_remove_cmd(pvt,RES_UNKNOWN);
    printf("DISCONNECT\t%lld\n", monotonic_ms());
}

#include "chan_dongle.h.inc"
#include "at_read.c.inc"
#include "ringbuffer.c.inc"
#include "char_conv.c.inc"
#include "pdu.c.inc"
#include "at_parse.c.inc"
#include "at_queue.c.inc"
#include "at_command.c.inc"
#include "at_response.c.inc"

/* Asterisk dispatch boundary: route the actual byte classifications to the
 * exact receive/RING and complete ERROR handlers. OK acknowledgments mirror
 * at_response_ok for the initialization, ping, storage selection, retrieve
 * and delete subset.
 * Real Asterisk call/channel creation and CDR production are intentionally absent.
 */
int at_response(struct pvt *pvt,const struct iovec *iov,int count,at_res_t result) {
    char line[4096];
    size_t length = 0;
    for(int i=0;i<count;++i) {
        if(length+iov[i].iov_len>=sizeof(line)) return -1;
        memcpy(line+length,iov[i].iov_base,iov[i].iov_len);
        length += iov[i].iov_len;
    }
    /* Match upstream at_response: omit its final CR before decoding PDU. */
    if(length) --length;
    line[length]='\0';
    printf("RESPONSE\t%s\t",at_res2str(result));
    for(size_t i=0;i<length;++i) printf("%02x",(unsigned char)line[i]);
    putchar('\n');
    if(result == RES_RING) return at_response_ring(pvt);
    if(result == RES_BUSY || result == RES_NO_CARRIER || result == RES_NO_DIALTONE) {
        at_response_busy(pvt,result == RES_BUSY ? AST_CONTROL_BUSY : AST_CONTROL_CONGESTION);
        return 0;
    }
    if(result == RES_CPMS) { phonelet_storage_counts(pvt,line); return 0; }
    if(result == RES_CMTI) return at_response_cmti(pvt,line);
    if(result == RES_SMS_PROMPT) return at_response_sms_prompt(pvt);
    if(result == RES_CMGR) return at_response_cmgr(pvt,line,length);
    if(result == RES_CMGS) {
        const at_queue_task_t *task = at_queue_head_task(pvt);
        const at_queue_cmd_t *ecmd = at_queue_task_cmd(task);
        const char *str = line;
        switch (result) {
#include "cmgs.inc"
        default: break;
        }
    }
    if(result == RES_OK) {
        const at_queue_cmd_t *head=at_queue_head_cmd(pvt);
        if(head && (head->res==RES_OK || head->res==RES_CMGR)) {
            if(head->cmd==CMD_AT_CNMI) pvt->initialized=pvt->has_sms=1;
            if(head->cmd==CMD_USER) puts("COMMAND_OK");
            if(head->cmd==CMD_AT_CPMS) phonelet_storage_init(pvt,1);
            if(head->cmd==CMD_AT_CPMS_SELECT) phonelet_storage_selected(pvt,1);
            /* Production advances the inbox before acknowledging the old
             * command, preserving its error-suppression mode for the next. */
            if(head->cmd==CMD_AT_CMGR)
                phonelet_sms_unanswered(pvt,at_cmd_suppress_error_mode(head->flags));
            at_queue_handle_result(pvt,RES_OK);
        }
    } else if(result == RES_CMS_ERROR || result == RES_ERROR) {
        if (at_response_error(pvt,result)) return -1;
    }
    if(!pvt->head && !pvt->ready_announced && pvt->incoming_sms_index==-1U) {
        pvt->ready_announced=1;
        puts("READY");
    }
    return 0;
}

#include "chan_dongle.c.inc"

#define HAS_PREFIX(text, prefix) (!strncmp((text), (prefix), sizeof(prefix) - 1))
#define AFTER_PREFIX(text, prefix) ((text) + sizeof(prefix) - 1)

/* Applies a stdin control line under pvt->lock; 0 when text is not a control. */
static int apply_control(struct pvt *pvt, const char *text) {
    if (HAS_PREFIX(text, "ADVANCE=")) test_time_offset += atoi(AFTER_PREFIX(text, "ADVANCE="));
    else if (HAS_PREFIX(text, "WALL=")) test_wall_offset += atoi(AFTER_PREFIX(text, "WALL="));
    else if (HAS_PREFIX(text, "FAIL_CPMS=")) fail_cpms_select = atoi(AFTER_PREFIX(text, "FAIL_CPMS="));
    else if (HAS_PREFIX(text, "FAIL_CPMS_ME=")) fail_cpms_me = atoi(AFTER_PREFIX(text, "FAIL_CPMS_ME="));
    else if (HAS_PREFIX(text, "FAIL_CMGR=")) fail_cmgr = atoi(AFTER_PREFIX(text, "FAIL_CMGR="));
    else if (HAS_PREFIX(text, "STATE=")) {
        if (sscanf(AFTER_PREFIX(text, "STATE="), "%d,%d,%d,%d,%d,%d,%d", &pvt->chansno, &pvt->ring,
                   &pvt->dialing, &pvt->cwaiting, &pvt->initialized, &pvt->has_sms, &pvt->disablesms) != 7) abort();
    } else return 0;
    printf("ADAPTED\t%s\n", text);
    return 1;
}

struct writer_context { struct pvt *pvt; int stop; };
static void *command_writer(void *arg) {
    struct writer_context *context = arg;
    struct pvt *pvt = context->pvt;
    char *line = NULL;
    size_t capacity = 0;
    for (;;) {
        ast_mutex_lock(&pvt->lock);
        int stop = context->stop;
        ast_mutex_unlock(&pvt->lock);
        if (stop) break;
        struct pollfd input = {STDIN_FILENO, POLLIN, 0};
        if (poll(&input,1,10) <= 0) continue;
        if (getline(&line,&capacity,stdin) < 0) break;
        char *text;
        long timeout = strtol(line,&text,10);
        if (*text++ != '\t' || timeout < 0 || timeout > 10000) abort();
        text[strcspn(text,"\n")] = '\0';
        for (;;) {
            ast_mutex_lock(&pvt->lock);
            stop = context->stop;
            if (!stop) {
                int applied = apply_control(pvt, text);
                if (applied) {
                    ast_mutex_unlock(&pvt->lock);
                    break;
                }
            }
            if (!stop && pvt->data_fd >= 0 && pvt->ready_announced &&
                pvt->idle_wait && !pvt->head) {
                at_queue_cmd_t command = ATQ_CMD_DECLARE_DYN(CMD_USER);
                command.timeout = (struct timeval){timeout/1000,(timeout%1000)*1000};
                if (!strcmp(text, "SEND_SMS")) {
                    if (at_enqueue_pdu(&pvt->sys_chan, "00", 2, 1, 4242)) abort();
                } else if (at_fill_generic_cmd(&command,"%s\r",text) ||
                    at_queue_insert(&pvt->sys_chan,&command,1,0)) abort();
                printf("WRITTEN\t%s\t%lld\n",text,monotonic_ms());
                ast_mutex_unlock(&pvt->lock);
                break;
            }
            ast_mutex_unlock(&pvt->lock);
            if (stop) break;
            usleep(1000);
        }
    }
    free(line);
    return NULL;
}

static int is_pty(const char *path) {
    char resolved[PATH_MAX];
    return realpath(path,resolved) && HAS_PREFIX(resolved, "/dev/pts/");
}
int main(int argc,char **argv) {
    if(argc!=4) return 2;
    setvbuf(stdout,NULL,_IOLBF,0);
    setvbuf(stdin,NULL,_IONBF,0);
    spool_directory=argv[2];
    command_timeout_ms=atoi(argv[3]);
    if(command_timeout_ms<10 || command_timeout_ms>10000) return 2;
    /* Production keeps one pvt across reconnects: the SMS memory state, the
     * inbox and a pending receive retry are carried over. do_monitor_phone
     * itself clears only the memory state; the inbox and retry stay. */
    unsigned mem=0, mem_switch=0, mem_pending=0, mem_failed=0;
    unsigned char inbox[SMS_INDEX_MAX]={0};
    time_t retry_at=0;
    unsigned parse_retry_slot=0;
    char bad_pdu[4096]="";
    for(;;) {
        if(!is_pty(argv[1])) { usleep(10000); continue; }
        struct pvt pvt = {.id="dongle0", .imsi="001010123456789", .data_fd=-1,
            .audio_fd=-1, .incoming_sms_index=-1U, .autodeletesms=1,
            .resetdongle=1, .u2diag=-1, .volume_sync_step=1,
            .phonelet_mem=mem, .phonelet_mem_switch=mem_switch,
            .phonelet_mem_pending=mem_pending, .phonelet_mem_failed=mem_failed,
            .phonelet_retry_at=retry_at, .phonelet_parse_retry_slot=parse_retry_slot};
        memcpy(pvt.phonelet_bad_pdu,bad_pdu,sizeof(bad_pdu));
        memcpy(pvt.inbox,inbox,sizeof(inbox));
        char *lockfile=NULL;
        pvt.sys_chan.pvt=&pvt;
        /* One port: data= is PCUI, where URCs and replies are interleaved. */
        pvt.data_fd=opentty(argv[1],&lockfile);
        if(pvt.data_fd<0) { usleep(10000); continue; }
        pvt.audio_fd=pvt.data_fd; /* voice transport intentionally absent */
        if (pthread_mutex_init(&pvt.lock,NULL)) abort();
        monitor_pvt=&pvt;
        struct writer_context writer = {.pvt=&pvt};
        pthread_t thread;
        if (pthread_create(&thread,NULL,command_writer,&writer)) abort();
        puts("OPEN");
        do_monitor_phone(&pvt);
        mem=pvt.phonelet_mem; mem_switch=pvt.phonelet_mem_switch;
        mem_pending=pvt.phonelet_mem_pending; mem_failed=pvt.phonelet_mem_failed;
        memcpy(inbox,pvt.inbox,sizeof(inbox)); retry_at=pvt.phonelet_retry_at;
        parse_retry_slot=pvt.phonelet_parse_retry_slot;
        memcpy(bad_pdu,pvt.phonelet_bad_pdu,sizeof(bad_pdu));
        ast_mutex_lock(&pvt.lock);
        writer.stop=1;
        ast_mutex_unlock(&pvt.lock);
        pthread_join(thread,NULL);
        pthread_mutex_destroy(&pvt.lock);
        usleep(10000);
    }
}
