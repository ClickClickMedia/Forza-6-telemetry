"""Tests for lap segmentation, aggregates, and the tuning exports.

Uses the synthetic driver (which laps a 1 km oval) to produce realistic
multi-lap data end-to-end through the real pack/parse path.
"""

from __future__ import annotations

import numpy as np

from app.laps import lap_report
from app.packet import FIELD_NAMES, pack, parse
from app.session_data import SessionData
from app.synthetic import SyntheticDriver
from app.tuning_export import build_laps_csv, build_markdown


def _synthetic_session(seconds: float = 120.0, hz: float = 30.0) -> SessionData:
    driver = SyntheticDriver(hz=hz)
    cols = {name: [] for name in FIELD_NAMES}
    t_mono = []
    n = int(seconds * hz)
    for i in range(n):
        frame = parse(pack(driver.step()))  # exercise the real wire path
        for name in FIELD_NAMES:
            cols[name].append(getattr(frame, name))
        t_mono.append(i / hz)
    columns = {k: np.array(v, dtype=float) for k, v in cols.items()}
    columns["t_mono"] = np.array(t_mono)
    columns["t_wall"] = np.array(["" for _ in range(n)], dtype=object)
    return SessionData(columns, n)


def test_lap_report_segments_synthetic_laps():
    sd = _synthetic_session(seconds=120.0)
    rep = lap_report(sd)
    assert rep["has_laps"]
    complete = [l for l in rep["laps"] if l["complete"]]
    # ~40 m/s avg on a 1 km oval -> roughly 4-5 complete laps in 120 s.
    assert len(complete) >= 2
    for lap in complete:
        assert lap["time_s"] is not None and lap["time_s"] > 15
        assert lap["speed"]["max_kmh"] > lap["speed"]["avg_kmh"] > 0
        assert 0 <= lap["inputs"]["pct_full_throttle"] <= 100
    assert rep["best_lap_s"] == min(l["time_s"] for l in complete)
    assert rep["verdicts"]["balance"]["verdict"] in (
        "understeer", "oversteer", "neutral", "insufficient cornering data"
    )


def test_lap_report_free_roam_fallback():
    """A session with no lap transitions still gets a single stint."""
    sd = _synthetic_session(seconds=10.0)
    # Force LapNumber to zero across the board (free roam).
    sd.columns["LapNumber"] = np.zeros(sd.n)
    rep = lap_report(sd)
    assert not rep["has_laps"]
    assert len(rep["laps"]) == 1
    assert rep["laps"][0]["lap"] is None
    assert rep["session"]["distance_m"] > 0


def test_lap_report_rejects_rivals_restart_phantom():
    """Rivals resets LapNumber on a restart, leaving a ~0-distance segment that
    carries a stale (fast) LastLap. It must not be reported as a complete lap
    nor win best_lap (the '44.311' bug)."""
    sd = _synthetic_session(seconds=120.0)
    real_best = lap_report(sd)["best_lap_s"]
    assert real_best and real_best > 20  # synthetic 1 km oval laps ~25 s

    lap_no = sd.columns["LapNumber"].copy().astype(float)
    last = sd.columns["LastLap"].copy().astype(float)
    speed = sd.columns["Speed"].copy().astype(float)
    # Tail: reset to 0, then advance 0->1 with the car stopped (no distance)
    # and a stale LastLap of 18 s — faster than any real lap, but a phantom.
    lap_no[-20:-10] = 0.0
    lap_no[-10:] = 1.0
    speed[-25:] = 0.0
    last[-8:] = 18.0
    sd.columns["LapNumber"] = lap_no
    sd.columns["LastLap"] = last
    sd.columns["Speed"] = speed

    rep = lap_report(sd)
    complete_times = [l["time_s"] for l in rep["laps"]
                      if l["complete"] and l["time_s"]]
    assert 18.0 not in complete_times          # phantom rejected...
    assert rep["best_lap_s"] > 20              # ...and it did not win best_lap


def test_gearing_reports_on_power_shift_point():
    """The on-power shift point is exposed and sits at/above the plain average
    (part-throttle short-shifts drag the average down)."""
    sd = _synthetic_session(seconds=120.0)
    g = lap_report(sd)["session"]["gearing"]
    assert "shift_rpm_full_throttle" in g
    if g["shift_rpm_full_throttle"] and g["shift_rpm_avg"]:
        assert g["shift_rpm_full_throttle"] >= g["shift_rpm_avg"] - 1


def test_markdown_export_contains_key_sections():
    sd = _synthetic_session(seconds=60.0)
    meta = {"name": "Test Session", "car_ordinal": 2145, "car_class": 5,
            "car_pi": 798, "drivetrain": 1, "cylinders": 8,
            "created_at": "2026-07-18T00:00:00+00:00", "notes": "test notes"}
    md = build_markdown(sd, meta, "1.1.0")
    for expected in ("# Forza Horizon 6 tuning report", "## Car", "## Tyres",
                     "## Balance & traction", "## Suspension", "## Gearing",
                     "## My current setup", "## Prompt for the AI",
                     "test notes", "S2"):
        assert expected in md, f"missing {expected!r}"
    # Temps must be reported in Celsius (synthetic emits ~176-230 F -> 80-110 C).
    assert "°C" in md


def test_laps_csv_shape():
    sd = _synthetic_session(seconds=90.0)
    csv_text = build_laps_csv(sd)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("lap,run,route_m,complete,time_s")
    assert len(lines) >= 3  # header + at least two laps
    # Every row has the same number of columns as the header.
    n_cols = len(lines[0].split(","))
    assert all(len(l.split(",")) == n_cols for l in lines[1:])
