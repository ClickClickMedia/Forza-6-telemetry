"""Tests for the car-name registry and its use in exports."""

from __future__ import annotations

from pathlib import Path

from app.database import Database


def test_car_name_set_get_overwrite_clear(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    assert db.get_car_name(3198) is None
    assert db.get_car_name(None) is None

    db.set_car_name(3198, "'69 Camaro SS, 6.2L swap")
    assert db.get_car_name(3198) == "'69 Camaro SS, 6.2L swap"

    db.set_car_name(3198, "2020 GR Supra")
    assert db.get_car_name(3198) == "2020 GR Supra"
    assert db.car_names() == {3198: "2020 GR Supra"}

    db.set_car_name(3198, "   ")  # blank clears
    assert db.get_car_name(3198) is None
    db.close()


def test_markdown_uses_car_name_when_set():
    from app.tuning_export import build_markdown
    from tests.test_laps import _synthetic_session

    sd = _synthetic_session(seconds=30.0)
    meta = {"name": "S", "car_ordinal": 2145, "car_class": 5, "car_pi": 798,
            "drivetrain": 1, "cylinders": 8, "created_at": "2026-07-18",
            "notes": "", "car_name": "2020 Toyota GR Supra"}
    md = build_markdown(sd, meta, "1.1.0")
    assert "**2020 Toyota GR Supra** (Forza ordinal 2145)" in md
    assert "replace with the actual car name" not in md

    meta["car_name"] = None
    md2 = build_markdown(sd, meta, "1.1.0")
    assert "Unknown car — ordinal 2145" in md2
    assert "name it once on the Analysis" in md2
