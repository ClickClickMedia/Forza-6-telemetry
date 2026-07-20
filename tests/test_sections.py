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


def _sample(bucket):
    return bucket.get("only") or bucket.get("highest")


def test_sections_classify_crafted_shapes():
    sec = detect_sections(_crafted_session())
    assert sec is not None
    assert sec["hairpin"]["count"] >= 1
    hp = _sample(sec["hairpin"])
    assert hp["min_kmh"] < 60 and hp["heading_deg"] >= 100
    assert sec["transfer"]["count"] >= 1
    tr = _sample(sec["transfer"])
    assert tr["reversal_s"] < 3.0
    assert sec["straight"]["count"] >= 1
    st = _sample(sec["straight"])
    assert st["length_m"] >= 200
    # Representative samples carry timestamps the AI can find in the CSV.
    assert ":" in hp["start"]
    # Throttle semantics are self-consistent: full throttle everywhere in
    # the crafted session → never_lifted, never a bogus "already_on".
    assert hp["throttle_reapply_s"] == "never_lifted"
    assert hp["throttle_min_pct"] >= 50


def test_throttle_reapply_reports_time_after_a_lift():
    sd = _crafted_session()
    t = np.arange(sd.n) / 30.0
    # Lift to zero through the hairpin, back on 1 s after the deepest point.
    hp = (t >= 20) & (t < 23)
    sd.columns["Accel"][hp] = 0.0
    sec = detect_sections(sd)
    inst = _sample(sec["hairpin"])
    assert inst["throttle_min_pct"] == 0
    assert isinstance(inst["throttle_reapply_s"], float)
    assert inst["throttle_reapply_s"] > 0


def test_impact_contaminated_transfer_is_dropped():
    """A 5 g lateral spike (collision/landing) inside a flick must not
    reach the evidence table as a 'performance transfer'."""
    sd = _crafted_session()
    t = np.arange(sd.n) / 30.0
    spike = (t >= 40.0) & (t < 42.2)
    sd.columns["AccelerationX"][spike] = np.sign(
        sd.columns["AccelerationX"][spike] + 1e-9) * 5.0 * 9.81
    sec = detect_sections(sd)
    assert sec["transfer"]["count"] == 0


def test_launch_separated_from_flying_straights():
    sd = _crafted_session()
    n = sd.n
    t = np.arange(n) / 30.0
    # Standing start: first 12 s accelerate 0 -> 50 m/s.
    ramp = t < 12.0
    sd.columns["Speed"][ramp] = np.linspace(0, 50, int(ramp.sum()))
    sec = detect_sections(sd)
    assert sec["launch"]["count"] >= 1
    la = _sample(sec["launch"])
    assert la["start"] == "00:00.00"
    # The remaining straights all have flying starts.
    for key in ("only", "lowest", "median", "highest"):
        inst = sec["straight"].get(key)
        if inst:
            assert float(inst["speed_kmh"].split("→")[0]) >= 30


def test_two_instance_bucket_uses_lower_higher():
    """An even pair has no median member — never invent one."""
    sd = _crafted_session()
    sec = detect_sections(sd)
    for cat in ("hairpin", "turn", "sweeper", "transfer", "straight"):
        b = sec[cat]
        if b["count"] == 2:
            assert "lower" in b and "higher" in b and "median" not in b
        elif b["count"] == 1:
            assert "only" in b


def test_timed_windows_gate_samples():
    """Instances outside the timed windows are labelled and excluded from
    representative samples — staging shuffles must never be a category's
    'lowest'."""
    sd = _crafted_session()
    # Timed window covers only 15–45 s: the hairpin (20–24) and chicane
    # (40–42.4) are inside; the trailing straight after 45 s is outside.
    sec = detect_sections(sd, timed_windows=[(15.0, 45.0)])
    hp = sec["hairpin"].get("only") or sec["hairpin"].get("highest")
    assert hp.get("timed") is True
    st = sec["straight"]
    assert st.get("outside_timed", 0) >= 1
    for key in ("only", "lower", "higher", "lowest", "median", "highest"):
        inst = st.get(key)
        if inst:
            assert inst.get("timed") is True, \
                "samples must come from timed running when any exist"


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
