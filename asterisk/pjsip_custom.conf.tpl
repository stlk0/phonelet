[transport-udp]
type=transport
protocol=udp
bind=${AST_SIP_BIND}

[phone]
type=endpoint
context=default
message_context=sms-outbound
disallow=all
allow=g722
allow=ulaw
allow=alaw
auth=phone-auth
aors=phone
direct_media=no
rtp_symmetric=yes
rewrite_contact=yes

[phone]
type=aor
max_contacts=1
remove_existing=yes
qualify_frequency=60

[phone-auth]
type=auth
username=${AST_SIP_USERNAME}
password=${AST_SIP_PASSWORD}
