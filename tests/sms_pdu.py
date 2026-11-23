"""Synthetic SMS-DELIVER PDUs for the offline modem integration tests.

Only the UCS-2 alphabet is generated. Decoding, UDH interpretation and journal
production are deliberately left to the pinned driver's real C parser.
"""
from datetime import datetime

TP_UDHI = 0x40  # first octet: user data starts with a header
INTERNATIONAL, UNKNOWN_TYPE = 0x91, 0x81  # type of address
CONCAT_8BIT, CONCAT_16BIT = 0, 8  # UDH information element ids
MAX_USER_DATA = 140  # octets in one TPDU


def _semi_octets(digits: str) -> bytes:
    digits += "F" if len(digits) % 2 else ""
    return bytes.fromhex("".join(digits[i + 1] + digits[i]
                                 for i in range(0, len(digits), 2)))


def deliver_pdu(
    text: str,
    *,
    sender: str = "+15551234567",
    stamp: str = "2026-10-17T12:00:00",
    ref: int | None = None,
    total: int = 1,
    part: int = 1,
    wide_ref: bool = False,
) -> str:
    timestamp = datetime.fromisoformat(stamp)
    scts = _semi_octets(timestamp.strftime("%y%m%d%H%M%S") + "00")
    udh = b""
    if ref is not None:
        if wide_ref:
            udh = bytes((6, CONCAT_16BIT, 4, ref >> 8, ref & 255, total, part))
        else:
            udh = bytes((5, CONCAT_8BIT, 3, ref, total, part))
    body = udh + text.encode("utf-16-be")
    if len(body) > MAX_USER_DATA:
        raise ValueError("Test SMS exceeds one TPDU")
    digits = sender.lstrip("+")
    # Received PDUs include the SMSC, unlike outgoing PDUs which may leave it
    # unspecified. Use a synthetic international SMSC with a valid BCD address.
    smsc = bytes.fromhex("07915155214365F7")
    packet = (
        smsc + bytes((TP_UDHI if udh else 0, len(digits), INTERNATIONAL if sender.startswith("+") else UNKNOWN_TYPE))
        + _semi_octets(digits)
        + bytes((0, 8))  # protocol identifier, UCS-2 data coding scheme
        + scts
        + bytes((len(body),))
        + body
    )
    return packet.hex().upper()
