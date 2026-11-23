"""Command SMS: the envelope, the page's encoder, reception, the worker and its loop, and the CLI."""
import asyncio
import base64
import getpass
import json
import logging
import os
import re
import runpy
import shutil
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from phonelet.config import Settings
from phonelet.db import Database, SMS_COMMAND_INCOMPLETE_TEXT, SMS_COMMAND_TEXT
from phonelet.cdr_events import CdrHandler
from phonelet.ingress import IngressReplay, parse_fragment
from phonelet.notification import deliver_pending
from phonelet.sms_commands import envelope
from phonelet.sms_commands.commands import wifi
from phonelet.sms_commands.commands.base import Command
from phonelet.sms_commands.worker import by_code, command_loop, process_commands
from tests.helpers import (StopLoop, command_rows, health_by_event, health_messages, journal_record,
                           stop_after)

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'tools' / 'sms-command.html'
# Invented values only.
PASSPHRASE = 'example passphrase, not real'
KEY = envelope.derive_key(PASSPHRASE)
OTHER_KEY = envelope.derive_key('another invented phrase')
KEY_TEXT = '3ujeaQY4cTEQCpRNHe9PPImoEllAF2H2-CMxW8bnL3k'
FINGERPRINT = 'b4233f2d'  # sha256(key)[:4]
SSID, PASSWORD = 'Example-Net', 'example-pass-123'
STAMP = 1_790_000_000_000  # ms; 2026-09-21 14:13:20 UTC
NONCE = bytes(range(12))
VECTOR = 'PC1 AQABAgMEBQYHCAkKC87cTWAW_JbaNDEjNfHbpqES7TeH6lfW7MrmP4EvOSKaHsuCjsK5XXfSXaqSqyha3rnTiQ'
ECHO_CODE = 200  # used by no real command
ECHO_VECTOR = 'PC1 AQABAgMEBQYHCAkKC87cTWAW_F_o-vZRNNVMFRxkLCC-XihH'  # echo_sms(STAMP, NONCE)
SCTS = '2026-09-21 14:13:00 +00:00'  # the SMSC stamp, 20 s before the phone's
BACKENDS = ('telegram', 'homeassistant', 'sip')
RECEIVED = 1_790_000_000.0  # synthetic receipt clock


def board_clock():
    """The board's clock for the worker (s): when the test SMS arrive, not today."""
    return RECEIVED


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def unb64(text):
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def sealed(plaintext, version=1):
    """An authentic envelope envelope.encode cannot make: another version byte, or a plaintext too short for a code."""
    aad = b'PC1' + bytes([version])
    return 'PC1 ' + b64(bytes([version]) + NONCE + AESGCM(KEY).encrypt(NONCE, plaintext, aad))


def wifi_sms(ssid, password, stamp, nonce=None, key=KEY):
    """The SMS text of a Wi-Fi command, as the CLI makes it."""
    return envelope.encode(key, wifi.CODE, wifi.pack_args(ssid, password), stamp, nonce)


def echo_sms(stamp, nonce=None, code=ECHO_CODE):
    """The SMS text of an EchoCommand (or, with its code, a SecondEcho)."""
    return envelope.encode(KEY, code, b'', stamp, nonce)


LONG = wifi_sms('Ж' * 16, '~' * 63, STAMP, NONCE)  # the longest Wi-Fi command: 32 bytes of SSID, 63 of password


@pytest_asyncio.fixture
async def db(tmp_db_path):
    database = Database(tmp_db_path, sms_commands_enabled=True)
    await database.init()
    yield database
    await database.close()


# --- envelope --------------------------------------------------------------------------------

def test_fixed_vectors():
    assert envelope.format_key(KEY) == KEY_TEXT
    assert wifi_sms(SSID, PASSWORD, STAMP, NONCE) == VECTOR
    assert envelope.decode(KEY, VECTOR) == envelope.Envelope(
        STAMP, wifi.CODE, bytes([len(SSID)]) + SSID.encode() + PASSWORD.encode())
    assert echo_sms(STAMP, NONCE) == ECHO_VECTOR
    assert envelope.decode(KEY, ECHO_VECTOR) == envelope.Envelope(STAMP, ECHO_CODE, b'')


def test_decode_ignores_surrounding_whitespace():
    assert envelope.decode(KEY, '  ' + VECTOR + '\n') == envelope.decode(KEY, VECTOR)


@pytest.mark.parametrize('stamp', [0, 2 ** 48 - 1], ids=['zero', 'largest-48-bit'])
def test_extreme_stamps_round_trip(stamp):
    assert envelope.decode(KEY, echo_sms(stamp)).stamp_ms == stamp


def test_each_encoding_draws_a_new_nonce():
    assert wifi_sms(SSID, PASSWORD, STAMP) != wifi_sms(SSID, PASSWORD, STAMP)


def test_envelope_repr_hides_the_arguments():
    assert PASSWORD not in repr(envelope.decode(KEY, VECTOR))


def test_wrong_key_or_any_changed_byte_fails_authentication():
    with pytest.raises(envelope.DecodeError) as caught:
        envelope.decode(OTHER_KEY, VECTOR)
    assert caught.value.reason == 'auth_failed' and str(caught.value) == 'auth_failed'
    data = unb64(VECTOR[4:])
    for index in range(1, len(data)):  # nonce, ciphertext and tag; byte 0 is the version
        damaged = bytearray(data)
        damaged[index] ^= 0x01
        with pytest.raises(envelope.DecodeError) as caught:
            envelope.decode(KEY, 'PC1 ' + b64(bytes(damaged)))
        assert caught.value.reason == 'auth_failed'


@pytest.mark.parametrize('text', [
    pytest.param('PC1', id='prefix-only'),
    pytest.param('PC1 ', id='empty-body'),
    pytest.param('pc1 ' + VECTOR[4:], id='lower-case-prefix'),
    pytest.param('PC2 ' + VECTOR[4:], id='other-prefix'),
    pytest.param('PC1  ' + VECTOR[4:], id='two-spaces'),
    pytest.param(VECTOR + '=', id='padding'),
    pytest.param(VECTOR[:-1] + '+', id='not-url-alphabet'),
    pytest.param(VECTOR[:5], id='one-character-body'),
    pytest.param(VECTOR[:40], id='too-short'),
    pytest.param(VECTOR + 'A' * ((1 - len(VECTOR[4:])) % 4 or 4), id='impossible-length'),
    pytest.param(sealed(STAMP.to_bytes(6, 'big') + bytes([wifi.CODE]), version=2), id='version-2'),
    pytest.param(sealed(STAMP.to_bytes(6, 'big')), id='authentic-but-no-command-code'),
    pytest.param('PC1 жжжж', id='non-ascii-body'),
])
def test_malformed_envelopes_are_bad_format(text):
    with pytest.raises(envelope.DecodeError) as caught:
        envelope.decode(KEY, text)
    assert caught.value.reason == 'bad_format'
    # Nothing chained: a rejection carries only its reason.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_non_canonical_base64url_is_bad_format():
    # A body whose last character carries unused bits: setting one keeps the bytes, not the text.
    text = next(t for t in (envelope.encode(KEY, ECHO_CODE, b'n' * n, STAMP, NONCE) for n in range(1, 4))
                if len(t[4:]) % 4 == 2)
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
    sloppy = text[:-1] + alphabet[alphabet.index(text[-1]) | 1]
    assert sloppy != text and unb64(sloppy[4:]) == unb64(text[4:])
    assert envelope.decode(KEY, text).packed_args in (b'n', b'nn', b'nnn')
    with pytest.raises(envelope.DecodeError) as caught:
        envelope.decode(KEY, sloppy)
    assert caught.value.reason == 'bad_format'


@pytest.mark.parametrize('code,stamp,nonce', [(-1, STAMP, NONCE), (256, STAMP, NONCE), (1, -1, NONCE),
                                              (1, 2 ** 48, NONCE), (1, STAMP, bytes(13))],
                         ids=['code-negative', 'code-256', 'stamp-negative', 'stamp-49-bits', 'nonce-13-bytes'])
def test_encoder_rejects_out_of_range_values(code, stamp, nonce):
    with pytest.raises(ValueError):
        envelope.encode(KEY, code, b'', stamp, nonce)


def test_sms_parts_counts_gsm_7_bit_parts():
    assert [envelope.sms_parts('x' * n) for n in (160, 161, 306, 307)] == [1, 2, 2, 3]
    # Base64url and the space are in the GSM 7-bit basic alphabet (no escape characters).
    assert re.fullmatch(r'PC1 [A-Za-z0-9_-]+', LONG)


def test_key_setting_takes_a_key_or_nothing():
    assert Settings(sms_command_key=KEY_TEXT).sms_command_key == KEY_TEXT
    assert Settings().sms_command_key == ''


@pytest.mark.parametrize('bad', [KEY_TEXT + '=', KEY_TEXT[:-1], KEY_TEXT + 'AAAA', KEY_TEXT[:-1] + '+',
                                 KEY_TEXT[:-1] + 'l', 'not a key at all, invented'],
                         ids=['padding', '31-bytes', '35-bytes', 'not-url-alphabet', 'non-canonical', 'not-base64'])
def test_bad_key_setting_is_refused_without_its_value(bad):
    with pytest.raises(ValueError) as caught:
        Settings(sms_command_key=bad)
    assert 'PHONELET_SMS_COMMAND_KEY' in str(caught.value) and bad not in str(caught.value)


# --- the page --------------------------------------------------------------------------------

def page_core():
    match = re.search(r'<script id="sms-command-core">(.*?)</script>', PAGE.read_text(), re.S)
    assert match
    return match.group(1)


def test_page_is_offline_and_stores_nothing():
    page = PAGE.read_text()
    assert ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; form-action \'none\'; '
            'base-uri \'none\'">') in page
    assert not re.search(r'localStorage|sessionStorage|indexedDB|document\.cookie|fetch\(|XMLHttpRequest', page)
    assert not re.search(r'<[a-z]+[^>]*\s(src|href)=', page)  # no external resources


def test_page_has_a_form_per_command():
    page = PAGE.read_text()
    options = re.findall(r'<option value="([a-z]+)">', page)
    assert options == ['wifi']
    for name in options:
        assert f'<fieldset data-command="{name}">' in page and re.search(rf'^\s+{name}: \{{$', page, re.M)


def test_page_takes_the_ssid_as_typed_and_links_both_phone_platforms():
    page = PAGE.read_text()
    ssid_input = re.search(r'<input id="ssid"[^>]*>', page).group(0)
    for attribute in ('autocapitalize="off"', 'autocorrect="off"', 'spellcheck="false"', 'autocomplete="off"'):
        assert attribute in ssid_input
    assert '"?&body="' in page  # Android reads ?body=, iOS &body=
    assert 'Date.now()' in page  # the phone's clock is the stamp
    # Any edit hides the text made from the earlier input, also one made while the key derives (a text pin).
    assert '\n$("form").addEventListener("input", () => { inputs += 1; $("result").hidden = true; });' in page
    assert '\n    $("result").hidden = madeFrom !== inputs;' in page


# What the board rejects (test_wifi_command): the page must refuse it too, else the SMS ends as bad_format.
BAD_NETWORKS = [
    pytest.param('', PASSWORD, 'SSID', id='empty-ssid'),
    pytest.param('x' * 33, PASSWORD, 'SSID', id='ssid-33-bytes'),
    pytest.param('Ж' * 17, PASSWORD, 'SSID', id='ssid-34-utf8-bytes'),
    pytest.param(SSID, 'short12', 'Password', id='password-7'),
    pytest.param(SSID, 'x' * 64, 'Password', id='password-64'),
    pytest.param(SSID, 'пароль-пароль', 'Password', id='password-not-ascii'),
    pytest.param(SSID, 'tab\tinside', 'Password', id='password-control-char'),
]
BAD_CODES = [pytest.param(-1, id='negative'), pytest.param(256, id='256'), pytest.param(1.5, id='fraction')]
NODE_CASES = [
    pytest.param(dict(passphrase=PASSPHRASE, ssid=SSID, password=PASSWORD, stamp=STAMP, nonce=list(NONCE)),
                 id='vector'),
    pytest.param(dict(passphrase='фраза "с" \\ кавычками', ssid='Кафе "x\\y": #1', password='pa"ss\\word: #',
                      stamp=2 ** 48 - 1, nonce=[255] * 12), id='quotes-and-largest-stamp'),
    pytest.param(dict(passphrase='p', ssid='😀' * 8, password='', stamp=0, nonce=list(range(100, 112))),
                 id='emoji-open-network'),
    pytest.param(dict(passphrase='p', ssid='Ж' * 16, password='~' * 63, stamp=STAMP + 1, nonce=list(NONCE)),
                 id='longest'),
]
# Calls what the form's submit handler calls; a refusal is its error message, an acceptance null.
NODE_HARNESS = '''
(async () => {
  const input = JSON.parse(process.env.SMS_COMMAND_INPUT);
  const refusal = async (make) => {
    try { await make(); return null; } catch (error) { return error.message; }
  };
  const cases = [];
  for (const c of input.cases) {
    const args = packWifiArgs(c.ssid, c.password);
    const key = await deriveKey(c.passphrase);
    const text = await encodeEnvelope(key, WIFI.code, args, c.stamp, new Uint8Array(c.nonce));
    cases.push({text, parts: smsParts(text), fingerprint: await keyFingerprint(key)});
  }
  const e = input.empty;
  const key = await deriveKey(e.passphrase);
  const empty = await encodeEnvelope(key, e.code, new Uint8Array(0), e.stamp, new Uint8Array(e.nonce));
  const badNetworks = [];
  for (const [ssid, password] of input.badNetworks) badNetworks.push(await refusal(() => packWifiArgs(ssid, password)));
  const badCodes = [];
  for (const code of input.badCodes) {
    badCodes.push(await refusal(() => encodeEnvelope(key, code, new Uint8Array(0), e.stamp, new Uint8Array(e.nonce))));
  }
  process.stdout.write(JSON.stringify({cases, empty, badNetworks, badCodes}));
})();
'''


@pytest.fixture(scope='module')
def page_run(tmp_path_factory):
    node = shutil.which('node')
    if not node:
        if os.environ.get('PHONELET_REQUIRE_NODE') == '1':
            pytest.fail('Node.js is required when PHONELET_REQUIRE_NODE=1')
        pytest.skip('Node.js is not installed; HTML/Python encoder comparison requires node')
    script = tmp_path_factory.mktemp('page') / 'run.js'
    script.write_text(page_core() + NODE_HARNESS)
    given = dict(cases=[case.values[0] for case in NODE_CASES],
                 empty=dict(passphrase=PASSPHRASE, code=ECHO_CODE, stamp=STAMP, nonce=list(NONCE)),
                 badNetworks=[network.values for network in BAD_NETWORKS],
                 badCodes=[code.values[0] for code in BAD_CODES])
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=120,
                          env=os.environ | {'SMS_COMMAND_INPUT': json.dumps(given)})
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize('index', range(len(NODE_CASES)), ids=[case.id for case in NODE_CASES])
def test_page_encoder_matches_python(page_run, index):
    case = NODE_CASES[index].values[0]
    key = envelope.derive_key(case['passphrase'])
    expected = wifi_sms(case['ssid'], case['password'], case['stamp'], bytes(case['nonce']), key)
    assert page_run['cases'][index] == dict(text=expected, parts=envelope.sms_parts(expected),
                                            fingerprint=envelope.key_fingerprint(key))


def test_page_encodes_a_command_without_arguments(page_run):
    assert page_run['empty'] == ECHO_VECTOR


@pytest.mark.parametrize('index', range(len(BAD_NETWORKS)), ids=[network.id for network in BAD_NETWORKS])
def test_page_refuses_what_the_board_would(page_run, index):
    expected = {'SSID': 'The SSID must be', 'Password': 'The password must be'}[BAD_NETWORKS[index].values[-1]]
    assert page_run['badNetworks'][index].startswith(expected)  # names the field that is wrong


@pytest.mark.parametrize('index', range(len(BAD_CODES)), ids=[code.id for code in BAD_CODES])
def test_page_refuses_bad_command_codes(page_run, index):
    assert page_run['badCodes'][index] == 'Bad command code'


# --- reception -------------------------------------------------------------------------------

def fragment(text, part=1, total=1, received=RECEIVED, **kwargs):
    item = parse_fragment(journal_record(text, part, total, slot=part, **kwargs))
    item['received_epoch'] = received
    return item


async def payloads(db):
    return [(job['backend'], json.loads(job['payload'])) for job in
            await db._all("SELECT backend,payload FROM outbox WHERE method='notify_sms' ORDER BY id")]


async def assert_hidden_command(db, ciphertext):
    """One complete command SMS: hidden in history and in its latest notifications, its ciphertext queued."""
    [sms] = await db.list_sms()
    assert sms['text'] == SMS_COMMAND_TEXT and not sms['partial']
    [command] = await command_rows(db)
    assert command['sms_id'] == sms['id'] and command['ciphertext'] == ciphertext
    latest = (await payloads(db))[-len(BACKENDS):]
    assert sorted(b for b, _ in latest) == sorted(BACKENDS) and all(p['text'] == SMS_COMMAND_TEXT for _, p in latest)
    return command


async def test_single_sms_command_is_hidden_and_queued_once(db):
    db.backends = BACKENDS
    await db.ingest_fragment(fragment(VECTOR))
    command = await assert_hidden_command(db, VECTOR)
    assert len(await payloads(db)) == len(BACKENDS)  # queued once
    assert command['status'] == 'pending' and command['name'] is None


def split(text):
    return text[:envelope.SMS_PART_CHARS], text[envelope.SMS_PART_CHARS:]


@pytest.mark.parametrize('order', [(1, 2), (2, 1)], ids=['in-order', 'reversed'])
async def test_two_part_command_in_any_order(db, order):
    db.backends = BACKENDS
    parts = dict(zip((1, 2), split(LONG)))
    for index, part in enumerate(order):
        await db.ingest_fragment(fragment(parts[part], part, 2, RECEIVED + 1000 * index))
    await assert_hidden_command(db, LONG)
    assert all(p['text'] == SMS_COMMAND_TEXT for _, p in await payloads(db))


@pytest.mark.parametrize('first', [1, 2], ids=['part-1-first', 'part-2-first'])
async def test_command_completed_after_its_first_part_was_published(db, first):
    db.backends = BACKENDS
    parts = dict(zip((1, 2), split(LONG)))
    # Without part 1 a base64url-only part 2 may be a command's ciphertext: hidden as well.
    shown = SMS_COMMAND_INCOMPLETE_TEXT
    await db.ingest_fragment(fragment(parts[first], first, 2, RECEIVED))
    await db.flush_fragments(timeout=600, now=RECEIVED + 601)
    [sms] = await db.list_sms()
    assert sms['partial'] and sms['text'] == shown and not await command_rows(db)
    assert all(p['text'] == shown for _, p in await payloads(db))
    await db.ingest_fragment(fragment(parts[3 - first], 3 - first, 2, RECEIVED + 1000))
    await assert_hidden_command(db, LONG)
    assert len(await payloads(db)) == 2 * len(BACKENDS)  # one hidden update, nothing else


@pytest.mark.parametrize('second,enabled,hidden', [
    (None, True, True),  # part 2 of LONG: base64url only
    ('see you at 5, bye', True, False),  # ordinary text
    (None, False, False),  # commands off: nothing to hide
], ids=['ciphertext', 'ordinary-text', 'commands-off'])
async def test_part_without_part_1_is_hidden_only_when_it_can_be_ciphertext(db, second, enabled, hidden):
    db.backends = BACKENDS
    db.sms_commands_enabled = enabled
    second = second or split(LONG)[1]
    await db.ingest_fragment(fragment(second, 2, 2, RECEIVED))
    await db.flush_fragments(timeout=600, now=RECEIVED + 601)
    [sms] = await db.list_sms()
    shown = SMS_COMMAND_INCOMPLETE_TEXT if hidden else '[missing part 1/2]' + second
    assert sms['partial'] and sms['text'] == shown and not await command_rows(db)
    assert all(p['text'] == shown for _, p in await payloads(db))


async def test_ambiguous_command_is_hidden_and_creates_nothing(db):
    db.backends = BACKENDS
    first, second = split(LONG)
    # Two first parts under one reference, then a part 2 whose SCTS matches neither: all three are
    # ambiguous; the command check comes before the ambiguous-reference text.
    for index, (text, part, scts) in enumerate([(first, 1, SCTS), (first, 1, '2026-09-21 14:13:05 +00:00'),
                                                 (second, 2, '2026-09-21 14:13:09 +00:00')], start=1):
        await db.ingest_fragment(fragment(text, part, 2, RECEIVED + index, ref=8, stamp=scts))
    await db.flush_fragments(timeout=600, now=RECEIVED + 700)
    assert [sms['text'] for sms in await db.list_sms()] == [SMS_COMMAND_INCOMPLETE_TEXT] * 3
    assert not await command_rows(db)
    assert all(first not in payload['text'] and second not in payload['text'] for _, payload in await payloads(db))


@pytest.mark.parametrize('text', ['hello', 'PC1', 'PC1x' + VECTOR[4:], 'pc1 ' + VECTOR[4:], 'Text PC1 ' + VECTOR[4:]],
                         ids=['plain', 'prefix-only', 'no-space', 'lower-case-prefix', 'prefix-inside'])
async def test_ordinary_sms_is_untouched(db, text):
    await db.ingest_fragment(fragment(text))
    [sms] = await db.list_sms()
    assert sms['text'] == text
    assert not await command_rows(db)


async def test_pw1_is_stored_and_notified_as_ordinary_sms_with_commands_enabled(db):
    db.backends = BACKENDS
    text = 'PW1 ordinary SMS text'
    for _ in range(2):  # journal replay must not duplicate the SMS or its notifications
        await db.ingest_fragment(fragment(text))
    [sms] = await db.list_sms()
    assert sms['text'] == text and not sms['partial']
    assert await db._all('SELECT text FROM fragments') == [{'text': text}]
    assert sorted(await payloads(db)) == sorted((b, {'sender': sms['sender'], 'text': text}) for b in BACKENDS)
    assert await command_rows(db) == []
    delivered = []

    class FakeBackend:
        def __init__(self, backend_id):
            self.backend_id = backend_id

        async def deliver(self, method, payload, checkpoint):
            delivered.append((self.backend_id, method, payload['text']))
            return True

    await deliver_pending(db, [FakeBackend(backend) for backend in BACKENDS])
    assert sorted(delivered) == sorted((b, 'notify_sms', text) for b in BACKENDS)


async def test_command_sms_with_whitespace_around_is_hidden_and_stored_trimmed(db):
    db.backends = BACKENDS
    await db.ingest_fragment(fragment(' ' + VECTOR + '\n'))
    await assert_hidden_command(db, VECTOR)


def replay(db, directory):
    return IngressReplay(db, CdrHandler(AsyncMock(), AsyncMock()), directory, directory / 'phonelet.csv',
                         reassembly_timeout=600)


async def test_journal_replay_adds_nothing_new(db, tmp_path):
    db.backends = BACKENDS
    first, second = split(LONG)
    (tmp_path / 'a.sms').write_bytes(journal_record(VECTOR, slot=3))
    (tmp_path / 'b.sms').write_bytes(journal_record(first, 1, 2, ref=8, slot=4))
    (tmp_path / 'c.sms').write_bytes(journal_record(second, 2, 2, ref=8, slot=5))
    await replay(db, tmp_path).replay_once()

    async def snapshot():
        return await db.list_sms(), await db._all('SELECT * FROM outbox ORDER BY id'), await command_rows(db)
    before = await snapshot()
    _, _, rows = before
    assert [row['ciphertext'] for row in rows] == [VECTOR, LONG]
    await replay(db, tmp_path).replay_once()  # a restart reads the whole journal again
    assert await snapshot() == before
    # A finished command drops its ciphertext; replay does not bring it back.
    await db.finish_sms_command(rows[0]['id'], 'rejected:disabled', 'SMS command rejected: disabled')
    await replay(db, tmp_path).replay_once()
    rows = await command_rows(db)
    assert len(rows) == 2 and rows[0]['status'] == 'done' and rows[0]['ciphertext'] is None


# --- the worker ------------------------------------------------------------------------------

class EchoCommand(Command):
    """A command known only to the tests: the worker runs it without changes."""
    code, name, label = ECHO_CODE, 'echo', 'Echo'

    @classmethod
    def from_settings(cls, config):
        return cls()

    def __init__(self, fail=False):
        self.ran, self.fail = [], fail

    def parse(self, packed_args):
        if packed_args:
            raise ValueError('Echo takes no arguments')
        return None

    async def run(self, args):
        self.ran.append(args)
        if self.fail:
            raise RuntimeError('invented failure')
        return 'done', f'{self.label}: done'


class SecondEcho(EchoCommand):
    """Another kind, for the tests that need two."""
    code, name, label = 201, 'second', 'Second'


@pytest.mark.parametrize('stamp_delta_ms,board_age_ms,last_stamp,expected', [
    (0, 0, None, 'done'),
    (-24 * 3600 * 1000, 0, None, 'done'),
    (-24 * 3600 * 1000 - 1, 0, None, 'rejected:stale'),
    (3600 * 1000, 0, None, 'done'),
    (3600 * 1000 + 1, 0, None, 'rejected:future'),
    (0, 48 * 3600 * 1000, None, 'done'),
    (0, 48 * 3600 * 1000 + 1, None, 'rejected:stale'),
    (0, 0, STAMP, 'rejected:replay'),
], ids=['fresh', 'stamp-24-h', 'stamp-too-old', 'stamp-1-h-ahead', 'stamp-too-far-ahead',
        'receipt-48-h', 'receipt-too-old', 'replayed-stamp'])
async def test_invalid_scts_single_command_checks_receipt_time(db, tmp_path, stamp_delta_ms, board_age_ms,
                                                             last_stamp, expected):
    db.backends = ('telegram',)
    item = tmp_path/'single-command.sms'
    item.write_bytes(journal_record(echo_sms(STAMP + stamp_delta_ms), stamp='2026-00-25 12:00:00 +00:00'))
    os.utime(item, (RECEIVED, RECEIVED))
    if last_stamp is not None:
        await db._set_metadata('sms_command_last_stamp', str(last_stamp))
        await db._conn.commit()
    await replay(db, tmp_path).replay_once()
    [sms] = await db.list_sms()
    assert sms['timestamp'] == datetime.fromtimestamp(RECEIVED, timezone.utc).isoformat()
    assert sms['text'] == SMS_COMMAND_TEXT and not sms['partial']
    echo = EchoCommand()
    await process_commands(db, KEY, by_code([echo]), clock=lambda: (STAMP + board_age_ms) / 1000)
    [command] = await command_rows(db)
    assert command['status'] == 'done' and command['result'] == expected
    assert echo.ran == ([None] if expected == 'done' else [])
    assert await db.sms_command_last_stamp() == (STAMP + stamp_delta_ms if expected == 'done' else last_stamp)
    before = await db.list_sms(), await command_rows(db), await db._all('SELECT * FROM outbox ORDER BY id')
    await replay(db, tmp_path).replay_once()
    await process_commands(db, KEY, by_code([echo]), clock=lambda: (STAMP + board_age_ms) / 1000)
    assert (await db.list_sms(), await command_rows(db), await db._all('SELECT * FROM outbox ORDER BY id')) == before
    assert echo.ran == ([None] if expected == 'done' else [])


@pytest.mark.parametrize('invalid_parts', [(1, 2), (1,), (2,)], ids=['both', 'first', 'second'])
@pytest.mark.parametrize('order', [(1, 2), (2, 1)], ids=['in-order', 'reversed'])
async def test_invalid_scts_multipart_command_executes_once(db, tmp_path, invalid_parts, order):
    """A receipt-time substitute in any part still joins the only candidate: one complete command."""
    db.backends = ('telegram',)
    received = time.time()
    text = echo_sms(int(received * 1000))
    parts = {1: text[:20], 2: text[20:]}
    for index, part in enumerate(order):
        stamp = '' if part in invalid_parts else datetime.fromtimestamp(received - 20, timezone.utc).isoformat()
        item = tmp_path/f'command-{part}.sms'
        item.write_bytes(journal_record(parts[part], part, 2, slot=part, stamp=stamp))
        os.utime(item, (received + index, received + index))
    await replay(db, tmp_path).replay_once()
    await db.flush_fragments(timeout=600, now=received + 602)
    echo = EchoCommand()
    await process_commands(db, KEY, by_code([echo]), clock=lambda: received)
    assert echo.ran == [None]
    [command] = await command_rows(db)
    assert command['status'] == 'done' and command['result'] == 'done'
    [sms] = await db.list_sms()
    assert sms['text'] == SMS_COMMAND_TEXT and not sms['partial']
    assert await db._all('SELECT ambiguous FROM assemblies') == [{'ambiguous': 0}]
    before = await db.list_sms(), await payloads(db), await command_rows(db)
    await replay(db, tmp_path).replay_once()
    await db.flush_fragments(timeout=600, now=received + 1200)
    await process_commands(db, KEY, by_code([echo]), clock=lambda: received)
    assert (await db.list_sms(), await payloads(db), await command_rows(db)) == before
    assert echo.ran == [None]


async def receive_command(db, text, scts=SCTS):
    """Receive `text` as a new one-part SMS; return its sms_commands row."""
    slot = len(await command_rows(db)) + 1  # a distinct journal record per SMS
    await db.ingest_fragment({**parse_fragment(journal_record(text, ref=slot, slot=slot, stamp=scts)),
                              'received_epoch': time.time()})
    return (await command_rows(db))[-1]


async def reject(db, text, key=KEY, scts=SCTS, clock=board_clock):
    """Receive and process one command that must be rejected; return (result, name, message)."""
    row = await receive_command(db, text, scts)
    stamp = await db.sms_command_last_stamp()
    kinds = [EchoCommand(), SecondEcho()]
    await process_commands(db, key, by_code(kinds), clock=clock)
    done = (await command_rows(db))[-1]
    assert done['id'] == row['id'] and done['status'] == 'done' and done['ciphertext'] is None
    assert all(kind.ran == [] for kind in kinds) and await db.sms_command_last_stamp() == stamp
    return done['result'], done['name'], (await health_by_event(db))[f"command:{row['id']}"]


@pytest.mark.parametrize('text,key,scts,expected', [
    pytest.param(VECTOR, None, SCTS, ('rejected:disabled', None, 'SMS command rejected: disabled'), id='disabled'),
    pytest.param(VECTOR, OTHER_KEY, SCTS, ('rejected:auth_failed', None, 'SMS command rejected: auth_failed'),
                 id='other-key'),
    pytest.param('PC1 not+base64', KEY, SCTS, ('rejected:bad_format', None, 'SMS command rejected: bad_format'),
                 id='bad-base64'),
    # Made 24 h and 1 s before the SMSC got it; the phone 1 h and 1 s ahead of the SMSC.
    pytest.param(echo_sms(STAMP), KEY, '2026-09-22 14:13:21 +00:00',
                 ('rejected:stale', 'echo', 'Echo command rejected: stale'), id='stale'),
    pytest.param(echo_sms(STAMP), KEY, '2026-09-21 13:13:19 +00:00',
                 ('rejected:future', 'echo', 'Echo command rejected: future'), id='future'),
    pytest.param(envelope.encode(KEY, ECHO_CODE, b'x', STAMP), KEY, SCTS,
                 ('rejected:bad_format', 'echo', 'Echo command rejected: bad_format'), id='bad-arguments'),
    pytest.param(envelope.encode(KEY, 77, b'', STAMP), KEY, SCTS,
                 ('rejected:unknown_command', None, 'SMS command rejected: unknown_command'), id='unknown-code'),
])
async def test_rejected_command_changes_nothing(db, text, key, scts, expected):
    db.backends = ('telegram',)
    assert await reject(db, text, key, scts) == expected


async def test_replays_whatever_the_command(db):
    db.backends = ('telegram',)
    await receive_command(db, echo_sms(STAMP, code=SecondEcho.code))
    await process_commands(db, KEY, by_code([SecondEcho()]), clock=board_clock)
    # The same stamp under a new nonce, and an older stamp: replays, of this kind or another.
    assert await reject(db, echo_sms(STAMP)) == ('rejected:replay', 'echo', 'Echo command rejected: replay')
    assert (await reject(db, echo_sms(STAMP - 1)))[0] == 'rejected:replay'
    assert (await reject(db, echo_sms(STAMP, code=SecondEcho.code)))[:2] == ('rejected:replay', 'second')
    assert await db.sms_command_last_stamp() == STAMP


async def test_checks_run_in_the_documented_order(db):
    db.backends = ('telegram',)
    await receive_command(db, echo_sms(STAMP))
    await process_commands(db, KEY, by_code([EchoCommand()]), clock=board_clock)
    stale = '2026-09-23 14:13:21 +00:00'
    assert (await reject(db, echo_sms(STAMP), scts=stale))[0] == 'rejected:replay'  # replay before freshness
    assert (await reject(db, envelope.encode(KEY, 77, b'', STAMP)))[0] == 'rejected:replay'  # ... and the code
    assert (await reject(db, envelope.encode(KEY, 77, b'', STAMP + 1), scts=stale))[0] == 'rejected:stale'
    # The same for the age by the board's clock.

    def days_later():
        return RECEIVED + 3 * 24 * 3600
    assert (await reject(db, echo_sms(STAMP), clock=days_later))[0] == 'rejected:replay'
    assert (await reject(db, envelope.encode(KEY, 77, b'', STAMP + 2), clock=days_later))[0] == 'rejected:stale'


@pytest.mark.parametrize('scts', ['2026-09-21 13:13:20 +00:00', '2026-09-22 14:13:20 +00:00'],
                         ids=['1-h-ahead', '24-h-old'])
async def test_freshness_limits_are_inclusive(db, scts):
    db.backends = ('telegram',)
    await receive_command(db, echo_sms(STAMP), scts)
    await process_commands(db, KEY, by_code([EchoCommand()]), clock=board_clock)
    assert await db.sms_command_last_stamp() == STAMP


async def test_old_command_from_the_journal_is_stale_by_the_board_clock(db):
    # A journal record outside the first inventory can still be rejected as stale.
    db.backends = ('telegram',)
    row = await receive_command(db, echo_sms(STAMP))
    echo = EchoCommand()
    await process_commands(db, KEY, by_code([echo]))  # the default clock: the board's real one
    assert echo.ran == [] and (await command_rows(db))[0]['result'] == 'rejected:stale'
    assert (await health_by_event(db))[f"command:{row['id']}"] == 'Echo command rejected: stale'


SMSC_MS = STAMP - 20_000  # SCTS


@pytest.mark.parametrize('board_ms,ran', [(SMSC_MS + 48 * 3600 * 1000, True), (SMSC_MS + 48 * 3600 * 1000 + 1, False),
                                          (0, True)], ids=['48-h-old', '48-h-and-1-ms-old', 'board-clock-far-behind'])
async def test_command_age_by_the_board_clock(db, board_ms, ran):
    db.backends = ('telegram',)
    echo = EchoCommand()
    await receive_command(db, echo_sms(STAMP))
    await process_commands(db, KEY, by_code([echo]), clock=lambda: board_ms / 1000)
    assert echo.ran == ([None] if ran else [])
    assert [r['result'] for r in await command_rows(db)] == ['done' if ran else 'rejected:stale']


async def test_rejection_log_names_the_command_or_its_code(db, caplog):
    db.backends = ('telegram',)
    caplog.set_level(logging.WARNING, logger='phonelet.sms_commands.worker')
    await reject(db, VECTOR, key=None)
    await reject(db, envelope.encode(KEY, 77, b'', STAMP))
    await reject(db, echo_sms(STAMP), scts='2026-09-22 14:13:21 +00:00')
    shown = [re.search(r'command=(.*) reason=', record.getMessage()).group(1) for record in caplog.records
             if record.name == 'phonelet.sms_commands.worker']
    assert shown == ['-', 'code 77', 'echo']


async def test_unexpected_check_error_rejects_and_the_next_command_runs(db):
    db.backends = ('telegram',)
    broken = await receive_command(db, echo_sms(STAMP))
    await db._conn.execute("UPDATE sms SET timestamp='broken' WHERE id=?", (broken['sms_id'],))
    await db._conn.commit()
    await receive_command(db, echo_sms(STAMP + 1))
    echo = EchoCommand()
    await process_commands(db, KEY, by_code([echo]), clock=board_clock)
    assert [row['result'] for row in await command_rows(db)] == ['rejected:error', 'done']
    assert (await health_by_event(db))[f"command:{broken['id']}"] == 'SMS command rejected: error'
    assert echo.ran == [None]


async def test_commands_of_two_kinds_run_in_one_stamp_sequence(db):
    db.backends = ('telegram',)
    echo, second = EchoCommand(), SecondEcho()
    first = await receive_command(db, echo_sms(STAMP))
    other = await receive_command(db, echo_sms(STAMP + 1, code=SecondEcho.code))
    await process_commands(db, KEY, by_code([echo, second]), clock=board_clock)
    assert echo.ran == [None] and second.ran == [None]
    assert [(r['name'], r['result']) for r in await command_rows(db)] == [('echo', 'done'), ('second', 'done')]
    assert await health_by_event(db) == {f"command:{first['id']}": 'Echo: done',
                                         f"command:{other['id']}": 'Second: done'}
    assert await db.sms_command_last_stamp() == STAMP + 1


async def test_a_failing_command_ends_as_failed_and_does_not_block_the_next(db):
    db.backends = ('telegram',)
    second = SecondEcho()
    first = await receive_command(db, echo_sms(STAMP))
    await receive_command(db, echo_sms(STAMP + 1, code=SecondEcho.code))
    await process_commands(db, KEY, by_code([EchoCommand(fail=True), second]), clock=board_clock)
    assert [(r['status'], r['result']) for r in await command_rows(db)] == [('done', 'failed:error'),
                                                                            ('done', 'done')]
    assert (await health_by_event(db))[f"command:{first['id']}"] == 'Echo: command failed, check the board'
    assert second.ran == [None]


async def test_a_command_is_running_and_its_stamp_recorded_before_it_runs(db):
    seen = []

    class Probe(EchoCommand):
        async def run(self, args):
            seen.append(((await command_rows(db))[0]['status'], await db.sms_command_last_stamp()))
            return await super().run(args)
    await receive_command(db, echo_sms(STAMP))
    await process_commands(db, KEY, by_code([Probe()]), clock=board_clock)
    assert seen == [('running', STAMP)]


async def test_a_command_the_database_cannot_mark_running_does_not_run(db, monkeypatch):
    echo = EchoCommand()
    await receive_command(db, echo_sms(STAMP))

    async def locked(*args):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(db, 'begin_sms_command', locked)
    with pytest.raises(RuntimeError):
        await process_commands(db, KEY, by_code([echo]), clock=board_clock)
    assert echo.ran == [] and (await command_rows(db))[0]['status'] == 'pending'


async def test_a_describe_that_raises_rejects_and_the_next_command_runs(db):
    db.backends = ('telegram',)

    class Undescribable(SecondEcho):
        def describe(self, args):
            raise RuntimeError('invented')
    bad, echo = Undescribable(), EchoCommand()
    await receive_command(db, echo_sms(STAMP, code=SecondEcho.code))
    await receive_command(db, echo_sms(STAMP + 1))
    await process_commands(db, KEY, by_code([bad, echo]), clock=board_clock)
    assert [r['result'] for r in await command_rows(db)] == ['rejected:error', 'done']
    assert bad.ran == [] and echo.ran == [None]


async def test_a_stop_while_a_command_runs_leaves_it_running_for_the_restart(db):
    db.backends = ('telegram',)

    class Stopped(EchoCommand):
        async def run(self, args):
            raise asyncio.CancelledError  # what stop_tasks does during a run
    await receive_command(db, echo_sms(STAMP))
    with pytest.raises(asyncio.CancelledError):
        await process_commands(db, KEY, by_code([Stopped()]), clock=board_clock)
    assert (await command_rows(db))[0]['status'] == 'running' and await health_messages(db) == []


async def test_a_result_and_its_notification_are_saved_together(db, monkeypatch):
    db.backends = ('telegram',)
    row = await receive_command(db, echo_sms(STAMP))

    async def locked(*args, **kwargs):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(db, '_enqueue', locked)  # after the reception, which queues too
    unsaved = await process_commands(db, KEY, by_code([EchoCommand()]), clock=board_clock)
    assert unsaved == (row['id'], 'done', 'Echo: done')
    assert (await command_rows(db))[0]['status'] == 'running'  # not done without its notification


def test_command_codes_must_be_unique_and_fit_a_byte():
    with pytest.raises(ValueError):
        by_code([EchoCommand(), EchoCommand()])
    wrong = EchoCommand()
    wrong.code = 256
    with pytest.raises(ValueError):
        by_code([wrong])


def test_a_command_without_parse_or_run_cannot_be_made():
    class NoRun(Command):
        code, name, label = 202, 'no_run', 'No run'

        def parse(self, packed_args):
            return None

    class NoParse(Command):
        code, name, label = 203, 'no_parse', 'No parse'

        async def run(self, args):
            return 'done', 'No parse: done'
    for incomplete in (NoRun, NoParse):
        with pytest.raises(TypeError):
            incomplete()


async def test_a_result_the_database_refuses_is_saved_later_without_running_again(db, monkeypatch):
    db.backends = ('telegram',)
    row = await receive_command(db, echo_sms(STAMP))
    await receive_command(db, echo_sms(STAMP + 1))
    real, calls = db.finish_sms_command, []

    async def flaky(*args, **kwargs):
        calls.append(args[0])
        if len(calls) <= 2:
            raise RuntimeError('database is locked')
        return await real(*args, **kwargs)
    monkeypatch.setattr(db, 'finish_sms_command', flaky)
    echo = EchoCommand()
    stop_after(monkeypatch, 3)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([echo]), 5, clock=board_clock)
    # Run once; its result refused on two passes, saved on the third before the next command ran.
    assert echo.ran == [None, None] and calls[:3] == [row['id']] * 3
    assert [(r['status'], r['result']) for r in await command_rows(db)] == [('done', 'done'), ('done', 'done')]
    assert (await health_by_event(db))[f"command:{row['id']}"] == 'Echo: done'


async def test_cancelling_the_worker_while_a_result_waits_to_be_saved_stops_it_and_leaves_the_command_running(
        db, monkeypatch):
    await receive_command(db, echo_sms(STAMP))

    async def broken(*args, **kwargs):
        raise RuntimeError('database is locked')

    async def sleep(seconds):
        raise asyncio.CancelledError  # what stop_tasks does to the waiting worker
    monkeypatch.setattr(db, 'finish_sms_command', broken)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    echo = EchoCommand()
    with pytest.raises(asyncio.CancelledError):
        await command_loop(db, KEY, by_code([echo]), 5, clock=board_clock)
    assert echo.ran == [None]
    assert (await command_rows(db))[0]['status'] == 'running'  # the next start ends it as interrupted


async def test_cancelling_the_worker_while_it_retries_a_save_stops_it(db, monkeypatch):
    await receive_command(db, echo_sms(STAMP))
    calls = []

    async def finish(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1:
            raise RuntimeError('database is locked')
        raise asyncio.CancelledError  # the stop lands in the retried save
    monkeypatch.setattr(db, 'finish_sms_command', finish)
    echo = EchoCommand()
    stop_after(monkeypatch, 3)
    with pytest.raises(asyncio.CancelledError):
        await command_loop(db, KEY, by_code([echo]), 5, clock=board_clock)
    assert echo.ran == [None] and len(calls) == 2


async def test_a_database_error_leaves_the_command_for_the_next_pass(db, monkeypatch):
    db.backends = ('telegram',)
    echo = EchoCommand()
    await receive_command(db, echo_sms(STAMP))
    real = db.sms_command_last_stamp

    async def locked():
        monkeypatch.setattr(db, 'sms_command_last_stamp', real)  # only the first read fails
        raise RuntimeError('database is locked')
    monkeypatch.setattr(db, 'sms_command_last_stamp', locked)
    ran_after_first_pass = []

    def between(passes):
        ran_after_first_pass.extend(echo.ran)
    stop_after(monkeypatch, 2, between)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([echo]), 5, clock=board_clock)
    # Had the first pass taken the command, the second would not find it pending.
    assert ran_after_first_pass == [] and echo.ran == [None]


async def test_loop_interrupts_each_running_command_with_its_own_message(db, monkeypatch):
    db.backends = ('telegram',)
    rows = [await receive_command(db, echo_sms(STAMP + n)) for n in range(3)]
    await db.begin_sms_command(rows[0]['id'], STAMP, 'echo')
    await db.begin_sms_command(rows[1]['id'], STAMP + 1, 'retired')  # a kind no longer in this release
    echo = EchoCommand()
    stop_after(monkeypatch, 1)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([echo]), 5, clock=board_clock)
    assert [(r['status'], r['result']) for r in await command_rows(db)] == [
        ('done', 'interrupted'), ('done', 'interrupted'), ('done', 'done')]
    assert echo.ran == [None]  # the pending one only: the interrupted ones are never repeated
    assert await health_messages(db) == [
        'Echo: command interrupted by a restart; not repeated',
        'SMS command retired interrupted by a restart; not repeated',
        'Echo: done',
    ]


async def test_loop_retries_ending_interrupted_commands_after_a_database_error(db, monkeypatch):
    db.backends = ('telegram',)
    row = await receive_command(db, echo_sms(STAMP))
    await db.begin_sms_command(row['id'], STAMP, 'echo')
    real, calls = db.running_sms_commands, []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('database is locked')
        return await real()
    monkeypatch.setattr(db, 'running_sms_commands', flaky)
    stop_after(monkeypatch, 3)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([EchoCommand()]), 5, clock=board_clock)
    assert (await command_rows(db))[0]['result'] == 'interrupted' and len(calls) == 2  # once after it worked


async def test_an_interrupted_command_is_logged_by_its_sms(db, monkeypatch, caplog):
    await db.ingest_fragment(fragment('an ordinary SMS first', ref=1))
    row = await receive_command(db, echo_sms(STAMP))
    await db.begin_sms_command(row['id'], STAMP, 'echo')
    stop_after(monkeypatch, 1)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([EchoCommand()]), 5, clock=board_clock)
    assert (row['id'], row['sms_id']) == (1, 2)
    assert 'SMS command interrupted, not repeated: sms=2 command=echo' in caplog.text


async def test_a_failing_tick_does_not_stop_the_other_ticks_or_the_commands(db, monkeypatch):
    db.backends = ('telegram',)
    ticked = []

    class Broken(SecondEcho):
        name = 'broken'

        async def tick(self, db):
            raise RuntimeError('invented')

    class Ticking(EchoCommand):
        async def tick(self, db):
            ticked.append(self.name)
    echo = Ticking()
    await receive_command(db, echo_sms(STAMP))
    stop_after(monkeypatch, 1)
    with pytest.raises(StopLoop):
        await command_loop(db, KEY, by_code([Broken(), echo]), 5, clock=board_clock)
    assert ticked == ['echo'] and echo.ran == [None]


async def test_with_key_removed_the_loop_still_ticks_and_rejects_queued_commands(db, monkeypatch):
    db.backends = ('telegram',)
    ticked = []

    class Ticking(EchoCommand):
        async def tick(self, db):
            ticked.append(1)
    echo = Ticking()
    await receive_command(db, echo_sms(STAMP))
    db.sms_commands_enabled = False  # reception is now off; the old queue must still drain
    stop_after(monkeypatch, 2)
    with pytest.raises(StopLoop):
        await command_loop(db, None, by_code([echo]), 5, clock=board_clock)
    assert ticked == [1, 1] and echo.ran == []
    assert await health_messages(db) == ['SMS command rejected: disabled']


@pytest.mark.parametrize('bodies,order', [
    pytest.param((VECTOR,), (1,), id='pc1-single'),
    pytest.param(split(LONG), (1, 2), id='pc1-multipart'),
    pytest.param(split(LONG), (2, 1), id='pc1-multipart-reversed'),
])
@pytest.mark.parametrize('history', [False, True], ids=['live', 'history'])
async def test_without_key_command_texts_are_ordinary_sms(tmp_db_path, monkeypatch, bodies, order, history):
    db = Database(tmp_db_path, BACKENDS)  # commands are off by default
    await db.init()
    try:
        for _ in range(2):  # journal replay must not duplicate notifications
            for part in order:
                await db.ingest_fragment(fragment(bodies[part - 1], part, len(bodies)), history=history)
        [sms] = await db.list_sms()
        text = ''.join(bodies)
        assert sms['text'] == text and not sms['partial']
        jobs = await payloads(db)
        assert sorted(backend for backend, _ in jobs) == ([] if history else sorted(BACKENDS))
        assert all(payload['text'] == text for _, payload in jobs)
        assert [row['text'] for row in await db._all('SELECT text FROM fragments ORDER BY part')] == list(bodies)
        assert await command_rows(db) == []
        delivered = []

        class FakeBackend:
            def __init__(self, backend_id):
                self.backend_id = backend_id

            async def deliver(self, method, payload, checkpoint):
                delivered.append((self.backend_id, method, payload['text']))
                return True

        await deliver_pending(db, [FakeBackend(backend) for backend in BACKENDS])
        assert sorted(delivered) == ([] if history else sorted((b, 'notify_sms', text) for b in BACKENDS))
        echo = EchoCommand()
        stop_after(monkeypatch, 2)
        with pytest.raises(StopLoop):
            await command_loop(db, None, by_code([echo]), 5, clock=board_clock)
        assert echo.ran == [] and await health_messages(db) == []
    finally:
        await db.close()


# --- the CLI ---------------------------------------------------------------------------------

def run_cli(monkeypatch, capsys, args, answers=(), read=None):
    """Run the CLI with getpass answering `answers` in turn, or calling `read`; return (exit code, output)."""
    answers = iter(answers)
    monkeypatch.setattr(getpass, 'getpass', read or (lambda prompt: next(answers)))
    monkeypatch.setattr(sys, 'argv', ['phonelet.sms_commands', *args])
    try:
        runpy.run_module('phonelet.sms_commands', run_name='__main__')
        code = None
    except SystemExit as stop:
        code = stop.code
    return code, capsys.readouterr()


def test_cli_module_help():
    done = subprocess.run([sys.executable, '-m', 'phonelet.sms_commands', '--help'],
                          cwd=ROOT, env=os.environ | {'PYTHONPATH': 'src'},
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert '{key,wifi}' in done.stdout
    assert 'derive PHONELET_SMS_COMMAND_KEY' in done.stdout
    assert done.stderr == ''


def test_cli_key(monkeypatch, capsys):
    code, out = run_cli(monkeypatch, capsys, ['key'], [PASSPHRASE, PASSPHRASE])
    assert code is None and out.out == f'PHONELET_SMS_COMMAND_KEY={KEY_TEXT}\n'
    assert f'Key fingerprint: {FINGERPRINT}' in out.err


@pytest.mark.parametrize('extra,answers,password', [([], [PASSWORD, PASSWORD, PASSPHRASE], PASSWORD),
                                                    (['--open'], [PASSPHRASE], '')], ids=['wpa', 'open'])
def test_cli_wifi(monkeypatch, capsys, extra, answers, password):
    before = time.time() * 1000
    code, out = run_cli(monkeypatch, capsys, ['wifi', '--ssid', SSID, *extra], answers)
    text = out.out.strip()
    decoded = envelope.decode(KEY, text)
    assert code is None and decoded.code == wifi.CODE and wifi.unpack_args(decoded.packed_args) == (SSID, password)
    assert before - 1 <= decoded.stamp_ms <= time.time() * 1000  # the computer's clock, in ms
    assert f'{len(text)} characters, 1 SMS part(s)' in out.err and f'Key fingerprint: {FINGERPRINT}' in out.err


@pytest.mark.parametrize('args,answers,message,secret', [
    (['key'], [PASSPHRASE, PASSPHRASE + 'x'], 'The two entries differ', PASSPHRASE),
    (['key'], ['fifteen chars..'] * 2, 'shorter than 16', 'fifteen'),
    (['wifi', '--ssid', SSID], ['short12'] * 2, 'Password must be', 'short12'),  # before the passphrase
    (['wifi', '--ssid', SSID], ['', ''], 'Empty Wi-Fi password', None),
    (['wifi', '--ssid', SSID, '--open'], [''], 'Empty passphrase', None),
], ids=['entries-differ', 'short-passphrase', 'bad-password', 'empty-password', 'empty-passphrase'])
def test_cli_refuses_without_printing_secrets(monkeypatch, capsys, args, answers, message, secret):
    code, out = run_cli(monkeypatch, capsys, args, answers)
    assert message in code and out.out == ''
    assert secret is None or secret not in code + out.out + out.err


@pytest.mark.parametrize('args', [['key'], ['wifi', '--ssid', SSID]], ids=['passphrase', 'wifi-password'])
def test_cli_does_not_quote_undecodable_input(monkeypatch, capsys, args):
    def undecodable(prompt):  # a non-UTF-8 byte typed at a hidden prompt
        raise UnicodeDecodeError('utf-8', b'ab\xffcd', 2, 3, 'invalid start byte')
    code, out = run_cli(monkeypatch, capsys, args, read=undecodable)
    assert code == 'Input is not valid UTF-8' and '0xff' not in code + out.out + out.err


def test_cli_refuses_to_read_a_secret_the_terminal_may_echo(monkeypatch, capsys):
    def echoing(prompt):  # getpass without a terminal it can control: a warning, then plain input
        warnings.warn('Can not control echo on the terminal.', getpass.GetPassWarning)
        return PASSPHRASE
    for args in (['key'], ['wifi', '--ssid', SSID, '--open']):
        code, out = run_cli(monkeypatch, capsys, args, read=echoing)
        assert code == 'A terminal with hidden input is required' and out.out == ''


@pytest.mark.history_rebuild
async def test_clock_reset_after_rebuild_notifies_and_runs_wifi_command(tmp_path, monkeypatch):
    from phonelet.db import Database
    from tests.test_netplan_wifi import FakeSystem
    system = FakeSystem(tmp_path, networks={SSID: PASSWORD})
    command = wifi.WifiCommand(system.netplan())
    db = Database(str(tmp_path / 'rebuild.db'), ('test',), sms_commands_enabled=True)
    await db.init()
    try:
        consumer = replay(db, tmp_path)
        await consumer.replay_once()  # empty initial inventory completes durably
        assert (await db.ingress_state())[0]
        assert board_clock() < time.time()  # the board clock is behind the real time (a clock reset)
        monkeypatch.setattr('phonelet.db.time.time', board_clock)
        (tmp_path / 'new.sms').write_bytes(journal_record(VECTOR, stamp=SCTS))
        os.utime(tmp_path / 'new.sms', (RECEIVED, RECEIVED))
        await consumer.replay_once()
        assert (await db.list_sms())[0]['text'] == SMS_COMMAND_TEXT
        assert [p['text'] for _, p in await payloads(db)] == [SMS_COMMAND_TEXT]
        await process_commands(db, KEY, by_code([command]), clock=board_clock)
        assert system.calls and system.associated == (SSID, PASSWORD)
        assert (await command_rows(db))[0]['result'] == 'connected'
    finally:
        await db.close()


@pytest.mark.history_rebuild
@pytest.mark.parametrize('partial_first', [False, True])
async def test_history_command_completed_after_marker_never_runs(tmp_path, partial_first):
    from phonelet.db import Database
    db = Database(str(tmp_path / 'rebuild.db'), ('test',), sms_commands_enabled=True)
    await db.init()
    try:
        consumer = replay(db, tmp_path)
        text = echo_sms(STAMP)
        first, second = text[:20], text[20:]
        (tmp_path / 'first.sms').write_bytes(journal_record(first, 1, 2, stamp=SCTS))
        await consumer.replay_once()
        assert (await db.ingress_state())[0]
        if partial_first:
            await db.flush_fragments(60, now=time.time() + 601)
            assert (await db.list_sms())[0]['text'] == SMS_COMMAND_INCOMPLETE_TEXT
        (tmp_path / 'second.sms').write_bytes(journal_record(second, 2, 2, stamp=SCTS))
        await consumer.replay_once()
        assert (await db.list_sms())[0]['text'] == SMS_COMMAND_TEXT
        echo = EchoCommand()
        await process_commands(db, KEY, by_code([echo]), clock=board_clock)
        assert echo.ran == [] and await command_rows(db) == [] and await payloads(db) == []
    finally:
        await db.close()


@pytest.mark.history_rebuild
async def test_interrupted_inventory_precedes_new_part_after_clock_reset(tmp_path, monkeypatch):
    from phonelet.db import Database
    db_path = str(tmp_path / 'rebuild.db')
    text = echo_sms(STAMP)
    first = tmp_path / 'first.sms'
    first.write_bytes(journal_record(text[:20], 1, 2, stamp=SCTS))
    os.utime(first, (RECEIVED + 1000, RECEIVED + 1000))
    db = Database(db_path, BACKENDS, sms_commands_enabled=True)
    await db.init()
    try:
        consumer = replay(db, tmp_path)
        await consumer._first_inventory()
        commit = db._conn.commit
        with monkeypatch.context() as patcher:
            failed = False
            async def fail_once():
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError('synthetic commit failure')
                await commit()
            patcher.setattr(db._conn, 'commit', fail_once)
            await consumer.replay_once()
        assert consumer.errors == 1 and not (await db.ingress_state())[0]
        assert await db._all('SELECT * FROM assemblies') == []
    finally:
        await db.close()
    second = tmp_path / 'second.sms'
    second.write_bytes(journal_record(text[20:], 2, 2, stamp=SCTS))
    os.utime(second, (RECEIVED, RECEIVED))
    db = Database(db_path, BACKENDS, sms_commands_enabled=True)
    await db.init()
    try:
        await replay(db, tmp_path).replay_once()
        [assembly] = await db._all('SELECT * FROM assemblies')
        assert assembly['id'] == parse_fragment(first.read_bytes())['source_id']
        assert assembly['history'] and (await db.ingress_state())[0]
        assert (await db.list_sms())[0]['text'] == SMS_COMMAND_TEXT
        echo = EchoCommand()
        await process_commands(db, KEY, by_code([echo]), clock=board_clock)
        assert await payloads(db) == [] and await command_rows(db) == [] and echo.ran == []
    finally:
        await db.close()


@pytest.mark.parametrize('published', [False, True], ids=['unpublished', 'published-partial'])
async def test_historical_part_promotes_a_live_assembly_to_history(db, published):
    db.backends = BACKENDS
    text = echo_sms(STAMP)
    await db.ingest_fragment(fragment(text[20:], 2, 2))
    if published:
        await db.flush_fragments(60, now=RECEIVED + 601)
    before = await payloads(db)
    assert len(before) == (len(BACKENDS) if published else 0)
    await db.ingest_fragment(fragment(text[:20], 1, 2, received=RECEIVED + 1), history=True)
    assert (await db._all('SELECT history FROM assemblies')) == [{'history': 1}]
    assert (await db.list_sms())[0]['text'] == SMS_COMMAND_TEXT
    echo = EchoCommand()
    await process_commands(db, KEY, by_code([echo]), clock=board_clock)
    assert await payloads(db) == before and await command_rows(db) == [] and echo.ran == []
