import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch

from fastapi.routing import APIRoute

from phonelet.api import create_app
from tests.helpers import store_call, store_sms


@pytest.fixture
def api_key():
    return "test-api-key"


@pytest_asyncio.fixture
async def app(tmp_db_path, api_key):
    with patch("phonelet.config.settings") as mock_settings:
        mock_settings.db_path = tmp_db_path
        mock_settings.api_key = api_key
        mock_settings.recordings_dir = "/tmp/recordings"

        from phonelet.api import create_app
        from phonelet.db import Database

        database = Database(tmp_db_path)
        await database.init()

        application = create_app(database)
        yield application
        await database.close()


@pytest_asyncio.fixture
async def client(app, api_key):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["X-API-Key"] = api_key
        yield c


async def test_list_sms_empty(client):
    resp = await client.get("/api/sms")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_sms_with_data(client, app):
    db = app.state.db
    await store_sms(db, sender="+15551234567", text="Hello")

    resp = await client.get("/api/sms")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["sender"] == "+15551234567"


async def test_list_calls_empty(client):
    resp = await client.get("/api/calls")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_rejected_call_appears_in_api_without_delivery_jobs(client, app):
    db = app.state.db
    db.backends = ("telegram", "homeassistant", "other")
    call_id = await db.insert_call("+15551234567", 0, "rejected", source_id="cdr:rejected",
                                  timestamp="2026-09-25T12:00:00+00:00")

    response = await client.get("/api/calls")
    assert response.status_code == 200
    calls = response.json()
    assert len(calls) == 1
    assert calls[0]["id"] == call_id
    assert calls[0]["caller"] == "+15551234567"
    assert calls[0]["status"] == "rejected"
    assert calls[0]["duration"] == 0
    assert await db.outbox_stats() == []


async def test_get_stats(client, app):
    db = app.state.db
    await store_sms(db, sender="+7", text="t")
    await store_call(db, caller="+7", duration=60, status="answered")
    await store_call(db, caller="+7", duration=0, status="missed")

    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert stats["total_sms"] == 1
    assert stats["total_calls"] == 2
    assert stats["missed_calls"] == 1
    assert stats["ami_connected"] is False


@pytest.mark.parametrize('method,path', [
    (method, route.path_format.format(**{name: 1 for name in route.param_convertors}))
    for route in create_app().routes
    if isinstance(route, APIRoute) and route.path != '/health'
    for method in sorted(route.methods)
])
async def test_api_key_required(app, method, path):
    # Enumerate routes, not auth dependencies: removing protection must still fail.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.request(method, path)
        assert resp.status_code == 401


async def test_wrong_api_key(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["X-API-Key"] = "wrong-key"
        resp = await c.get("/api/sms")
        assert resp.status_code == 401


@pytest.mark.parametrize("header,status", [(None, 401), ("", 401), ("wrong-key", 401),
                                           ("test-api", 401), ("test-api-key-2", 401),
                                           ("test-api-key", 200)])
async def test_api_key_comparison(app, api_key, header, status):
    assert api_key == "test-api-key"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        if header is not None:
            c.headers["X-API-Key"] = header
        assert (await c.get("/api/sms")).status_code == status


@pytest.mark.parametrize("header", [b"\xff\xfe", "ключ".encode()])
async def test_non_ascii_api_key_is_rejected(app, header):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/sms", headers={"X-API-Key": header})).status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_documentation_disabled(client, path):
    assert (await client.get(path)).status_code == 404


async def test_health_no_auth(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"status": "ok"}
        assert "ami_connected" not in data
        assert "disk_free_mb" not in data


async def test_recording_path_traversal(client, app):
    """Path traversal attempt must return 404."""
    db = app.state.db
    call_id = await store_call(db,
        caller="+7", duration=60, status="answered",
        recording_path="../../etc/passwd",
    )
    resp = await client.get(f"/api/calls/{call_id}/recording")
    assert resp.status_code == 404


async def test_recording_nonexistent_file(client, app):
    """Valid-looking path under recordings_dir but file doesn't exist must return 404."""
    db = app.state.db
    call_id = await store_call(db,
        caller="+7", duration=60, status="answered",
        recording_path="/tmp/recordings/2026/03/21/nonexistent.mp3",
    )
    resp = await client.get(f"/api/calls/{call_id}/recording")
    assert resp.status_code == 404


async def test_ready_recording_outside_directory_is_not_served(client, app, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from phonelet.constants import RecordingState

    base = tmp_path / 'recordings'
    base.mkdir()
    path = tmp_path / 'outside.mp3'
    path.write_bytes(b'ID3synthetic audio')
    path.with_name(path.name + '.status.json').write_text('{"status":"ready"}')
    app.state.settings = SimpleNamespace(api_key=client.headers['X-API-Key'], recordings_dir=str(base))
    call_id = await store_call(app.state.db, '+15550000001', 10, 'answered', str(path))
    await app.state.db.set_recording_state(call_id, RecordingState.READY)
    # Exercise the API boundary independently of recording_state's own path guard.
    monkeypatch.setattr('phonelet.api.recording_state', lambda call, base: RecordingState.READY)

    assert (await client.get(f'/api/calls/{call_id}/recording')).status_code == 404


async def test_sms_filters_and_response_model(client, app):
    db = app.state.db
    await store_sms(db, '+7','one',timestamp='2026-09-01T12:00:00+00:00',source_id='private-id')
    await store_sms(db, '+8','two',timestamp='2026-09-02T12:00:00+00:00')
    response = await client.get('/api/sms',params={'sender':'+7',
                                                'since':'2026-09-01T13:00:00+02:00'})
    assert len(response.json()) == 1
    assert 'source_id' not in response.json()[0]
    assert 'received_at' in response.json()[0]


async def test_ready_requires_authenticated_driver_boundary(client,app):
    from types import SimpleNamespace
    assert (await client.get('/ready')).status_code == 503
    app.state.ami_client = SimpleNamespace(authenticated=True)
    app.state.modem_monitor = SimpleNamespace(readiness={'ready':True},driver_ingress={})
    app.state.ingress = SimpleNamespace(status={'sms_journal_available':True,'cdr_journal_available':True,
                                               'ingress_errors':0,'ingress_updated':'now','ingress_fresh':True})
    assert (await client.get('/ready')).status_code == 503  # old driver
    app.state.modem_monitor.driver_ingress = {'version':'1','status':'ok'}
    assert (await client.get('/ready')).status_code == 200
    app.state.modem_monitor.driver_ingress['status'] = 'error'
    assert (await client.get('/ready')).status_code == 503
    client.headers.pop('X-API-Key')
    assert (await client.get('/ready')).status_code == 401


@pytest.mark.parametrize('marker',[None,'{"status":"pending"}','{"status":"unknown"}','{broken','[]'])
async def test_recording_download_requires_ready_marker(client,app,tmp_path,marker):
    from types import SimpleNamespace
    app.state.settings = SimpleNamespace(api_key=client.headers['X-API-Key'],recordings_dir=str(tmp_path))
    path = tmp_path/'recording.mp3'
    path.write_bytes(b'ID3nonempty data')
    call_id = await store_call(app.state.db, '+7',10,'answered',str(path))
    sidecar = path.with_name(path.name+'.status.json')
    if marker is not None:
        sidecar.write_text(marker)
    # The filesystem completion contract is enforced even with a stale DB flag.
    await app.state.db.set_recording_state(call_id,'ready')
    assert (await client.get(f'/api/calls/{call_id}/recording')).status_code == 404
    sidecar.write_text('{"status":"ready"}')
    response = await client.get(f'/api/calls/{call_id}/recording')
    assert response.status_code == 200
    assert response.content == path.read_bytes()


def ready_state(app, network_registered=True):
    from types import SimpleNamespace
    app.state.ami_client = SimpleNamespace(authenticated=True)
    app.state.modem_monitor = SimpleNamespace(readiness={'ready': network_registered},
                                              driver_ingress={'version': '1', 'status': 'ok'})
    app.state.ingress = SimpleNamespace(status={'sms_journal_available': True, 'cdr_journal_available': True,
                                               'ingress_errors': 0, 'ingress_updated': 'now', 'ingress_fresh': True})


@pytest.mark.parametrize('registered', [True, False])
async def test_ready_separates_software_from_network_registration(client, app, registered):
    ready_state(app, network_registered=registered)
    response = await client.get('/ready')
    body = response.json()
    assert response.status_code == (200 if registered else 503)
    assert body['status'] == ('ready' if registered else 'unavailable')
    assert body['software_ready'] is True
    assert body['modem_ready'] is registered
    assert body['ami_authenticated'] is True and body['driver_ingress'] == {'version': '1', 'status': 'ok'}
    assert body['ingress_fresh'] is True


@pytest.mark.parametrize('condition', ['ami', 'version', 'journal', 'sms_journal_available', 'cdr_journal_available',
                                       'ingress_updated', 'ingress_fresh', 'ingress_errors'])
async def test_each_software_condition_blocks_software_readiness(client, app, condition):
    ready_state(app)
    if condition == 'ami':
        app.state.ami_client.authenticated = False
    elif condition == 'version':
        app.state.modem_monitor.driver_ingress['version'] = '2'
    elif condition == 'journal':
        app.state.modem_monitor.driver_ingress['status'] = 'error'
    else:
        app.state.ingress.status[condition] = 1 if condition == 'ingress_errors' else None
    response = await client.get('/ready')
    assert response.status_code == 503
    assert response.json()['software_ready'] is False
    assert response.json()['status'] == 'unavailable'


async def test_ready_rejects_disconnected_device_even_with_registered_fields(client, app):
    from unittest.mock import AsyncMock
    from phonelet.modem_status import ModemStatusMonitor

    ready_state(app)
    ami = app.state.ami_client
    ami.connected = True
    ami.send_action = AsyncMock(return_value=[{
        'Device': 'dongle0', 'State': 'Not connected',
        'GSMRegistrationStatus': 'Registered, home network', 'RSSI': '18, -77 dBm',
        'SMSJournalVersion': '1', 'SMSJournalStatus': 'ok',
    }])
    app.state.modem_monitor = ModemStatusMonitor(ami)
    await app.state.modem_monitor.refresh()

    response = await client.get('/ready')
    assert response.status_code == 503
    assert response.json()['software_ready'] is True
    assert response.json()['modem_ready'] is False
