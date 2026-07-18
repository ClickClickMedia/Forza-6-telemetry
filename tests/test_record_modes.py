"""Tests for the recording-mode state machine (event / motion / manual)."""

from __future__ import annotations

import time
from pathlib import Path

from app.database import Database
from app.packet import FIELD_NAMES, parse, pack
from app.recorder import Recorder


def _frame(**overrides):
    values = {name: 0 for name in FIELD_NAMES}
    values.update(overrides)
    return parse(pack(values))


def _rec(tmp_path: Path, mode: str, **kw) -> tuple:
    db = Database(tmp_path / "t.db")
    rec = Recorder(db, tmp_path / "sessions", raw_format="csv",
                   record_mode=mode, keep_min_s=0.0, **kw)
    return db, rec


def test_event_mode_ignores_free_roam_driving(tmp_path: Path):
    db, rec = _rec(tmp_path, "event")
    t = time.monotonic()
    for i in range(300):  # 5 s of ordinary driving, no event signals
        rec.feed(_frame(IsRaceOn=1, Speed=40, DistanceTraveled=500 + i), t)
        t += 1 / 60
    assert rec.active_session_id is None
    db.close()


def test_event_mode_starts_when_staged_negative(tmp_path: Path):
    db, rec = _rec(tmp_path, "event")
    t = time.monotonic()
    rec.feed(_frame(IsRaceOn=1, Speed=30, DistanceTraveled=800), t)
    assert rec.active_session_id is None
    rec.feed(_frame(IsRaceOn=1, Speed=0, DistanceTraveled=-238.0), t + 0.1)
    assert rec.active_session_id is not None  # staged at the line -> armed
    db.close()


def test_event_mode_starts_on_lap_fields(tmp_path: Path):
    db, rec = _rec(tmp_path, "event")
    t = time.monotonic()
    rec.feed(_frame(IsRaceOn=1, Speed=50, DistanceTraveled=900, CurrentLap=12.5), t)
    assert rec.active_session_id is not None  # joined a circuit event
    db.close()


def test_event_mode_zero_hold_staging(tmp_path: Path):
    db, rec = _rec(tmp_path, "event")
    t = time.monotonic()
    for _ in range(59):
        rec.feed(_frame(IsRaceOn=1, Speed=0, DistanceTraveled=0.0), t)
        t += 1 / 60
    assert rec.active_session_id is None  # not held long enough yet
    rec.feed(_frame(IsRaceOn=1, Speed=0, DistanceTraveled=0.0), t)
    assert rec.active_session_id is not None  # 60-frame zero hold = staged
    db.close()


def test_event_mode_closes_on_snap_but_survives_restart(tmp_path: Path):
    db, rec = _rec(tmp_path, "event")
    t = time.monotonic()
    rec.feed(_frame(IsRaceOn=1, Speed=0, DistanceTraveled=-238.0), t)
    sid = rec.active_session_id
    assert sid is not None
    # Run the route.
    for i in range(120):
        t += 1 / 60
        rec.feed(_frame(IsRaceOn=1, Speed=50, DistanceTraveled=i * 30.0), t)
    # Restart: snap back to staged negative — must NOT close (grace window).
    t += 1 / 60
    rec.feed(_frame(IsRaceOn=1, Speed=0, DistanceTraveled=-238.0), t)
    for _ in range(30):
        t += 1 / 60
        rec.feed(_frame(IsRaceOn=1, Speed=0, DistanceTraveled=-238.0), t)
    assert rec.active_session_id == sid, "restart must not split the session"
    # Second run, then event exit: snap to ~0 with no re-staging.
    for i in range(120):
        t += 1 / 60
        rec.feed(_frame(IsRaceOn=1, Speed=50, DistanceTraveled=i * 30.0), t)
    t += 1 / 60
    rec.feed(_frame(IsRaceOn=1, Speed=20, DistanceTraveled=120.0), t)  # snap
    for _ in range(400):  # > 5 s grace of ordinary roaming
        t += 1 / 60
        rec.feed(_frame(IsRaceOn=1, Speed=20, DistanceTraveled=130.0 + t % 7), t)
    assert rec.active_session_id is None, "event exit should close the session"
    db.close()


def test_manual_session_honours_stationary_timeout(tmp_path: Path):
    db, rec = _rec(tmp_path, "manual", stationary_timeout_s=30.0)
    t = time.monotonic()
    rec.start_manual()
    rec.feed(_frame(IsRaceOn=1, Speed=10, DistanceTraveled=100), t)
    assert rec.active_session_id is not None
    # Sit still (not staged — distance nonzero) past the timeout.
    for i in range(40):
        t += 1.0
        rec.feed(_frame(IsRaceOn=1, Speed=0, DistanceTraveled=100), t)
    assert rec.active_session_id is None, "walk-away net applies to manual too"
    db.close()


def test_manual_mode_never_autostarts(tmp_path: Path):
    db, rec = _rec(tmp_path, "manual")
    t = time.monotonic()
    rec.feed(_frame(IsRaceOn=1, Speed=60, DistanceTraveled=-238.0), t)
    rec.feed(_frame(IsRaceOn=1, Speed=60, CurrentLap=5.0), t + 0.1)
    assert rec.active_session_id is None
    db.close()
