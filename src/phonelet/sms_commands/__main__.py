#!/usr/bin/env python3
"""Owner's tool for command SMS (docs/sms-commands.md, "Commands by encrypted SMS").

  PYTHONPATH=src .venv/bin/python -m phonelet.sms_commands key
  PYTHONPATH=src .venv/bin/python -m phonelet.sms_commands wifi --ssid SSID [--open]

Secrets are asked with getpass, never taken as arguments: arguments stay in the
shell history and the process list.
"""
import argparse
import getpass
import sys
import time
import warnings

from phonelet.sms_commands import envelope
from phonelet.sms_commands.commands import wifi


def read_secret(prompt):
    """getpass, without its fallback to input that the terminal may echo."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        sys.exit("A terminal with hidden input is required")


def ask_twice(prompt):
    first = read_secret(prompt + ": ")
    if read_secret(prompt + " again: ") != first:
        sys.exit("The two entries differ")
    return first


def pack_wifi(args):
    password = "" if args.open else ask_twice("Wi-Fi password")
    if not args.open and not password:
        sys.exit("Empty Wi-Fi password: use --open for an open network")
    return wifi.CODE, wifi.pack_args(args.ssid, password)


def print_key():
    passphrase = ask_twice("Passphrase")
    if len(passphrase) < envelope.MIN_PASSPHRASE_LENGTH:
        sys.exit(f"Passphrase shorter than {envelope.MIN_PASSPHRASE_LENGTH} characters: use 5 or more "
                 f"random words or {envelope.MIN_PASSPHRASE_LENGTH} or more random characters")
    key = envelope.derive_key(passphrase)
    print("PHONELET_SMS_COMMAND_KEY=" + envelope.format_key(key))
    print("Key fingerprint: " + envelope.key_fingerprint(key), file=sys.stderr)


def print_sms(code, packed):
    key = envelope.derive_key(read_secret("Passphrase: "))
    text = envelope.encode(key, code, packed, int(time.time() * 1000))
    print(text)
    print(f"{len(text)} characters, {envelope.sms_parts(text)} SMS part(s); "
          "send it within 24 hours, a new one for each try", file=sys.stderr)
    # Must equal the one `key` printed: else the passphrase was mistyped and the board rejects the SMS.
    print("Key fingerprint: " + envelope.key_fingerprint(key), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(prog="python -m phonelet.sms_commands", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("key", help="derive PHONELET_SMS_COMMAND_KEY from the passphrase")
    wifi_parser = subparsers.add_parser("wifi", help="print the SMS text that moves the board to a Wi-Fi network")
    wifi_parser.add_argument("--ssid", required=True)
    wifi_parser.add_argument("--open", action="store_true", help="open network, no password")
    wifi_parser.set_defaults(pack=pack_wifi)
    args = parser.parse_args()
    try:
        if args.command == "key":
            print_key()
        else:
            print_sms(*args.pack(args))  # pack checks the arguments before the slow key derivation
    except UnicodeError:  # its text quotes the offending byte of the input
        sys.exit("Input is not valid UTF-8")
    except ValueError as error:  # the codecs' own messages name the field, never its value
        sys.exit(str(error))


if __name__ == "__main__":
    main()
