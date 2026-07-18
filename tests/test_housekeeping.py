"""Tests for storage housekeeping: blip discard and the retention cap."""

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


def _drive(rec: Recorder, t0: float, n: int, **overrides) -> float:
    t = t0
    for _ in range(n):
        rec.feed(_frame(IsRaceOn=1, Speed=15, **overrides), t)
        t += 1 / 60
    return t


def test_blip_sessions_are_discarded(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rec = Recorder(db, tmp_path / "sessions", raw_format="csv", keep_min_s=5.0, record_mode="motion")
    t = time.monotonic()
    t = _drive(rec, t, 60)          # one second of driving = a blip
    rec.check_idle(t + 30)          # idle timeout closes it
    time.sleep(0.2)                 # let the writer thread finish
    assert db.list_sessions() == []
    assert list((tmp_path / "sessions").glob("*.csv")) == []
    db.close()


def test_manual_sessions_survive_blip_filter(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rec = Recorder(db, tmp_path / "sessions", raw_format="csv", keep_min_s=5.0, record_mode="motion")
    t = time.monotonic()
    rec.start_manual()
    t = _drive(rec, t, 60)          # short, but manual = user intent
    rec.stop_manual()
    time.sleep(0.2)
    assert len(db.list_sessions()) == 1
    db.close()


def test_retention_cap_prunes_oldest_disposable(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    # Tiny cap so the second session's close pushes usage over it.
    rec = Recorder(db, tmp_path / "sessions", raw_format="csv", record_mode="motion",
                   keep_min_s=1.0, max_data_mb=0.2)
    t = time.monotonic()

    t = _drive(rec, t, 400)                    # session 1 (~0.4 MB)
    rec.check_idle(t + 30)
    time.sleep(0.2)
    first = db.list_sessions()[0]["id"]

    rec.feed(_frame(IsRaceOn=0, Speed=0), t + 31)  # reset race edge
    t = _drive(rec, t + 32, 400)               # session 2
    rec.check_idle(t + 30)
    time.sleep(0.2)

    rows = db.list_sessions()
    ids = [r["id"] for r in rows]
    assert first not in ids, "oldest disposable session should be pruned"
    assert len(ids) == 1
    # And its raw file is gone with it.
    assert len(list((tmp_path / "sessions").glob("*.csv"))) == 1
    db.close()


def test_retention_cap_protects_renamed_sessions(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    rec = Recorder(db, tmp_path / "sessions", raw_format="csv", record_mode="motion",
                   keep_min_s=1.0, max_data_mb=0.2)
    t = time.monotonic()
    t = _drive(rec, t, 400)
    rec.check_idle(t + 30)
    time.sleep(0.2)
    first = db.list_sessions()[0]["id"]
    db.rename_session(first, "PB run — keep this")   # user renamed = protected

    rec.feed(_frame(IsRaceOn=0, Speed=0), t + 31)
    t = _drive(rec, t + 32, 400)
    rec.check_idle(t + 30)
    time.sleep(0.2)

    ids = [r["id"] for r in db.list_sessions()]
    assert first in ids, "renamed sessions must never be auto-pruned"
    db.close()
