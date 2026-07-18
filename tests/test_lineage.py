"""Tests for the tune-lineage feature: session summaries, the DB column
migration, and the before/after table in the export."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.database import Database
from app.laps import compact_summary, lap_report
from app.tuning_export import build_markdown
from tests.test_laps import _synthetic_session
from tests.test_setups import META


def _add_finalized(db: Database, ordinal: int, name: str,
                   notes: str = "", summary: dict = None) -> int:
    sid = db.create_session(name, "2026-07-18T00:00:00", True, f"{name}.csv", "csv")
    db.finalize_session(sid, "2026-07-18T01:00:00", 100,
                        {"car_ordinal": ordinal})
    if notes:
        db.set_notes(sid, notes)
    if summary is not None:
        db.set_session_summary(sid, json.dumps(summary))
    return sid


def test_migration_adds_summary_column_to_old_db(tmp_path: Path):
    """Databases created before the lineage feature must gain the column on
    open — CREATE TABLE IF NOT EXISTS alone never retrofits it."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE sessions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               name TEXT NOT NULL, created_at TEXT NOT NULL, ended_at TEXT,
               started_manually INTEGER NOT NULL DEFAULT 0, raw_path TEXT,
               raw_format TEXT NOT NULL DEFAULT 'csv',
               frame_count INTEGER NOT NULL DEFAULT 0, car_ordinal INTEGER,
               car_class INTEGER, car_pi INTEGER, drivetrain INTEGER,
               cylinders INTEGER, car_group INTEGER, best_lap REAL,
               notes TEXT NOT NULL DEFAULT '');
           INSERT INTO sessions (name, created_at) VALUES ('legacy', 't');"""
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.set_session_summary(1, json.dumps({"best_s": 90.0}))
    rows = db.sessions_for_car(0, -1)  # no match expected, just no crash
    assert rows == []
    db.close()


def test_sessions_for_car_orders_and_excludes(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    a = _add_finalized(db, 3198, "run A", summary={"best_s": 96.0})
    b = _add_finalized(db, 3198, "run B", notes="official 1:34.612")
    _add_finalized(db, 9999, "other car")
    c = _add_finalized(db, 3198, "run C (current)")
    rows = db.sessions_for_car(3198, exclude_id=c)
    assert [r["name"] for r in rows] == ["run B", "run A"]  # newest first
    assert rows[0]["notes"] == "official 1:34.612"
    assert json.loads(rows[1]["summary_json"])["best_s"] == 96.0
    assert a not in [c]
    missing = db.sessions_missing_summary()
    assert b in missing and c in missing and a not in missing
    db.close()


def test_compact_summary_from_report():
    rep = lap_report(_synthetic_session(seconds=30.0))
    s = compact_summary(rep)
    assert s is not None
    for key in ("usi", "spin_total_s", "lock_s", "temp_f_c", "max_kmh",
                "shifts", "duration_s"):
        assert key in s
    # Storable as-is.
    assert json.loads(json.dumps(s)) == s


def test_export_renders_lineage_table():
    sd = _synthetic_session(seconds=30.0)
    lineage = [{
        "name": "AWD baseline", "created_at": "2026-07-18T10:00:00",
        "notes": "official 1:35.764 — baseline", "best_lap": 186.031,
        "summary": {"best_s": 186.031, "timing": "runs", "usi": 0.289,
                    "spin_total_s": 35.2, "spin_multi_s": 18.4,
                    "lock_s": 0.4, "temp_f_c": 95.0, "temp_r_c": 88.0,
                    "max_kmh": 231.0, "shifts": 81},
    }]
    md = build_markdown(sd, META, "2.1.8", lineage=lineage)
    assert "## Tune lineage — earlier sessions with this car" in md
    assert "AWD baseline" in md and "3:06.031 (run)" in md
    assert "+0.289" in md and "35.2 (18.4 multi)" in md
    assert "official 1:35.764 — baseline" in md
    # The clock-first rule rides with the full report...
    assert "Judge tune changes by the clock first" in md
    # ...and the handling summary carries the character-not-success caveat.
    assert "never as a reason to revert a faster setup" in md


def test_data_only_export_is_actually_data_only():
    """'Copy data only' must carry numbers and lineage, but no AI prompt,
    no handling headline and no coaching."""
    sd = _synthetic_session(seconds=30.0)
    lineage = [{"name": "prev", "created_at": "2026-07-18", "notes": "",
                "best_lap": 94.6, "summary": {"best_s": 94.6, "usi": 0.331}}]
    md = build_markdown(sd, META, "2.1.8", setup=None,
                        include_fill_in=False, lineage=lineage)
    assert "Prompt for the AI" not in md
    assert "## Handling summary" not in md
    assert "Judge tune changes by the clock first" not in md
    assert "## Tune lineage" in md and "1:34.600" in md
    assert "## Balance & traction" in md
