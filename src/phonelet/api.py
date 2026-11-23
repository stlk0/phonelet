import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from phonelet import config
from phonelet.constants import RecordingState
from phonelet.contracts import sms_journal_ok
from phonelet.db import Database
from phonelet.recording import recording_state


class SmsResponse(BaseModel):
    id: int
    timestamp: str
    received_at: str
    sender: str
    text: str
    partial: bool


class CallResponse(BaseModel):
    id: int
    timestamp: str
    received_at: str
    caller: str
    duration: int
    status: str
    recording_path: str | None
    recording_state: str


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _app_settings(app: FastAPI):
    return getattr(app.state, "settings", config.settings)


async def api_key_auth(request: Request, key: str = Depends(_api_key_header)):
    settings = _app_settings(request.app)
    if not key or not secrets.compare_digest(key.encode(), settings.api_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


def readiness(state) -> dict:
    """Readiness to receive SMS and calls; software_ready excludes modem network registration."""
    ami = getattr(state, "ami_client", None)
    monitor = getattr(state, "modem_monitor", None)
    ingress = getattr(state, "ingress", None)
    ingress_status = ingress.status if ingress else {}
    driver = monitor.driver_ingress if monitor else {}
    authenticated = bool(ami and ami.authenticated)
    modem_ready = bool(monitor and monitor.readiness.get('ready'))
    software_ready = bool(authenticated
                          and sms_journal_ok(driver.get('version'), driver.get('status'))
                          and ingress_status.get('sms_journal_available')
                          and ingress_status.get('cdr_journal_available')
                          and ingress_status.get('ingress_updated')
                          and ingress_status.get('ingress_fresh')
                          and ingress_status.get('ingress_errors') == 0)
    is_ready = software_ready and modem_ready
    return {'status': 'ready' if is_ready else 'unavailable', 'ami_authenticated': authenticated,
            'software_ready': software_ready, 'modem_ready': modem_ready, 'driver_ingress': driver,
            **ingress_status}


def create_app(db: Database | None = None) -> FastAPI:
    app = FastAPI(title="Phonelet", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db

    def database():
        return app.state.db

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready", dependencies=[Depends(api_key_auth)])
    async def ready():
        content = readiness(app.state)
        return JSONResponse(status_code=200 if content['status'] == 'ready' else 503, content=content)

    @app.get("/api/sms", response_model=list[SmsResponse], dependencies=[Depends(api_key_auth)])
    async def list_sms(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        sender: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ):
        return await database().list_sms(limit=limit, offset=offset, sender=sender,
                                 since=since.isoformat() if since else None,
                                 until=until.isoformat() if until else None)

    @app.get("/api/calls", response_model=list[CallResponse], dependencies=[Depends(api_key_auth)])
    async def list_calls(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return await database().list_calls(limit=limit, offset=offset)

    @app.get("/api/calls/{call_id}/recording", dependencies=[Depends(api_key_auth)])
    async def get_recording(call_id: int):
        call = await database().get_call(call_id)
        if not call or not call.get("recording_path"):
            raise HTTPException(status_code=404, detail="Recording not found")
        rec_path = Path(call["recording_path"]).resolve()
        recordings_base = Path(_app_settings(app).recordings_dir).resolve()
        # Served files must stay under the recordings dir; checked here, not only inside recording_state.
        if (not rec_path.is_relative_to(recordings_base)
                or recording_state(call, recordings_base) != RecordingState.READY):
            raise HTTPException(status_code=404, detail="Recording not found")
        return FileResponse(rec_path, media_type="audio/mpeg", filename=rec_path.name)

    @app.get("/api/stats", dependencies=[Depends(api_key_auth)])
    async def get_stats():
        stats = await database().get_stats()
        stats["notification_queue"] = await database().outbox_stats()
        ingress = getattr(app.state, "ingress", None)
        if ingress:
            stats.update(ingress.status)
        ami_client = getattr(app.state, "ami_client", None)
        stats["ami_connected"] = ami_client.connected if ami_client else False
        recordings_dir = Path(_app_settings(app).recordings_dir)
        if recordings_dir.exists():
            disk = shutil.disk_usage(recordings_dir)
            stats["disk_free_mb"] = round(disk.free / 1024 / 1024)
        modem_monitor = getattr(app.state, "modem_monitor", None)
        if modem_monitor:
            stats.update(modem_monitor.status)
        return stats

    @app.get("/api/modem", dependencies=[Depends(api_key_auth)])
    async def get_modem():
        modem_monitor = getattr(app.state, "modem_monitor", None)
        if not modem_monitor:
            raise HTTPException(status_code=503, detail="Modem monitor not available")
        return modem_monitor.status

    return app
