import os
import sys

import pytest
import pytest_asyncio

# Settings are instantiated at import time, before fixtures can isolate them.
# Keep only test switches; exported developer settings must never enable live backends.
TEST_ENVIRONMENT_SWITCHES = {'PHONELET_REQUIRE_NODE'}
for name in list(os.environ):
    if name.startswith('PHONELET_') and name not in TEST_ENVIRONMENT_SWITCHES:
        del os.environ[name]

# Production locations that no test may touch. The default Settings point here,
# so a test that forgets to override a path would read or write the real host
# directories (or silently pass only on a machine where they do not exist).
HOST_PATHS = ('/var/spool/asterisk', '/var/log/asterisk', '/var/lib/phonelet', '/etc/netplan')
# Audit events whose first argument is a filesystem path: open() and os.open,
# SQLite connections (sqlite3 opens the file itself, not through open()),
# directory listing and globbing, and the mutating calls.
PATH_EVENTS = frozenset({
    'open', 'sqlite3.connect', 'os.listdir', 'os.scandir', 'os.walk', 'glob.glob',
    'pathlib.Path.glob', 'pathlib.Path.rglob', 'os.mkdir', 'os.rmdir', 'os.remove',
    'os.rename', 'os.truncate', 'os.chmod', 'os.chown', 'os.link', 'os.symlink',
    'os.chdir', 'shutil.copyfile', 'shutil.copytree', 'shutil.move', 'shutil.rmtree',
})
TWO_PATH_EVENTS = frozenset({
    'os.rename', 'os.link', 'os.symlink', 'shutil.copyfile', 'shutil.copytree', 'shutil.move',
})  # (src, dst)
VIOLATIONS = []


def _host_path(value):
    if isinstance(value, (bytes, os.PathLike)):
        value = os.fsdecode(value)
    if not isinstance(value, str):  # file descriptors, None
        return None
    value = os.path.abspath(value)
    if any(value == root or value.startswith(root + '/') for root in HOST_PATHS):
        return value
    return None


def _audit(event, args):
    if event not in PATH_EVENTS:
        return
    for path in args[:2] if event in TWO_PATH_EVENTS else args[:1]:
        host_path = _host_path(path)
        if host_path:
            VIOLATIONS.append(f'{event} {host_path}')
            # Refuse the operation, so a failing test never reaches the real
            # path. Code under test may swallow this error (replay_once logs
            # and retries), hence the check at fixture teardown.
            raise PermissionError(f'test touched a production path: {host_path}')


# Not caught: subprocesses (deploy.sh, --self-test), bare stat/exists, sqlite "file:" URIs.
# An audit hook cannot be removed; it is installed at import and stays on for the whole session,
# so module and session fixtures are guarded too (their violations surface on the next test).
sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def no_host_paths():
    """Fail any test that opens, lists or modifies a production host path.

    Yields the recorded violations so the guard's own tests can provoke one
    on purpose and clear it.
    """
    yield VIOLATIONS
    found = list(VIOLATIONS)
    VIOLATIONS.clear()
    assert not found, 'test touched production paths (give it paths under tmp_path): ' + '; '.join(found)


@pytest.fixture(autouse=True)
def records_are_new(request, monkeypatch):
    """Most tests exercise normal reception after rebuild; dedicated tests keep it pending."""
    if request.node.get_closest_marker('history_rebuild'):
        return
    from phonelet.db import Database
    init = Database.init

    async def replay_already_complete(self):
        await init(self)
        await self.complete_ingress()
    monkeypatch.setattr(Database, 'init', replay_already_complete)


@pytest.fixture
def tmp_db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def db(tmp_db_path):
    from phonelet.db import Database
    database = Database(tmp_db_path)
    await database.init()
    yield database
    await database.close()


@pytest.fixture(scope='module')
def driver_binary(tmp_path_factory):
    """The patched C driver runner, compiled once per PTY test module."""
    from tests.pty_modem import compile_driver
    return compile_driver(tmp_path_factory.mktemp('pty-driver'))
