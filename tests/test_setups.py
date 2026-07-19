"""Tests for saved setups and their appearance in the tuning export."""

from __future__ import annotations

import json
from pathlib import Path

from app.database import Database
from app.tuning_export import build_markdown
from tests.test_laps import _synthetic_session

META = {"name": "S", "car_ordinal": 3726, "car_class": 4, "car_pi": 703,
        "drivetrain": 0, "cylinders": 4, "created_at": "2026-07-18",
        "notes": "", "car_name": "2023 Acura Integra A-Spec"}


def test_setup_storage_versions_per_car(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    a = db.add_setup(3726, "v1", "2026-07-18T00:00:00", json.dumps({"tp_f": "2.2"}))
    b = db.add_setup(3726, "v2", "2026-07-18T01:00:00", json.dumps({"tp_f": "2.1"}))
    db.add_setup(9999, "other-car", "2026-07-18T02:00:00", "{}")
    rows = db.list_setups(3726)
    assert [r["id"] for r in rows] == [b, a]  # newest first
    assert db.count_setups(3726) == 2
    assert json.loads(db.get_setup(a)["data"])["tp_f"] == "2.2"
    db.close()


def test_export_embeds_setup_values():
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "Touge v2", "data": {
        "car_text": "2023 Acura Integra, race build",
        "drivetrain": "FWD", "gearbox": "Race",
        "tp_f": "2.2 bar", "tp_r": "2.0 bar", "final": "3.42",
        "arb_f": "27.6", "arb_r": "23.1",
        "goal": "touge leader A class",
    }}
    md = build_markdown(sd, META, "2.1.0", setup=setup)
    assert "## My setup — Touge v2" in md
    assert "2.2 bar" in md and "3.42" in md
    assert "Drivetrain (as built): **FWD**" in md
    assert "touge leader A class" in md
    # The blank fill-in block must be gone.
    assert "fill in before asking the AI" not in md


def test_export_renders_declared_assists():
    """ABS/TCS aren't on the wire — when the user declares them, the report
    must state them AND tell the AI how to weigh lock-threshold time."""
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "v1", "data": {
        "car_text": "Pantera", "abs_assist": "On", "tcs_assist": "Off",
    }}
    md = build_markdown(sd, META, "2.1.7", setup=setup)
    assert "Assists: **ABS on, traction control off**" in md
    assert "the assist modulating" in md
    # Every report carries both brake numbers, clearly told apart.
    assert "Sustained brake locks" in md
    assert "ABS-style slip modulation" in md


def test_export_data_only_mode():
    sd = _synthetic_session(seconds=30.0)
    md = build_markdown(sd, META, "2.1.0", setup=None, include_fill_in=False)
    assert "Telemetry-only export" in md
    assert "fill in before asking the AI" not in md
    # Telemetry sections still present.
    assert "## Balance & traction" in md
