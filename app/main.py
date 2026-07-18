"""FastAPI application: HTTP API, WebSocket live feed, and static dashboard.

Wires together the UDP receiver, telemetry hub, recorder and SQLite database
under a single asyncio lifespan, and serves the mobile dashboard and its
analysis / comparison / debug pages.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import packet
from .config import settings
from .database import Database
from .logging_config import configure
from .recorder import Recorder
from .session_data import load_session
from .telemetry_hub import TelemetryHub
from .udp_receiver import UDPReceiver

log = logging.getLogger("app")


def _static_dir() -> Path:
    """Locate the bundled ``static`` directory.

    Under a PyInstaller build the data files are unpacked to ``sys._MEIPASS``;
    from a normal source checkout they sit next to this module.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "app" / "static"
    return Path(__file__).parent / "static"


STATIC_DIR = _static_dir()


class AppState:
    """Container for long-lived singletons, attached to ``app.state``."""

    db: Database
    hub: TelemetryHub
    receiver: UDPReceiver
    recorder: Recorder
    housekeeping_task: Optional[asyncio.Task] = None
    synthetic_task: Optional[asyncio.Task] = None


async def _housekeeping(state: AppState) -> None:
    """Periodic maintenance: idle-session detection."""
    try:
        while True:
            await asyncio.sleep(1.0)
            state.recorder.check_idle(time.monotonic())
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure(settings.log_level, settings.log_json)
    log.info(
        "starting FH6 telemetry",
        extra={"extra": {
            "udp": f"{settings.udp_host}:{settings.udp_port}",
            "http": f"{settings.http_host}:{settings.http_port}",
            "push_hz": settings.push_hz,
            "synthetic": settings.synthetic,
        }},
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)

    state = AppState()
    state.db = Database(settings.db_path)

    # One-time migration: recordings made with v1.0.x were decoded under a
    # mis-specified packet layout. Detect and re-decode them (originals are
    # kept as *.v1bak). No-op when there is nothing to rescue.
    try:
        from .rescue import rescue_data_dir
        rescued = rescue_data_dir(settings.sessions_dir, state.db)
        if rescued:
            log.info("v1.0.x recordings rescued",
                     extra={"extra": {"count": len(rescued)}})
    except Exception:  # pragma: no cover - rescue must never block startup
        log.exception("v1 recording rescue failed; continuing")

    state.hub = TelemetryHub(push_hz=settings.push_hz)
    state.recorder = Recorder(
        db=state.db,
        sessions_dir=settings.sessions_dir,
        idle_timeout_s=settings.session_idle_timeout_s,
        raw_format=settings.raw_format,
    )
    state.hub.on_frame = state.recorder.feed
    state.receiver = UDPReceiver(state.hub, settings.udp_host, settings.udp_port)

    app.state.fh6 = state

    await state.hub.start()
    await state.receiver.start()
    state.housekeeping_task = asyncio.create_task(_housekeeping(state), name="housekeeping")

    if settings.synthetic:
        from .synthetic import run_synthetic
        state.synthetic_task = asyncio.create_task(
            run_synthetic("127.0.0.1", settings.udp_port, settings.synthetic_hz),
            name="synthetic",
        )
        log.info("synthetic generator enabled")

    try:
        yield
    finally:
        log.info("shutting down")
        # Graceful shutdown: stop feeds, flush recorder, close DB.
        for task in (state.synthetic_task, state.housekeeping_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await state.receiver.stop()
        await state.hub.stop()
        state.recorder.shutdown()
        state.db.close()
        log.info("shutdown complete")


app = FastAPI(title="Forza Horizon 6 Telemetry", lifespan=lifespan)


def _state(request: Request = None) -> AppState:  # type: ignore[assignment]
    return app.state.fh6


# ---------------------------------------------------------------------------
# Health & status
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> Dict[str, Any]:
    st = _state()
    return {
        "status": "ok",
        "udp_port": settings.udp_port,
        "http_port": settings.http_port,
        "packet_size": packet.FH6_PACKET_SIZE,
        "recording": st.recorder.active_session_id is not None,
    }


@app.get("/api/status")
async def api_status() -> Dict[str, Any]:
    st = _state()
    now = time.monotonic()
    return {
        "receiver": st.hub.status_payload(now),
        "recording": st.recorder.status(),
        "push_hz": settings.push_hz,
        "synthetic": settings.synthetic,
    }


# ---------------------------------------------------------------------------
# WebSocket live feed
# ---------------------------------------------------------------------------
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    st = _state()
    q = st.hub.register()
    try:
        # Send an immediate status frame so the UI populates before the first tick.
        await ws.send_json({
            "type": "hello",
            "status": st.hub.status_payload(),
            "packet_size": packet.FH6_PACKET_SIZE,
        })
        while True:
            msg = await q.get()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover
        log.debug("websocket error", exc_info=True)
    finally:
        st.hub.unregister(q)


# ---------------------------------------------------------------------------
# Recording control
# ---------------------------------------------------------------------------
class MarkerBody(BaseModel):
    label: str = ""


@app.post("/api/recording/start")
async def rec_start() -> Dict[str, Any]:
    return _state().recorder.start_manual()


@app.post("/api/recording/stop")
async def rec_stop() -> Dict[str, Any]:
    return _state().recorder.stop_manual()


@app.post("/api/recording/marker")
async def rec_marker(body: MarkerBody) -> Dict[str, Any]:
    return _state().recorder.add_marker(body.label, time.monotonic())


@app.get("/api/recording/status")
async def rec_status() -> Dict[str, Any]:
    return _state().recorder.status()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
class RenameBody(BaseModel):
    name: Optional[str] = None
    notes: Optional[str] = None


def _session_or_404(session_id: int) -> Dict[str, Any]:
    row = _state().db.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


@app.get("/api/sessions")
async def list_sessions() -> Dict[str, Any]:
    return {"sessions": _state().db.list_sessions()}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: int) -> Dict[str, Any]:
    row = _session_or_404(session_id)
    row["markers"] = _state().db.list_markers(session_id)
    return row


@app.patch("/api/sessions/{session_id}")
async def patch_session(session_id: int, body: RenameBody) -> Dict[str, Any]:
    st = _state()
    _session_or_404(session_id)
    changed = {}
    if body.name is not None:
        st.db.rename_session(session_id, body.name)
        changed["name"] = body.name
    if body.notes is not None:
        st.db.set_notes(session_id, body.notes)
        changed["notes"] = body.notes
    return {"ok": True, "changed": changed}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: int) -> Dict[str, Any]:
    st = _state()
    _session_or_404(session_id)
    raw_path = st.db.delete_session(session_id)
    if raw_path:
        p = settings.sessions_dir / raw_path
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete raw file", extra={"extra": {"path": str(p)}})
    return {"ok": True}


@app.get("/api/sessions/{session_id}/markers")
async def session_markers(session_id: int) -> Dict[str, Any]:
    _session_or_404(session_id)
    return {"markers": _state().db.list_markers(session_id)}


def _load_or_404(row: Dict[str, Any]):
    raw_path = settings.sessions_dir / (row.get("raw_path") or "")
    if not raw_path.exists():
        raise HTTPException(status_code=404, detail="raw data file missing")
    return load_session(raw_path, row.get("raw_format", "csv"))


@app.get("/api/sessions/{session_id}/analysis")
async def session_analysis(session_id: int) -> Dict[str, Any]:
    from .analysis import analyse
    row = _session_or_404(session_id)
    sd = _load_or_404(row)
    result = analyse(sd)
    result["session"] = {
        "id": row["id"], "name": row["name"],
        "car_ordinal": row.get("car_ordinal"), "car_class": row.get("car_class"),
        "car_pi": row.get("car_pi"), "drivetrain": row.get("drivetrain"),
        "cylinders": row.get("cylinders"), "notes": row.get("notes", ""),
    }
    return result


@app.get("/api/sessions/{session_id}/route")
async def session_route(session_id: int, colour_by: str = "speed") -> Dict[str, Any]:
    from .comparison import single_route
    row = _session_or_404(session_id)
    sd = _load_or_404(row)
    return single_route(sd, colour_by)


@app.get("/api/sessions/{session_id}/download.csv")
async def download_csv(session_id: int) -> Response:
    row = _session_or_404(session_id)
    raw_path = settings.sessions_dir / (row.get("raw_path") or "")
    if not raw_path.exists():
        raise HTTPException(status_code=404, detail="raw data file missing")
    if row.get("raw_format") == "csv":
        filename = f"{_safe(row['name'])}.csv"
        return FileResponse(raw_path, media_type="text/csv", filename=filename)
    # Parquet -> convert to CSV on the fly.
    import csv
    import io
    sd = load_session(raw_path, "parquet")
    buf = io.StringIO()
    writer = csv.writer(buf)
    from .session_data import RAW_COLUMNS
    writer.writerow(RAW_COLUMNS)
    for i in range(sd.n):
        writer.writerow([sd.columns[c][i] for c in RAW_COLUMNS])
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_safe(row["name"])}.csv"'},
    )


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or "session"


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
@app.get("/api/compare")
async def api_compare(a: int, b: int, colour_by: str = "speed") -> Dict[str, Any]:
    from .comparison import compare
    row_a = _session_or_404(a)
    row_b = _session_or_404(b)
    sd_a = _load_or_404(row_a)
    sd_b = _load_or_404(row_b)
    return compare(sd_a, sd_b, _meta(row_a), _meta(row_b), colour_by)


def _meta(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"],
        "car_ordinal": row.get("car_ordinal"), "car_class": row.get("car_class"),
        "car_pi": row.get("car_pi"), "drivetrain": row.get("drivetrain"),
        "best_lap": row.get("best_lap"), "frame_count": row.get("frame_count"),
    }


# ---------------------------------------------------------------------------
# Packet debug
# ---------------------------------------------------------------------------
@app.get("/api/debug/last")
async def debug_last() -> Dict[str, Any]:
    st = _state()
    raw = st.hub.latest_raw
    fields = packet.field_debug(raw) if raw is not None else []
    return {
        "have_packet": raw is not None,
        "packet_size": packet.FH6_PACKET_SIZE,
        "received_size": len(raw) if raw is not None else None,
        "format": packet.FH6_FORMAT,
        "fields": fields,
    }


@app.get("/api/debug/spec")
async def debug_spec() -> Dict[str, Any]:
    return {
        "packet_size": packet.FH6_PACKET_SIZE,
        "format": packet.FH6_FORMAT,
        "field_offsets": packet.FIELD_OFFSETS,
        "field_names": packet.FIELD_NAMES,
    }


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------
def _page(name: str) -> HTMLResponse:
    path = STATIC_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="page not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
async def page_index() -> HTMLResponse:
    return _page("index.html")


@app.get("/sessions", response_class=HTMLResponse)
async def page_sessions() -> HTMLResponse:
    return _page("sessions.html")


@app.get("/analysis", response_class=HTMLResponse)
async def page_analysis() -> HTMLResponse:
    return _page("analysis.html")


@app.get("/compare", response_class=HTMLResponse)
async def page_compare() -> HTMLResponse:
    return _page("compare.html")


@app.get("/debug", response_class=HTMLResponse)
async def page_debug() -> HTMLResponse:
    return _page("debug.html")


# Service worker must be served from root scope to control the whole app.
@app.get("/sw.js")
async def service_worker() -> Response:
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404)
    return Response(path.read_text(encoding="utf-8"), media_type="application/javascript")


@app.get("/manifest.webmanifest")
async def manifest() -> Response:
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404)
    return Response(path.read_text(encoding="utf-8"), media_type="application/manifest+json")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
