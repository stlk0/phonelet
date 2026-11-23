"""The autouse no_host_paths fixture (conftest.py) refuses the production host paths."""
import os
import sqlite3
from pathlib import Path

import pytest

from phonelet.config import Settings


@pytest.mark.parametrize('touch', [
    lambda: open('/var/lib/phonelet/phonelet.db', 'rb'),
    lambda: Path('/var/log/asterisk/cdr-custom/phonelet.csv').open('rb'),
    lambda: sqlite3.connect('/var/lib/phonelet/phonelet.db'),
    lambda: os.listdir('/var/spool/asterisk/phonelet-sms'),
    lambda: list(Path('/var/spool/asterisk/phonelet-sms').glob('*.sms')),
    lambda: os.makedirs('/var/lib/phonelet/recordings'),
    lambda: os.rename('/nonexistent/source', '/var/lib/phonelet/destination'),
    lambda: open('/var/lib/phonelet/../phonelet/recordings/x.mp3', 'wb'),
], ids=['open', 'path-open', 'sqlite', 'listdir', 'glob', 'makedirs', 'rename-target', 'dotdot'])
def test_production_paths_are_refused_and_recorded(no_host_paths, touch):
    with pytest.raises(PermissionError, match='production path'):
        touch()
    assert len(no_host_paths) == 1
    no_host_paths.clear()  # the violation was provoked on purpose


def test_similar_prefix_and_temp_paths_are_allowed(no_host_paths, tmp_path):
    with pytest.raises(FileNotFoundError):
        open('/var/lib/phonelet-other/x')
    (tmp_path / 'sms').mkdir()
    assert list((tmp_path / 'sms').glob('*.sms')) == []
    sqlite3.connect(tmp_path / 'x.db').close()
    assert no_host_paths == []


@pytest.mark.parametrize('field', ['db_path', 'recordings_dir', 'sms_ingress_dir', 'cdr_log_path',
                                   'wifi_netplan_file', 'wifi_state_dir'])
def test_every_default_setting_path_is_guarded(no_host_paths, field):
    """A test that forgets to override a path would use the default; it must be caught."""
    with pytest.raises(PermissionError):
        open(Settings.model_fields[field].default)
    no_host_paths.clear()
