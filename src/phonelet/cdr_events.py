import logging

from phonelet.constants import ANONYMOUS_SENDER, CALL_ANSWERED, CALL_MISSED, CALL_REJECTED
from phonelet.contracts import (
    CallEnded, CallReceiver, CdrRow, MalformedCdr, SmsFailed, SmsFailureReceiver, parse_timestamp,
)

logger = logging.getLogger(__name__)

CDR_DISPOSITION_MAP = {
    "ANSWERED": CALL_ANSWERED,
    "NO ANSWER": CALL_MISSED,
    "BUSY": CALL_REJECTED,
    "FAILED": CALL_MISSED,
}

# report@dongle-incoming keeps one CDR for a failed outgoing SMS and marks it with
# this userfield prefix plus the driver's SMS_REPORT_TYPE; success leaves no CDR.
SMS_REPORT_CHANNEL = "local/report@"
DONGLE_CHANNEL_PREFIX = "dongle/"  # channels of incoming calls
CALL_REJECTED_MARKER = "phonelet-call-rejected"
SMS_REPORT_MARKER = "phonelet-sms-report:"
# Report types of chan_dongle 7db8e75 with the empty report argument of [sms-send]:
# i = the modem answered ERROR to a part (at_response.c), e = a failed delivery report
# (none is requested, so not expected), t = the driver's record of the SMS outlived its
# validity (3 days) without a send result (chan_dongle.c): its queue was flushed on a
# command deadline, disconnect or restart, or the send reply was lost. Phonelet's own:
# r = the driver refused the SMS (or the number is not digits, or no driver is loaded), so
# no report comes: [sms-send] started report@ itself with Originate(), one Local pair and
# uniqueid per SMS. A refusal while queueing a later part leaves the earlier ones queued,
# so such an SMS may still go out or get an i or t as well. All of them alert.
SMS_REPORT_TYPES = ("i", "e", "t", "r")


class CdrHandler:
    """Turns Asterisk CDR records into call and failed-SMS callbacks.

    The CDR journal replay (ingress.IngressReplay) calls handle_cdr with each row.
    """

    def __init__(
        self,
        on_call_end: CallReceiver,
        on_sms_failed: SmsFailureReceiver,
    ):
        self.on_call_end = on_call_end
        self.on_sms_failed = on_sms_failed

    async def handle_cdr(self, row: CdrRow, *, history=False) -> None:
        channel = row.get("Channel", "").lower()
        if channel.startswith(SMS_REPORT_CHANNEL):
            await self._handle_sms_report(row, history=history)
            return
        if not channel.startswith(DONGLE_CHANNEL_PREFIX):
            return  # only incoming calls on dongle channels

        # A call that reached Dial has two rows, Dial then Hangup; the Dial row comes first and
        # wins in the database by source_id, so its disposition and userfield count. A call that
        # never entered Dial (the phone has no contact, the caller hung up before it) has only
        # the Hangup row, which keeps it in history.

        source = row.get("Source", "")
        status = CDR_DISPOSITION_MAP.get(row.get("Disposition"), CALL_MISSED)
        userfield = row.get("UserField", "")
        _, separator, marker = userfield.rpartition(";")
        if separator and marker == CALL_REJECTED_MARKER:
            status = CALL_REJECTED
        recording_path = userfield.partition(";")[0]

        unique_id = row.get("UniqueID")
        start = row.get("StartTime")
        if not unique_id or not start:
            raise MalformedCdr("CDR missing source ID or start time")
        try:
            duration = int(row.get("BillableSeconds", "0"))
            timestamp = parse_timestamp(start)
        except ValueError as error:
            raise MalformedCdr(f"CDR duration or start time invalid ({type(error).__name__})") from None
        parsed: CallEnded = dict(
            source_id="cdr:" + unique_id,
            timestamp=timestamp,
            caller=source or ANONYMOUS_SENDER,
            status=status,
            duration=duration,
            recording_path=recording_path if (recording_path and status == CALL_ANSWERED) else None,
        )
        if history:
            parsed["history"] = True
        await self.on_call_end(**parsed)

    async def _handle_sms_report(self, row: CdrRow, *, history=False) -> None:
        userfield = row.get("UserField", "")
        if not userfield.startswith(SMS_REPORT_MARKER):
            return  # lines from before the marker, or the other half when EXPORT() is missing
        unique_id = row.get("UniqueID")
        if not unique_id:
            raise MalformedCdr("CDR missing source ID")
        reason = userfield[len(SMS_REPORT_MARKER):]
        reason = reason if reason in SMS_REPORT_TYPES else "unknown"
        parsed: SmsFailed = dict(event_key="sms-report:" + unique_id,
                                 recipient=row.get("Source") or "unknown", reason=reason)
        if history:
            parsed["history"] = True
        # Only the report type is logged, never the number. A restart replays the retained CDR
        # journal: an alert already queued is only INFO. A failed enqueue raises to the replay.
        try:
            new = await self.on_sms_failed(**parsed)
        except Exception:
            logger.warning("Outgoing SMS failed: reason=%s; alert not queued yet", reason)
            raise
        if history:
            logger.info("Outgoing SMS failure retained as history: reason=%s", reason)
        elif new:
            logger.warning("Outgoing SMS failed: reason=%s", reason)
        else:
            logger.info("Outgoing SMS failed: reason=%s; alert already queued", reason)
