[sub-prepare-recording]
; ARG1 reaches filenames and shell commands: callers must limit it to digits, "+" or "anonymous".
exten => s,1,Set(RECORDING_PATH=)
 same => n,GotoIf($["${AST_RECORD_CALLS}" != "yes"]?done)
 same => n,TrySystem(/usr/local/lib/phonelet/convert-recording.py --check-space --root ${AST_RECORDINGS_DIR} --min-free-bytes ${AST_MIN_FREE_BYTES})
 same => n,GotoIf($["${SYSTEMSTATUS}" != "SUCCESS"]?done)
 same => n,Set(RECORDING_DIR=${AST_RECORDINGS_DIR}/${STRFTIME(${EPOCH},,%Y/%m/%d)})
 same => n,Set(RECORDING_FILE=${STRFTIME(${EPOCH},,%Y%m%d-%H%M%S)}-${UNIQUEID}-${ARG1})
 same => n,TrySystem(mkdir -p ${RECORDING_DIR})
 same => n,GotoIf($["${SYSTEMSTATUS}" != "SUCCESS"]?done)
 same => n,Set(RECORDING_PATH=${RECORDING_DIR}/${RECORDING_FILE})
 same => n,Set(CDR(userfield)=${RECORDING_PATH}.mp3)
 same => n(done),Return()

[sub-dial-outgoing]
; Dial() interprets commas and "&", and the driver passes DIALNUM to ATD unchecked.
; Accept only digits with an optional leading "+" to prevent option or command injection.
exten => s,1,GotoIf(${REGEX("^[+]?[0-9]+$" ${DIALNUM})}?:rejected)
 same => n,Dial(Dongle/dongle0/${DIALNUM},60)
 same => n,Return()
 same => n(rejected),Log(NOTICE,Outgoing call rejected: not a valid number)
 same => n,Hangup(28)

[sub-start-recording]
; Run on the answered callee via Dial's U() so unanswered calls produce no recording.
exten => s,1,GotoIf($["${ARG1}" = ""]?done)
 same => n,MixMonitor(${ARG1}.wav,b,/usr/local/lib/phonelet/convert-recording.py --root ${AST_RECORDINGS_DIR} --min-free-bytes ${AST_MIN_FREE_BYTES} ${ARG1}.wav)
 same => n(done),Return()

[sub-prepare-incoming-call]
exten => s,1,Set(CHANNEL(hangup_handler_push)=sub-incoming-hangup,s,1)
 same => n,Return()

[sub-incoming-hangup]
; Mark phone declines in the callee's CDR while it still belongs to this Dial.
; Check before local CANCEL so a timeout's later 487 cannot count as a decline.
; Do not move the Set after Dial: that creates a new caller-side CDR.
exten => s,1,ExecIf(${REGEX("^SIP (480|486|600|603) " ${HANGUPCAUSE(${CHANNEL},tech)})}?Set(CDR(userfield)=phonelet-call-rejected))
 same => n,Return()

[dongle-incoming]
; Disable the other Local half by name: CDR_PROP only affects this half (;1 for the driver,
; ;2 for Originate). Keep one marked failure CDR with caller ID as recipient.
; Types: i = send result, e = delivery report, t = report timeout, r = rejected before queue.
; Never log the number, text or SMS_REPORT; only type and success are safe.
exten => report,1,Log(NOTICE,Outgoing SMS report type=${SMS_REPORT_TYPE} success=${SMS_REPORT_SUCCESS})
 same => n,Set(EXPORT(${CHANNEL:0:-1}${IF($["${CHANNEL:-1}" = "1"]?2:1)},CDR_PROP(disable))=1)
 same => n,GotoIf($["${SMS_REPORT_SUCCESS}" != "1"]?failed)
 same => n,Set(CDR_PROP(disable)=1)
 same => n,Hangup()
 same => n(failed),Set(CDR(userfield)=phonelet-sms-report:${SMS_REPORT_TYPE})
 same => n,Hangup()

; Discard USSD without logging its contents or leaving private data in either half's CDR.
exten => ussd,1,Set(EXPORT(${CHANNEL:0:-1}${IF($["${CHANNEL:-1}" = "1"]?2:1)},CDR_PROP(disable))=1)
 same => n,Set(CDR_PROP(disable)=1)
 same => n,Hangup()

exten => _+X.,1,Goto(s,1)
exten => _X.,1,Goto(s,1)
exten => s,1,Set(SAFE_CALLER=${FILTER(0-9+,${CALLERID(num)})})
 same => n,Set(SAFE_CALLER=${IF($["${SAFE_CALLER}" = ""]?anonymous:${SAFE_CALLER})})
 same => n,Set(CALLERID(name)=${SAFE_CALLER})
 same => n,Gosub(sub-prepare-recording,s,1(${SAFE_CALLER}))
 same => n,Dial(PJSIP/phone,30,b(sub-prepare-incoming-call^s^1)U(sub-start-recording^${RECORDING_PATH}))
 same => n,Hangup()

[sms-outbound]
; Keep number normalization consistent with [default].
exten => _+X.,1,Set(SMS_TO=${EXTEN})
 same => n,Goto(sms-send,s,1)

exten => _00XXX.,1,Set(SMS_TO=+${EXTEN:2})
 same => n,Goto(sms-send,s,1)

exten => _${AST_TRUNK_PREFIX}XXXX.,1,Set(SMS_TO=${AST_COUNTRY_PREFIX}${EXTEN:1})
 same => n,Goto(sms-send,s,1)

exten => _X,1,Set(SMS_TO=${EXTEN})
 same => n,Goto(sms-send,s,1)

exten => _X.,1,Set(SMS_TO=${IF($[${LEN(${EXTEN})} > 5]?+)}${EXTEN})
 same => n,Goto(sms-send,s,1)

[sms-send]
; TryExec() expands arguments twice: validate SMS_TO and pass text as Base64 to prevent injection.
; TRYSTATUS: SUCCESS = queued; FAILED = rejected (earlier multipart parts may remain queued);
; NOAPP = missing driver; BADNUMBER = rejected by this dialplan before TryExec().
; Rejections have no driver report, so Originate a type r failure for report@.
; Never log the number or text. If Originate fails, only the error log remains; no alert.
exten => s,1,Set(TRYSTATUS=BADNUMBER)
 same => n,GotoIf(${REGEX("^[+]?[0-9]+$" ${SMS_TO})}?:rejected)
 same => n,TryExec(DongleSendSMSBase64(dongle0,${SMS_TO},${BASE64_ENCODE(${MESSAGE(body)})},,,1))
 same => n,GotoIf($["${TRYSTATUS}" = "SUCCESS"]?done)
 same => n(rejected),Log(NOTICE,Outgoing SMS rejected before the driver queue status=${TRYSTATUS})
 same => n,Originate(Local/report@dongle-incoming,app,Wait,1,,,ac(${FILTER(0-9+,${SMS_TO})})v(SMS_REPORT_TYPE=r^SMS_REPORT_SUCCESS=0))
 same => n,ExecIf($["${ORIGINATE_STATUS}" != "SUCCESS"]?Log(ERROR,No alert for the rejected outgoing SMS status=${ORIGINATE_STATUS}))
 same => n(done),Hangup()

[default]
; Asterisk takes the most specific pattern, not the first one in the file.
exten => _+X.,1,Set(DIALNUM=${EXTEN})
 same => n,Gosub(sub-dial-outgoing,s,1)
 same => n,Hangup()

; With trunk prefix 0 this outranks the local rule: its second 0 is a literal, not X.
exten => _00XXX.,1,Set(DIALNUM=+${EXTEN:2})
 same => n,Gosub(sub-dial-outgoing,s,1)
 same => n,Hangup()

; Deploy omits this rule and its SMS counterpart when AST_TRUNK_PREFIX is empty.
exten => _${AST_TRUNK_PREFIX}XXXX.,1,Set(DIALNUM=${AST_COUNTRY_PREFIX}${EXTEN:1})
 same => n,Gosub(sub-dial-outgoing,s,1)
 same => n,Hangup()

; Preserve short service numbers: no full number with a country code is five digits or fewer.
; _X. needs at least two digits, so a separate _X rule handles single digits in calls and SMS.
exten => _X,1,Set(DIALNUM=${EXTEN})
 same => n,Gosub(sub-dial-outgoing,s,1)
 same => n,Hangup()

exten => _X.,1,Set(DIALNUM=${IF($[${LEN(${EXTEN})} > 5]?+)}${EXTEN})
 same => n,Gosub(sub-dial-outgoing,s,1)
 same => n,Hangup()
