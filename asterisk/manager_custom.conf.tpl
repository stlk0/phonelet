[phonelet]
; Action replies bypass event filtering; Phonelet needs no unsolicited events.
read=
write=reporting,message
secret=${AST_AMI_SECRET}
deny=0.0.0.0/0.0.0.0
permit=127.0.0.1/255.255.255.255
