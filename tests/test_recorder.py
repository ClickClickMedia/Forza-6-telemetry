"""Tests for session recording lifecycle and the synthetic generator."""

from __future__ import annotations

import time
from pathlib import Path

from app.database import Database
from app.packet import parse, pack, FIELD_NAMES, FH6_PACKET_SIZE
from app.recorder import Recorder
from app.synthetic import SyntheticDriver


def _frame(**overrides):
    values = {name: 0 for name in FIELD_NAMES}
    values.update(overrides)
    return parse(pack(values))


def test_auto_session_on_raceon(tmp_path):
    db = Database(tmp_path / "s.db")
    rec = Recorder(db, tmp_path / "sessions", idle_timeout_s=5.0, raw_format="csv")

    t = 100.0
    # First frame with IsRaceOn=0: no session.
    rec.feed(_frame(IsRaceOn=0, Speed=0), t)
    assert rec.active_session_id is None

    # Transition to IsRaceOn=1: session opens.
    t += 0.016
    rec.feed(_frame(IsRaceOn=1, Speed=10, CarOrdinal=42, NumCylinders=6), t)
    sid = rec.active_session_id
    assert sid is not None

    for _ in range(10):
        t += 0.016
        rec.feed(_frame(IsRaceOn=1, Speed=20, CarOrdinal=42, NumCylinders=6,
                        BestLap=90.5), t)

    # Idle timeout closes the session.
    rec.check_idle(t + 6.0)
    assert rec.active_session_id is None

    row = db.get_session(sid)
    assert row["frame_count"] >= 11
    assert row["car_ordinal"] == 42
    assert row["cylinders"] == 6
    assert row["ended_at"] is not None

    # Raw file exists with a header + rows.
    raw = tmp_path / "sessions" / row["raw_path"]
    assert raw.exists()
    lines = raw.read_text().strip().splitlines()
    assert lines[0].startswith("t_mono,t_wall,IsRaceOn")
    assert len(lines) >= 12
    db.close()


def test_manual_recording_and_marker(tmp_path):
    db = Database(tmp_path / "s.db")
    rec = Recorder(db, tmp_path / "sessions", raw_format="csv")

    res = rec.start_manual()
    assert res["ok"] is True

    t = 200.0
    rec.feed(_frame(IsRaceOn=0, Speed=5), t)  # manual arm opens on next frame
    sid = rec.active_session_id
    assert sid is not None

    m = rec.add_marker("apex", t)
    assert m["ok"] is True

    t += 0.1
    rec.feed(_frame(IsRaceOn=0, Speed=6), t)

    stop = rec.stop_manual()
    assert stop["ok"] is True
    assert rec.active_session_id is None

    markers = db.list_markers(sid)
    assert len(markers) == 1
    assert markers[0]["label"] == "apex"
    db.close()


def test_synthetic_packets_are_valid_324_bytes():
    driver = SyntheticDriver(hz=60.0)
    for _ in range(200):
        values = driver.step()
        data = pack(values)
        assert len(data) == FH6_PACKET_SIZE
        frame = parse(data)  # must parse cleanly
        assert frame.IsRaceOn == 1
        # Speed stays in a sane range.
        assert 0 <= frame.Speed < 120


def test_shutdown_flushes_open_session(tmp_path):
    db = Database(tmp_path / "s.db")
    rec = Recorder(db, tmp_path / "sessions", raw_format="csv")
    t = 300.0
    rec.feed(_frame(IsRaceOn=0), t)
    rec.feed(_frame(IsRaceOn=1, Speed=10), t + 0.016)
    sid = rec.active_session_id
    assert sid is not None
    rec.shutdown()
    assert rec.active_session_id is None
    assert db.get_session(sid)["ended_at"] is not None
    db.close()
