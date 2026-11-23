[dongle0]
; Use PCUI (USB interface 02) for data: AT replies and unsolicited modem indications share it.
data=${AST_DONGLE_DATA}
audio=${AST_DONGLE_AUDIO}
context=dongle-incoming
; Gains are sample multipliers, not dB: 3 means x3 (+9.5 dB) and can clip.
; Values from -1 to 1 leave samples unchanged; values below -1 divide by their absolute value.
rxgain=${AST_DONGLE_RXGAIN}
txgain=${AST_DONGLE_TXGAIN}
autodeletesms=yes
