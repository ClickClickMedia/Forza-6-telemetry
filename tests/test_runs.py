"""Tests for free-roam run detection and the grouped-event analytics."""

from __future__ import annotations

import numpy as np

from app.laps import (
    _grouped_events,
    _hysteresis_mask,
    detect_runs,
    lap_report,
)
from app.session_data import SessionData
from tests.test_laps import _synthetic_session


def _with_run_signature(sd: SessionData) -> SessionData:
    """Overlay the real-world time-attack DistanceTraveled signature:
    free roam → staged negative → 0-crossing at launch → climb → snap reset."""
    n = sd.n
    dist = np.zeros(n)
    q = n // 10
    dist[: 2 * q] = np.linspace(500, 900, 2 * q)          # roaming
    dist[2 * q: 2 * q + 60] = -238.0                       # staged at the line
    run_len = 6 * q - 60
    dist[2 * q + 60: 8 * q] = np.linspace(0, 15000, run_len)   # the run
    dist[8 * q:] = np.linspace(0, 300, n - 8 * q)          # snap reset + roam
    sd.columns["DistanceTraveled"] = dist
    # Staged = stationary (as in every real capture); required since the
    # drive-past guard demands a near-zero-speed moment in the span.
    sd.columns["Speed"][2 * q: 2 * q + 60] = 0.0
    sd.columns["LapNumber"] = np.zeros(n)
    sd.columns["CurrentLap"] = np.zeros(n)
    sd.columns["BestLap"] = np.zeros(n)
    sd.columns["LastLap"] = np.zeros(n)
    return sd


def test_detect_runs_from_distance_signature():
    sd = _with_run_signature(_synthetic_session(seconds=200.0, hz=30.0))
    runs = detect_runs(sd)
    assert len(runs) == 1
    run = runs[0]
    # Route is integrated from speed (wire distance is untrustworthy).
    total = float(np.sum(sd.col("Speed") * sd.dt()))
    assert 0 < run["route_m"] <= total + 1
    assert run["time_s"] > 60
    rep = lap_report(sd)
    assert rep["has_runs"] and not rep["has_laps"]
    assert rep["laps"][0]["run"] == 1
    assert rep["best_lap_s"] == rep["laps"][0]["time_s"]


def test_detect_runs_staged_at_zero_variant():
    """Route-blueprint time attacks stage AT dist=0 (no negative phase):
    teleport in, hold 0 while staged, climb, snap-reset at the finish."""
    sd = _synthetic_session(seconds=200.0, hz=30.0)
    n = sd.n
    dist = np.zeros(n)
    q = n // 10
    dist[:q] = 0.0                                   # staged hold at zero
    dist[q: 8 * q] = np.linspace(0, 6000, 7 * q)     # the run
    dist[8 * q:] = np.linspace(120, 400, n - 8 * q)  # post-reset roaming
    dist[8 * q - 1] = 6000
    dist[8 * q] = 0.0                                # snap reset at finish
    sd.columns["DistanceTraveled"] = dist
    for col in ("LapNumber", "CurrentLap", "BestLap", "LastLap"):
        sd.columns[col] = np.zeros(n)
    runs = detect_runs(sd)
    assert len(runs) == 1
    assert runs[0]["route_m"] > 2000
    rep = lap_report(sd)
    assert rep["has_runs"]


def test_run_route_ignores_inflated_wire_distance():
    """Real circuit capture: DistanceTraveled advanced ~2.9x faster than the
    car moved. Route must come from speed integration, boundaries from
    distance — never distance magnitude."""
    sd = _synthetic_session(seconds=120.0, hz=30.0)
    n = sd.n
    q = n // 10
    real = np.cumsum(sd.columns["Speed"] * (1 / 30))
    dist = 3.0 * real                                # inflated wire value
    dist[:q] = 0.0                                   # staged hold
    dist[9 * q:] = 0.0                               # snap at event end
    sd.columns["DistanceTraveled"] = dist
    for col in ("LapNumber", "CurrentLap", "BestLap", "LastLap"):
        sd.columns[col] = np.zeros(n)
    runs = detect_runs(sd)
    assert len(runs) == 1
    run = runs[0]
    window_real = float(real[9 * q - 1] - real[run["i0"]])
    assert abs(run["route_m"] - window_real) < window_real * 0.05, \
        "route must track real driven distance, not the inflated wire value"


def test_plain_free_roam_detects_no_runs():
    sd = _synthetic_session(seconds=30.0, hz=30.0)
    sd.columns["LapNumber"] = np.zeros(sd.n)
    sd.columns["DistanceTraveled"] = np.linspace(0, 3000, sd.n)  # never negative
    assert detect_runs(sd) == []
    rep = lap_report(sd)
    assert not rep["has_runs"]
    assert len(rep["laps"]) == 1 and rep["laps"][0]["lap"] is None


def test_grouped_events_merges_chatter():
    dt = np.full(100, 1 / 60)  # 60 Hz
    mask = np.zeros(100, dtype=bool)
    mask[10:20] = True     # 167 ms event
    mask[22:30] = True     # gap of 2 frames (33 ms) -> merges with previous
    mask[80:82] = True     # 33 ms blip -> below min duration, dropped
    g = _grouped_events(mask, dt, min_s=0.1, gap_s=0.3)
    assert g["events"] == 1
    assert g["total_s"] > 0.3
    assert g["longest_s"] == g["total_s"]


def test_hysteresis_prevents_chatter():
    x = np.array([0.5, 1.1, 0.95, 1.02, 0.95, 0.85, 1.1, 0.5])
    m = _hysteresis_mask(x, enter=1.05, exit_=0.90)
    # One continuous slide from the first 1.1 until dropping below 0.90,
    # then a new one at the second 1.1.
    assert m.tolist() == [False, True, True, True, True, False, True, False]


def test_grouped_events_invariants():
    dt = np.full(200, 1 / 60)
    # No crossing.
    g = _grouped_events(np.zeros(200, dtype=bool), dt)
    assert (g["events"], g["total_s"], g["longest_s"]) == (0, 0.0, 0.0)
    # One 2 s event.
    m = np.zeros(200, dtype=bool); m[10:130] = True
    g = _grouped_events(m, dt)
    assert g["events"] == 1 and abs(g["total_s"] - 2.0) < 0.05
    assert abs(g["longest_s"] - g["total_s"]) < 1e-6
    # Two events (1 s and ~1.5 s) separated by > gap.
    m = np.zeros(200, dtype=bool); m[0:60] = True; m[110:200] = True  # 2nd open at end
    g = _grouped_events(m, dt)
    assert g["events"] == 2
    assert abs(g["total_s"] - (1.0 + 1.5)) < 0.05
    assert abs(g["longest_s"] - 1.5) < 0.05
    # Universal invariants.
    assert g["total_s"] >= g["longest_s"]
    assert (g["events"] == 0) == (g["total_s"] == 0)


def test_wheelspin_uses_driven_wheels_per_drivetrain():
    def session_with(drivetrain, spin_front):
        sd = _synthetic_session(seconds=30.0, hz=30.0)
        n = sd.n
        sd.columns["DrivetrainType"] = np.full(n, float(drivetrain))
        sd.columns["Accel"] = np.full(n, 255.0)
        sd.columns["Speed"] = np.full(n, 30.0)
        sd.columns["HandBrake"] = np.zeros(n)
        zero, spin = np.zeros(n), np.full(n, 1.5)
        for w in ("FrontLeft", "FrontRight"):
            sd.columns[f"TireSlipRatio{w}"] = spin if spin_front else zero
        for w in ("RearLeft", "RearRight"):
            sd.columns[f"TireSlipRatio{w}"] = zero if spin_front else spin
        return sd

    # FWD car with FRONT slip -> wheelspin detected.
    rep = lap_report(session_with(0, spin_front=True))
    assert rep["session"]["traction"]["driven_wheels"] == ["FL", "FR"]
    assert rep["session"]["traction"]["wheelspin_events"] >= 1
    byw = rep["session"]["traction"]["wheelspin_by_wheel_s"]
    assert set(byw) == {"FL", "FR"} and byw["FL"] > 0 and byw["FR"] > 0
    assert rep["session"]["traction"]["wheelspin_both_driven_s"] > 0
    # FWD car with only REAR slip (trailing wheels) -> NOT wheelspin.
    rep = lap_report(session_with(0, spin_front=False))
    assert rep["session"]["traction"]["wheelspin_events"] == 0
    # RWD car with rear slip -> detected.
    rep = lap_report(session_with(1, spin_front=False))
    assert rep["session"]["traction"]["driven_wheels"] == ["RL", "RR"]
    assert rep["session"]["traction"]["wheelspin_events"] >= 1


def test_observed_peaks_ignore_partial_throttle():
    sd = _synthetic_session(seconds=20.0, hz=30.0)
    # Zero throttle everywhere -> no valid pulls -> peaks are None.
    sd.columns["Accel"] = np.zeros(sd.n)
    rep = lap_report(sd)
    assert rep["session"]["observed_peaks"]["power_kw"] is None
