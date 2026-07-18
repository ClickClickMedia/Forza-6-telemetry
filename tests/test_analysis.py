"""Tests for the analysis engine and recording round-trip.

Builds a synthetic session on disk, loads it back, and checks that the analysis
metrics are self-consistent and that event detection fires on constructed
signals.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from app.analysis import _count_events, analyse
from app.session_data import RAW_COLUMNS, SessionData, load_session
from app.packet import FIELD_NAMES


def _write_csv(path: Path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(RAW_COLUMNS)
        for r in rows:
            w.writerow(r)


def _row(t, **overrides):
    """Build one raw CSV row (t_mono, t_wall, then all fields)."""
    values = {name: 0 for name in FIELD_NAMES}
    values.update(overrides)
    return [t, "2026-01-01T00:00:00+00:00"] + [values[name] for name in FIELD_NAMES]


def test_count_events_debounce():
    mask = np.array([0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 1], dtype=bool)
    # First run length 3 (>=3 -> counts), single 1 ignored, last run length 4 counts.
    assert _count_events(mask, min_len=3) == 2
    assert _count_events(mask, min_len=1) == 3


def test_analyse_empty():
    sd = SessionData({c: np.array([]) for c in RAW_COLUMNS}, 0)
    result = analyse(sd)
    assert result["empty"] is True


def test_analyse_basic_metrics(tmp_path):
    # 100 frames at 60 Hz. Speed ramps 0..50 m/s, full throttle throughout.
    rows = []
    for i in range(100):
        t = i / 60.0
        speed = i * 0.5  # up to 49.5 m/s
        rows.append(_row(
            t,
            Speed=speed,
            Accel=255,           # full throttle
            Brake=0,
            AccelerationX=10.0,  # lateral
            AccelerationZ=5.0,   # longitudinal
            Gear=3,
            CurrentEngineRpm=6000,
            TireTempFrontLeft=90.0,
            TireTempFrontRight=91.0,
            TireTempRearLeft=95.0,
            TireTempRearRight=96.0,
        ))
    p = tmp_path / "s.csv"
    _write_csv(p, rows)
    sd = load_session(p, "csv")
    r = analyse(sd)

    assert r["frames"] == 100
    assert r["speed"]["peak_kmh"] == pytest.approx(49.5 * 3.6, abs=0.1)
    assert r["inputs"]["pct_full_throttle"] == pytest.approx(100.0, abs=0.1)
    assert r["inputs"]["pct_braking"] == pytest.approx(0.0, abs=0.1)
    assert r["acceleration"]["max_lat_g"] == pytest.approx(10.0 / 9.81, abs=0.01)
    assert r["tyres"]["RL"]["temp_max"] == pytest.approx(95.0, abs=0.1)
    assert r["gear_usage_s"]["3"] > 0


def test_analyse_detects_events(tmp_path):
    # Construct frames that trigger wheelspin, bottom-out and brake lock.
    rows = []
    for i in range(60):
        t = i / 60.0
        if i < 20:
            # Wheelspin: high rear slip ratio + throttle.
            rows.append(_row(t, Speed=30, Accel=255,
                             TireSlipRatioRearLeft=1.5, TireSlipRatioRearRight=1.5,
                             TireCombinedSlipRearLeft=1.5, TireCombinedSlipRearRight=1.5))
        elif i < 40:
            # Suspension bottom-out.
            rows.append(_row(t, Speed=30,
                             NormalizedSuspensionTravelFrontLeft=0.99))
        else:
            # Brake lock: heavy brake, strongly negative slip ratio, moving.
            rows.append(_row(t, Speed=30, Brake=255,
                             TireSlipRatioFrontLeft=-0.9))
    p = tmp_path / "e.csv"
    _write_csv(p, rows)
    sd = load_session(p, "csv")
    r = analyse(sd)
    ev = r["events"]
    assert ev["wheelspin"] >= 1
    assert ev["suspension_bottom_out"] >= 1
    assert ev["brake_lock"] >= 1
