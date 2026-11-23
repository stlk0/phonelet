"""Compile the actual upstream grammar and patched sender; no modem or Asterisk daemon."""
import base64
from pathlib import Path
import subprocess

import pytest

from tests.pty_modem import extract_function, patch_sources

pytestmark = pytest.mark.driver

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def sender(tmp_path_factory):
    temporary = tmp_path_factory.mktemp('sms-parser')
    patch_sources(temporary)  # SHA-checked pristine sources and every project patch, --fuzz=0
    function = extract_function((temporary / 'app.c').read_text(), 'app_send_sms_base64_exec')
    preamble = r'''
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <alloca.h>
#define attribute_unused __attribute__((unused))
#define AST_APP_ARG(name) char *name
#define AST_DECLARE_APP_ARGS(name, members) struct { unsigned argc; union { char *argv[6]; struct { members }; }; } name
#define AST_STANDARD_APP_ARGS(name, parse) name.argc = __ast_app_separate_args(parse, ',', 1, name.argv, 6)
#define ast_strlen_zero(s) (!(s) || !*(s))
#define ast_strdupa(s) strcpy(alloca(strlen(s) + 1), s)
#define ast_malloc malloc
#define ast_free free
#define LOG_ERROR 1
#define error2str(err) "synthetic send error"
#define ast_log(level, ...) fprintf(stderr, __VA_ARGS__)
struct ast_channel;
static int send_sms(const char *dev, const char *number, const char *message,
                    const char *validity, const char *report, const char *payload, size_t size) {
    if (strcmp(dev, "dongle0") || strcmp(number, "+15551234567") || *validity || *report || strcmp(payload, "1") || size != 2) return -1;
    fwrite(message, 1, strlen(message), stdout);
    return 0;
}
'''
    source = preamble + (ROOT / 'tests/fixtures/asterisk_args.c').read_text() + (ROOT / 'tests/fixtures/asterisk_base64.c').read_text() + function + r'''
int main(void) {
    char buffer[100000];
    size_t len = fread(buffer, 1, sizeof(buffer)-1, stdin);
    buffer[len] = '\0';
    base64_init();
    return app_send_sms_base64_exec(NULL, buffer) ? 1 : 0;
}
'''
    (temporary / 'sender.c').write_text(source)
    # GCC 16 warns about incnt in the verbatim Asterisk Base64 fixture. Keep that
    # upstream copy intact; GCC and clang both support this targeted suppression.
    subprocess.run(['cc', '-Wall', '-Werror', '-Wno-unused-but-set-variable',
                    str(temporary / 'sender.c'), '-o', str(temporary / 'sender')], check=True)
    return temporary / 'sender'


@pytest.mark.parametrize('body', ['Hello, world', 'Say "yes"', 'Hello (world', 'unmatched ) [ ]',
                                  'back\\slash\\\\tail\\', 'line one\nline two\r\n',
                                  'Привет, "мир"! (тест', 'Привет, мир!\n' * 100, 'a,b\\c"(' * 100])
def test_actual_asterisk_parser_preserves_sms(sender, body):
    encoded = base64.b64encode(body.encode())
    result = subprocess.run([str(sender)], input=b'dongle0,+15551234567,' + encoded + b',,,1', capture_output=True, check=True)
    assert result.stdout == body.encode()


def test_sms_rejects_embedded_nul(sender):
    result = subprocess.run([str(sender)], input=b'dongle0,+15551234567,QQBC,,,1', capture_output=True)
    assert result.returncode != 0
    assert result.stdout == b''


@pytest.mark.parametrize('encoded', ['QQ==AAAA', 'Q!Q=', 'Q=Q=', 'QQ=A', 'QQ===', 'QQ',
                                     'QR==', 'QUJ=', '====', '=AAA', 'AAAA====', ''])
def test_sms_rejects_noncanonical_base64_with_private_log(sender, encoded):
    result = subprocess.run([str(sender)], input=f'dongle0,+15551234567,{encoded},,,1'.encode(),
                            capture_output=True)
    assert result.returncode == 1 and result.stdout == b''
    assert result.stderr == b'Invalid Base64 SMS body -- SMS will not be sent\n'
