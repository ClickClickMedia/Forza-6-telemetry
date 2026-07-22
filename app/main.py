"""FastAPI application: HTTP API, WebSocket live feed, and static dashboard.

Wires together the UDP receiver, telemetry hub, recorder and SQLite database
under a single asyncio lifespan, and serves the mobile dashboard and its
analysis / comparison / debug pages.
"""

from __future__ import annotations

import asyncio
import json
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
    stored_forward = state.db.get_setting("forward_to")
    if stored_forward is None:
        stored_forward = settings.forward_to
    # UI-changed settings persist in the DB and override env defaults.
    stored_mode = state.db.get_setting("record_mode") or settings.record_mode
    state.recorder = Recorder(
        db=state.db,
        sessions_dir=settings.sessions_dir,
        idle_timeout_s=settings.session_idle_timeout_s,
        raw_format=settings.raw_format,
        record_mode=stored_mode,
        stationary_timeout_s=settings.stationary_timeout_s,
        keep_min_s=settings.keep_min_s,
        max_data_mb=settings.max_data_mb,
    )

    def _compute_best_after_close(session_id: int, raw_path, raw_format) -> None:
        """Background: derive best lap/run time so the sessions list shows a
        real number even for events with no wire lap fields (time attacks)."""
        import threading

        def work() -> None:
            try:
                from .laps import compact_summary, lap_report
                sd = load_session(raw_path, raw_format)
                rep = lap_report(sd)
                best = rep.get("best_lap_s")
                summary = compact_summary(rep)
                if summary:
                    state.db.set_session_summary(
                        session_id, json.dumps(summary))
                if best:
                    state.db.update_best_lap(session_id, best)
                    log.info("best time computed",
                             extra={"extra": {"session_id": session_id,
                                              "best_lap_s": best}})
            except Exception:
                log.exception("post-close best-time computation failed")

        threading.Thread(target=work, name=f"best-{session_id}", daemon=True).start()

    state.recorder.on_closed = _compute_best_after_close

    def _backfill_summaries() -> None:
        """One-time catch-up: sessions recorded before the lineage feature
        get their compact summary computed in the background so tune
        comparisons work on existing data. Skips anything unreadable."""
        import threading

        def work() -> None:
            from .laps import compact_summary, lap_report
            todo = state.db.sessions_missing_summary()
            done = 0
            for sid in todo:
                row = state.db.get_session(sid)
                if not row or not row.get("raw_path"):
                    continue
                path = settings.sessions_dir / row["raw_path"]
                if not path.exists():
                    continue
                try:
                    rep = lap_report(load_session(path, row.get("raw_format", "csv")))
                    summary = compact_summary(rep)
                    if summary:
                        state.db.set_session_summary(sid, json.dumps(summary))
                        done += 1
                    time.sleep(1.0)  # stay out of the live loop's way
                except Exception:
                    log.exception("summary backfill failed",
                                  extra={"extra": {"session_id": sid}})
            if done:
                log.info("session summaries backfilled",
                         extra={"extra": {"count": done}})

        threading.Thread(target=work, name="summary-backfill",
                         daemon=True).start()

    _backfill_summaries()
    state.hub.on_frame = state.recorder.feed
    state.receiver = UDPReceiver(state.hub, settings.udp_host, settings.udp_port)
    if stored_forward:
        try:
            state.receiver.set_forward(stored_forward)
        except ValueError:
            log.warning("ignoring invalid forward target",
                        extra={"extra": {"forward_to": stored_forward}})

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
    from . import __version__
    from .config import lan_ip
    st = _state()
    return {
        "status": "ok",
        "version": __version__,
        "lan_ip": lan_ip(),
        "udp_port": settings.udp_port,
        "http_port": settings.http_port,
        "packet_size": packet.FH6_PACKET_SIZE,
        "recording": st.recorder.active_session_id is not None,
    }


@app.post("/api/update-check")
async def update_check() -> Dict[str, Any]:
    """User-initiated only: the sole network call this app can make.

    Triggered by the 'Check for updates' button on the Debug page — never
    automatically. Queries GitHub's public releases API.
    """
    import json as _json
    import urllib.request

    from . import __version__
    url = ("https://api.github.com/repos/ClickClickMedia/"
           "Forza-6-telemetry/releases/latest")
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"fh6-telemetry/{__version__}",
        })
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _json.load(resp)
    except Exception as exc:  # noqa: BLE001 - offline is a normal state
        return {"ok": False, "error": f"could not reach GitHub ({exc})",
                "current": __version__}
    latest = str(data.get("tag_name") or "").lstrip("v")

    def _ver(v: str):
        try:
            return tuple(int(p) for p in v.split("."))
        except ValueError:
            return (0,)

    return {
        "ok": True,
        "current": __version__,
        "latest": latest,
        "update_available": bool(latest) and _ver(latest) > _ver(__version__),
        "url": data.get("html_url"),
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


class SettingsBody(BaseModel):
    record_mode: Optional[str] = None
    forward_to: Optional[str] = None


@app.get("/api/settings")
async def get_settings() -> Dict[str, Any]:
    st = _state()
    return {
        "record_mode": st.recorder.record_mode,
        "stationary_timeout_s": st.recorder.stationary_timeout_s,
        "idle_timeout_s": st.recorder.idle_timeout_s,
        "forward": st.receiver.forward_status(),
    }


@app.put("/api/settings")
async def put_settings(body: SettingsBody) -> Dict[str, Any]:
    st = _state()
    if body.record_mode is not None:
        mode = body.record_mode.lower()
        if mode not in ("event", "motion", "manual"):
            raise HTTPException(status_code=422, detail="record_mode must be event|motion|manual")
        st.recorder.record_mode = mode
        st.db.set_setting("record_mode", mode)
    if body.forward_to is not None:
        try:
            applied = st.receiver.set_forward(body.forward_to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if body.forward_to.strip() and applied is None:
            raise HTTPException(status_code=422,
                                detail="forward target must be ip:port")
        st.db.set_setting("forward_to", applied or "")
    return await get_settings()


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


def _resolve_car_name(ordinal: Optional[int]) -> Optional[str]:
    """User registry first, then the community ordinal seed."""
    from . import car_db
    return _state().db.get_car_name(ordinal) or car_db.lookup(ordinal)


@app.get("/api/sessions")
async def list_sessions() -> Dict[str, Any]:
    st = _state()
    sessions = st.db.list_sessions()
    total_bytes = 0
    for s in sessions:
        s["car_name"] = _resolve_car_name(s.get("car_ordinal"))
        p = settings.sessions_dir / (s.get("raw_path") or "")
        try:
            s["raw_bytes"] = p.stat().st_size if p.exists() else 0
        except OSError:
            s["raw_bytes"] = 0
        total_bytes += s["raw_bytes"]
    return {
        "sessions": sessions,
        "storage": {
            "total_bytes": total_bytes,
            "data_dir": str(settings.data_dir),
            "max_data_mb": settings.max_data_mb or None,
        },
    }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: int) -> Dict[str, Any]:
    st = _state()
    row = _session_or_404(session_id)
    row["markers"] = st.db.list_markers(session_id)
    row["car_name"] = _resolve_car_name(row.get("car_ordinal"))
    return row


def _garage_aggregate(sessions, name_for, count_for, extra_named=None):
    """Pure aggregation for the garage view: one entry per car_ordinal.

    ``sessions`` newest-first (as list_sessions returns). ``name_for`` and
    ``count_for`` are callables ordinal -> name / tune-version count.
    ``extra_named`` maps ordinal -> name for cars that are named or tuned
    but have no sessions yet (they still belong in the garage).
    """
    by_ordinal: Dict[int, Dict[str, Any]] = {}
    for s in sessions:
        ordv = s.get("car_ordinal")
        if not ordv:  # skip menu / zero-identity sessions
            continue
        g = by_ordinal.get(ordv)
        if g is None:  # first (newest) session sets identity fields
            g = by_ordinal[ordv] = {
                "ordinal": ordv, "car_name": name_for(ordv),
                "car_class": s.get("car_class"), "car_pi": s.get("car_pi"),
                "drivetrain": s.get("drivetrain"),
                "cylinders": s.get("cylinders"),
                "session_count": 0, "best_lap": None,
                "last_driven": s.get("created_at"),
                "first_driven": s.get("created_at"),
            }
        g["session_count"] += 1
        bl = s.get("best_lap")
        if bl and (g["best_lap"] is None or bl < g["best_lap"]):
            g["best_lap"] = bl
        created = s.get("created_at")
        if created:
            if not g["last_driven"] or created > g["last_driven"]:
                g["last_driven"] = created
            if not g["first_driven"] or created < g["first_driven"]:
                g["first_driven"] = created
    for ordv, name in (extra_named or {}).items():
        by_ordinal.setdefault(ordv, {
            "ordinal": ordv, "car_name": name, "car_class": None,
            "car_pi": None, "drivetrain": None, "cylinders": None,
            "session_count": 0, "best_lap": None,
            "last_driven": None, "first_driven": None,
        })
    for ordv, g in by_ordinal.items():
        g["tune_versions"] = count_for(ordv)
    return sorted(by_ordinal.values(),
                  key=lambda g: (g["last_driven"] or ""), reverse=True)


@app.get("/api/garage")
async def garage() -> Dict[str, Any]:
    """The player's cars: one entry per car they've driven or named, with
    session count, saved tune versions, best lap and when last driven.
    Aggregated from the sessions, cars and setups tables — no new storage."""
    st = _state()
    return {"cars": _garage_aggregate(
        st.db.list_sessions(), _resolve_car_name, st.db.count_setups,
        extra_named=st.db.car_names())}


# ---------------------------------------------------------------------------
# Saved tune setups (user-entered tuning-screen values, versioned per car)
# ---------------------------------------------------------------------------
class SetupBody(BaseModel):
    car_ordinal: int
    label: Optional[str] = None
    data: Dict[str, Any] = {}


@app.get("/api/setups")
async def list_setups(ordinal: int) -> Dict[str, Any]:
    import json as _json
    rows = _state().db.list_setups(ordinal)
    for r in rows:
        try:
            r["data"] = _json.loads(r.pop("data") or "{}")
        except ValueError:
            r["data"] = {}
    return {"setups": rows}


@app.post("/api/setups")
async def create_setup(body: SetupBody) -> Dict[str, Any]:
    import json as _json
    from datetime import datetime, timezone
    st = _state()
    label = (body.label or "").strip()
    if not label:
        label = f"v{st.db.count_setups(body.car_ordinal) + 1}"
    # One identity store: a setup that names the car also registers it, so
    # reports and session lists pick it up without a second step.
    car_text = str((body.data or {}).get("car_text") or "").strip()
    if car_text and not st.db.get_car_name(body.car_ordinal):
        st.db.set_car_name(body.car_ordinal, car_text)
    sid = st.db.add_setup(
        body.car_ordinal, label,
        datetime.now(timezone.utc).isoformat(),
        _json.dumps(body.data or {}),
    )
    return {"ok": True, "id": sid, "label": label}


# ---------------------------------------------------------------------------
# Car names (Forza only broadcasts a numeric ordinal; players name cars once)
# ---------------------------------------------------------------------------
class CarNameBody(BaseModel):
    name: str = ""


@app.get("/api/cars")
async def list_car_names() -> Dict[str, Any]:
    from . import car_db
    return {"cars": _state().db.car_names(), "seed_meta": car_db.seed_meta()}


@app.put("/api/cars/{ordinal}")
async def put_car_name(ordinal: int, body: CarNameBody) -> Dict[str, Any]:
    st = _state()
    st.db.set_car_name(ordinal, body.name)
    return {"ok": True, "ordinal": ordinal, "name": body.name.strip() or None}


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


@app.delete("/api/sessions")
async def delete_all_sessions() -> Dict[str, Any]:
    """Bulk wipe of every recorded session and its raw file. Car names,
    tune setups and settings survive — they are per-car assets, not
    session data. Refused while a recording is open."""
    st = _state()
    if st.recorder and st.recorder.active_session_id is not None:
        raise HTTPException(status_code=409,
                            detail="recording in progress — stop it first")
    deleted = 0
    freed = 0
    for s in st.db.list_sessions():
        raw_path = st.db.delete_session(s["id"])
        if raw_path:
            p = settings.sessions_dir / raw_path
            try:
                if p.exists():
                    freed += p.stat().st_size
                p.unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete raw file",
                            extra={"extra": {"path": str(p)}})
        deleted += 1
    log.info("all sessions deleted",
             extra={"extra": {"deleted": deleted, "freed_bytes": freed}})
    return {"ok": True, "deleted": deleted, "freed_bytes": freed}


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


@app.get("/api/sessions/{session_id}/laps")
async def session_laps(session_id: int) -> Dict[str, Any]:
    from .laps import compact_summary, lap_report
    row = _session_or_404(session_id)
    sd = _load_or_404(row)
    result = lap_report(sd)
    result["session_meta"] = _meta(row)
    # Backfill the stored best so the sessions list shows run/lap times even
    # for events whose wire lap fields are empty (free-roam time attacks).
    best = result.get("best_lap_s")
    if best and row.get("best_lap") != best:
        _state().db.update_best_lap(session_id, best)
    summary = compact_summary(result)
    if summary:
        _state().db.set_session_summary(session_id, json.dumps(summary))
    return result


def _lineage_for(row: Dict[str, Any]) -> list:
    """Earlier sessions with the same car, decorated with their stored
    summaries — the report's tune-comparison baseline."""
    ordinal = row.get("car_ordinal")
    if not ordinal:
        return []
    out = []
    for prev in _state().db.sessions_for_car(ordinal, row["id"]):
        try:
            prev["summary"] = json.loads(prev.get("summary_json") or "null")
        except (TypeError, ValueError):
            prev["summary"] = None
        out.append(prev)
    return out


@app.get("/api/sessions/{session_id}/tuning.md")
async def session_tuning_md(session_id: int, download: int = 0,
                            setup_id: Optional[int] = None,
                            mode: str = "full",
                            style: str = "detailed") -> Response:
    import json as _json

    from . import __version__
    from .tuning_export import build_markdown
    st = _state()
    row = _session_or_404(session_id)
    row["car_name"] = _resolve_car_name(row.get("car_ordinal"))
    setup = None
    prev_setup = None
    if setup_id is not None:
        srow = st.db.get_setup(setup_id)
        if srow is None:
            raise HTTPException(status_code=404, detail="setup not found")
        setup = {"label": srow["label"],
                 "data": _json.loads(srow["data"] or "{}")}
        # The immediately-preceding revision for this car: its diff tells
        # the analyst exactly which variables this session is testing.
        older = [s for s in st.db.list_setups(srow["car_ordinal"])
                 if s["id"] < setup_id]
        if older:
            prev = older[0]  # list is newest-first
            prev_setup = {"label": prev["label"],
                          "data": _json.loads(prev["data"] or "{}")}
    sd = _load_or_404(row)
    saved_context = None
    if mode == "quick" and setup is None and row.get("car_ordinal"):
        # Quick copies inherit saved NON-tune context — the tool shouldn't
        # pretend it doesn't know the discipline or assists it has stored.
        latest = st.db.list_setups(row["car_ordinal"])
        if latest:
            ldata = _json.loads(latest[0]["data"] or "{}")
            saved_context = {k: ldata.get(k) for k in
                             ("discipline", "abs_assist", "tcs_assist",
                              "gearbox", "car_text") if ldata.get(k)}
    md = build_markdown(sd, row, __version__, setup=setup,
                        lineage=_lineage_for(row),
                        variant=(mode if mode in ("data", "quick", "experiment")
                                 else "full"),
                        verbose=(style != "compact"),
                        prev_setup=prev_setup,
                        saved_context=saved_context)
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{_safe(row["name"])}-tuning.md"'
        )
    return PlainTextResponse(md, media_type="text/markdown", headers=headers)


@app.get("/api/sessions/{session_id}/package.zip")
async def session_package(session_id: int) -> Response:
    """Complete session bundle: report, lap summary, section data, raw
    telemetry, metadata and the latest setup — one attachment for an AI,
    an archive, or a bug report."""
    import io
    import zipfile

    from . import __version__
    from .sections import detect_sections
    from .tuning_export import build_laps_csv, build_markdown
    st = _state()
    row = _session_or_404(session_id)
    row["car_name"] = _resolve_car_name(row.get("car_ordinal"))
    sd = _load_or_404(row)
    setup = None
    if row.get("car_ordinal"):
        setups = st.db.list_setups(row["car_ordinal"])
        if setups:
            setup = {"label": setups[0]["label"],
                     "data": json.loads(setups[0]["data"] or "{}")}
    from .laps import lap_report
    md = build_markdown(sd, row, __version__, setup=setup,
                        lineage=_lineage_for(row))
    rep = lap_report(sd)
    windows = [(l["t_start"], l["t_end"]) for l in rep.get("laps", [])
               if (l.get("lap") is not None or l.get("run"))
               and l.get("t_start") is not None]
    sec = detect_sections(sd, timed_windows=windows or None) or {}
    sdata = (setup or {}).get("data") or {}
    metadata = {k: row.get(k) for k in ("id", "name", "created_at",
                                        "ended_at", "frame_count",
                                        "car_ordinal", "car_name",
                                        "car_class", "car_pi", "drivetrain",
                                        "best_lap", "notes")}
    metadata.update({
        "discipline": sdata.get("discipline"),
        "assists": {"abs": sdata.get("abs_assist"),
                    "tcs": sdata.get("tcs_assist")},
        "setup_source": "saved_latest" if setup else None,
        "setup_snapshot_at_recording": False,
        "timed_windows_s": windows,
        "laps": [{"lap": l.get("lap"), "run": l.get("run"),
                  "time_s": l.get("time_s"),
                  "valid": bool(l.get("complete"))
                  and not l.get("rewind_affected"),
                  "rewind_affected": bool(l.get("rewind_affected")),
                  "t_start": l.get("t_start"), "t_end": l.get("t_end")}
                 for l in rep.get("laps", [])],
        "section_evidence_scope": "entire recording (see timed_windows_s)",
    })
    raw_path = settings.sessions_dir / (row.get("raw_path") or "")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("session-report.md", md)
        zf.writestr("laps.csv", build_laps_csv(sd))
        zf.writestr("sections.json", json.dumps(sec, indent=1))
        zf.writestr("metadata.json", json.dumps(metadata, indent=1,
                                                default=str))
        if setup:
            zf.writestr("setup.json", json.dumps(setup, indent=1))
        if raw_path.exists():
            zf.write(raw_path, "raw-telemetry.csv")
        zf.writestr("README.txt", (
            "Forza Horizon 6 telemetry session package\n"
            f"Generated by Forza-6-telemetry v{__version__}\n\n"
            "session-report.md  - full evidence report (AI-ready)\n"
            "laps.csv           - one row per detected lap/run\n"
            "sections.json      - every classified corner section\n"
            "raw-telemetry.csv  - full wire capture (~60 Hz, all channels)\n"
            "metadata.json      - session identity, lap validity, timed\n"
            "                     windows, and setup provenance flags\n"
            "setup.json         - LATEST saved tune for this car; it may\n"
            "                     postdate the recording (not a snapshot\n"
            "                     taken at recording time)\n"))
    return Response(
        buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{_safe(row["name"])}-package.zip"'})


@app.get("/api/sessions/{session_id}/sections.json")
async def session_sections(session_id: int, download: int = 0) -> Response:
    """Machine-readable section evidence (hairpins/turns/sweepers/
    transfers/straights with every instance) — the structured companion
    to the Markdown report's representative samples."""
    from .laps import lap_report
    from .sections import detect_sections
    row = _session_or_404(session_id)
    sd = _load_or_404(row)
    rep = lap_report(sd)
    windows = [(l["t_start"], l["t_end"]) for l in rep.get("laps", [])
               if (l.get("lap") is not None or l.get("run"))
               and l.get("t_start") is not None]
    sec = detect_sections(sd, timed_windows=windows or None) or {}
    sec["session"] = {"id": session_id, "name": row.get("name")}
    sec["timed_windows"] = windows
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{_safe(row["name"])}-sections.json"'
        )
    return Response(json.dumps(sec, indent=1), media_type="application/json",
                    headers=headers)


@app.get("/api/sessions/{session_id}/laps.csv")
async def session_laps_csv(session_id: int) -> Response:
    from .tuning_export import build_laps_csv
    row = _session_or_404(session_id)
    sd = _load_or_404(row)
    return PlainTextResponse(
        build_laps_csv(sd),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{_safe(row["name"])}-laps.csv"'},
    )


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
        "car_name": _resolve_car_name(row.get("car_ordinal")),
        "notes": row.get("notes", ""),
        "created_at": row.get("created_at"),
        "cylinders": row.get("cylinders"),
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


@app.get("/garage", response_class=HTMLResponse)
async def page_garage() -> HTMLResponse:
    return _page("garage.html")


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
