"""Command SMS envelope, contract v1: pure encoding and decoding, no I/O.

Text: PREFIX + base64url without '=' of VERSION | nonce | AES-256-GCM(key, nonce,
plaintext, AAD), the 16-byte tag at the end. Plaintext: stamp (6 bytes big-endian,
ms of Unix time on the sender's phone) | command code (1 byte) | arguments (the
command's own format, may be empty; see its module in commands/). The code is
encrypted too: history and the SMSC see only that a command was sent. The key is
PBKDF2-HMAC-SHA256 of the owner's passphrase; the board keeps only the key. The
core script of tools/sms-command.html implements the same encoder in
WebCrypto: change both together.
"""
import base64
import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "PC1 "
VERSION = 1
AAD = b"PC1" + bytes([VERSION])
KDF_SALT = b"phonelet-sms-command-v1"  # fixed: the passphrase alone must reproduce the key
KDF_ITERATIONS = 600_000
KEY_BYTES = 32
FINGERPRINT_BYTES = 4
# The ciphertext can be brute-forced offline from any one SMS: the CLI refuses shorter phrases.
MIN_PASSPHRASE_LENGTH = 16
NONCE_BYTES = 12
TAG_BYTES = 16
STAMP_BYTES = 6
CODE_BYTES = 1
MAX_CODE = 255
# GSM 7-bit: one SMS holds 160 characters, a concatenated part 153.
# Base64url and the space are all in the basic alphabet.
SMS_SINGLE_CHARS, SMS_PART_CHARS = 160, 153


class RejectionReason(StrEnum):
    """Why a command SMS is rejected: stored as "rejected:<reason>"."""
    DISABLED = "disabled"  # no key set: commands are off
    BAD_FORMAT = "bad_format"
    AUTH_FAILED = "auth_failed"
    REPLAY = "replay"
    FUTURE = "future"
    STALE = "stale"
    UNKNOWN_COMMAND = "unknown_command"  # authentic, but this release has no command with its code
    CHECK_ERROR = "error"


class DecodeError(ValueError):
    """The text is not an authentic v1 command SMS; `reason` (BAD_FORMAT or AUTH_FAILED) is its only detail."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Envelope:
    stamp_ms: int
    code: int
    packed_args: bytes = field(repr=False)  # may hold secrets (a Wi-Fi password)


def _b64encode(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text):
    """Canonical base64url without '=' only: the text must re-encode to itself."""
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))  # ValueError on bad input
    if _b64encode(data) != text:
        raise ValueError("Non-canonical base64url")
    return data


def derive_key(passphrase):
    if not passphrase:
        raise ValueError("Empty passphrase")
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), KDF_SALT, KDF_ITERATIONS, KEY_BYTES)


def format_key(key):
    """The PHONELET_SMS_COMMAND_KEY value: base64url without '='."""
    return _b64encode(key)


def key_fingerprint(key):
    """First bytes of SHA-256(key) as hex: compares keys without showing them (catches a mistyped phrase)."""
    return hashlib.sha256(key).digest()[:FINGERPRINT_BYTES].hex()


def parse_key(text):
    """Key bytes of a PHONELET_SMS_COMMAND_KEY value; ValueError without the value in it."""
    try:
        key = _b64decode(text)
    except ValueError:
        key = b""
    if len(key) != KEY_BYTES:
        raise ValueError(f"must be base64url without '=' of exactly {KEY_BYTES} bytes")
    return key


def encode(key, code, packed_args, stamp_ms, nonce=None):
    """The SMS text of command `code` with its packed arguments."""
    if not 0 <= code <= MAX_CODE:
        raise ValueError("Command code out of range")
    if not 0 <= stamp_ms < 1 << (8 * STAMP_BYTES):
        raise ValueError("Stamp out of range")
    nonce = os.urandom(NONCE_BYTES) if nonce is None else nonce
    if len(nonce) != NONCE_BYTES:
        raise ValueError("Nonce must be 12 bytes")
    plaintext = stamp_ms.to_bytes(STAMP_BYTES, "big") + bytes([code]) + bytes(packed_args)
    sealed = AESGCM(key).encrypt(nonce, plaintext, AAD)
    return PREFIX + _b64encode(bytes([VERSION]) + nonce + sealed)


def is_command(text):
    return text.strip().startswith(PREFIX)


_CONTINUATION = re.compile(r"[A-Za-z0-9_-]+")


def is_continuation(text):
    """Whether a part can continue a command: base64url only (the prefix is in part 1)."""
    return _CONTINUATION.fullmatch(text.strip()) is not None


def decode(key, text):
    """Return the Envelope; DecodeError(BAD_FORMAT or AUTH_FAILED) otherwise.

    The arguments are not checked here: that is the command's parse.
    """
    text = text.strip()
    if not text.startswith(PREFIX):
        raise DecodeError(RejectionReason.BAD_FORMAT)
    try:
        data = _b64decode(text[len(PREFIX):])
    except ValueError:
        raise DecodeError(RejectionReason.BAD_FORMAT) from None
    version, nonce, sealed = data[:1], data[1:1 + NONCE_BYTES], data[1 + NONCE_BYTES:]
    if version != bytes([VERSION]) or len(sealed) < STAMP_BYTES + CODE_BYTES + TAG_BYTES:
        raise DecodeError(RejectionReason.BAD_FORMAT)
    try:
        plaintext = AESGCM(key).decrypt(nonce, sealed, AAD)
    except InvalidTag:
        raise DecodeError(RejectionReason.AUTH_FAILED) from None
    return Envelope(stamp_ms=int.from_bytes(plaintext[:STAMP_BYTES], "big"), code=plaintext[STAMP_BYTES],
                    packed_args=plaintext[STAMP_BYTES + CODE_BYTES:])


def sms_parts(text):
    """Number of GSM 7-bit SMS parts for a text in the basic alphabet."""
    return 1 if len(text) <= SMS_SINGLE_CHARS else math.ceil(len(text) / SMS_PART_CHARS)
