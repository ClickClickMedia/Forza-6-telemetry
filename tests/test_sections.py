"""Tests for corner-section classification (evidence layer)."""

from __future__ import annotations

import numpy as np

from app.sections import detect_sections
from app.tuning_export import build_markdown
from tests.test_laps import _synthetic_session
from tests.test_setups import META


def _crafted_session():
    """60 s with a known shape: straight → hairpin → straight → chicane
    flick. Lateral g, yaw and speed are authored; the rest of the columns
    come from the synthetic wire path."""
    sd = _synthetic_session(seconds=60.0, hz=30.0)
    n = sd.n
    t = np.arange(n) / 30.0
    speed = np.full(n, 50.0)
    lat = np.zeros(n)
    yaw = np.zeros(n)

    # Hairpin at 20–24 s: dip to 12 m/s, 160° heading change, right-hander.
    hp = (t >= 20) & (t < 24)
    speed[hp] = 12.0 + 28.0 * np.abs(np.linspace(-1, 1, int(hp.sum())))
    lat[hp] = 0.6 * 9.81 / 9.81  # authored in g, converted below
    yaw[t >= 24] += np.radians(160)
    ramp = np.linspace(0, np.radians(160), int(hp.sum()))
    yaw[hp] = ramp

    # Chicane at 40–42.4 s: left flick then right, 0.6 s apart at speed.
    fl = (t >= 40.0) & (t < 41.0)
    fr = (t >= 41.2) & (t < 42.2)
    lat[fl] = -0.7
    lat[fr] = 0.7
    sd.columns["Speed"] = speed
    sd.columns["AccelerationX"] = lat * 9.81
    sd.columns["Yaw"] = yaw
    sd.columns["Accel"] = np.full(n, 255.0)
    sd.columns["Brake"] = np.zeros(n)
    return sd


def test_sections_classify_crafted_shapes():
    sec = detect_sections(_crafted_session())
    assert sec is not None
    assert sec["hairpin"]["count"] >= 1
    hp = sec["hairpin"]["highest"]
    assert hp["min_kmh"] < 60 and hp["heading_deg"] >= 100
    assert sec["transfer"]["count"] >= 1
    tr = sec["transfer"]["highest"]
    assert tr["reversal_s"] < 3.0
    assert sec["straight"]["count"] >= 1
    st = sec["straight"]["highest"]
    assert st["length_m"] >= 200
    # Representative samples carry timestamps the AI can find in the CSV.
    assert ":" in hp["start"]


def test_sections_none_for_tiny_session():
    sd = _synthetic_session(seconds=5.0)
    sd.columns["Speed"] = np.zeros(sd.n)
    # 5 s at 30 Hz = 150 frames < the 300-frame floor.
    assert detect_sections(sd) is None


def test_report_carries_section_evidence():
    md = build_markdown(_crafted_session(), META, "2.2.0")
    assert "## Section evidence" in md
    assert "### Hairpin" in md
    assert "ordered by" in md.lower()
    # Evidence, not verdicts: the export never prescribes settings.
    for banned in ("increase rear", "reduce front", "recommend increasing",
                   "you should"):
        assert banned not in md.lower()
