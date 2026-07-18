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


def _straightline(sd: SessionData) -> SessionData:
    """Non-returning trajectory: the synthetic driver laps an oval, which
    the position-gate detector correctly reads as a circuit — these tests
    exercise the DistanceTraveled machinery for point-to-point runs."""
    sd.columns["PositionX"] = np.linspace(0.0, 20000.0, sd.n)
    sd.columns["PositionZ"] = np.zeros(sd.n)
    return sd


def _with_run_signature(sd: SessionData) -> SessionData:
    """Overlay the real-world time-attack DistanceTraveled signature:
    free roam → staged negative → 0-crossing at launch → climb → snap reset."""
    _straightline(sd)
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
    sd = _straightline(_synthetic_session(seconds=200.0, hz=30.0))
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

    # FWD car with FRONT slip -> wheelspin detected. Both wheels spin
    # together, so exclusive per-wheel buckets are 0 and 'multiple' carries
    # the time; buckets must reconcile with the total exactly.
    rep = lap_report(session_with(0, spin_front=True))
    trac = rep["session"]["traction"]
    assert trac["driven_wheels"] == ["FL", "FR"]
    assert trac["wheelspin_events"] >= 1
    byw = trac["wheelspin_by_wheel_s"]
    assert set(byw) == {"FL", "FR"}
    assert trac["wheelspin_multi_s"] > 0
    total = trac["wheelspin_total_s"]
    assert abs(sum(byw.values()) + trac["wheelspin_multi_s"] - total) < 0.2
    assert abs(trac["wheelspin_turning_s"] + trac["wheelspin_straight_s"] - total) < 0.2
    # FWD car with only REAR slip (trailing wheels) -> NOT wheelspin.
    rep = lap_report(session_with(0, spin_front=False))
    assert rep["session"]["traction"]["wheelspin_events"] == 0
    # RWD car with rear slip -> detected.
    rep = lap_report(session_with(1, spin_front=False))
    assert rep["session"]["traction"]["driven_wheels"] == ["RL", "RR"]
    assert rep["session"]["traction"]["wheelspin_events"] >= 1


def test_brake_lock_requires_wheel_stoppage_not_just_slip():
    """Forza's normalized slip crosses -0.5 in ordinary hard braking with
    zero lockup (verified on a real capture). Lock detection must key on
    the wheel actually stopping relative to road speed."""
    sd = _synthetic_session(seconds=60.0, hz=30.0)
    n = sd.n
    speed = np.full(n, 30.0)
    sd.columns["Speed"] = speed
    k = 3.0
    rot = k * speed.copy()
    brake = np.zeros(n)
    slip = np.zeros(n)
    q = n // 6
    # Coasting for calibration (first third), then two braking phases.
    sd.columns["Accel"] = np.zeros(n)
    sd.columns["Brake"] = brake
    # Phase A: hard braking, deep slip ratio, wheels STILL TURNING.
    brake[2 * q:3 * q] = 255.0
    slip[2 * q:3 * q] = -0.9
    # Phase B: hard braking, wheels genuinely stopped (rot -> 5% expected).
    brake[4 * q:5 * q] = 255.0
    rot[4 * q:5 * q] = 0.05 * k * 30.0
    sd.columns["Brake"] = brake
    for w in ("FrontLeft", "FrontRight", "RearLeft", "RearRight"):
        sd.columns[f"TireSlipRatio{w}"] = slip
        sd.columns[f"WheelRotationSpeed{w}"] = rot
    sd.columns["HandBrake"] = np.zeros(n)
    trac = lap_report(sd)["session"]["traction"]
    assert trac["brake_lock_method"] == "wheel-speed deficit"
    # Only phase B (~10 s) counts; phase A's slip excursion must not.
    front_s = trac["brake_lock_front_s"]
    assert 8.0 < front_s < 12.0, front_s
    # Phase A is exactly the ABS-modulation signature (deep slip, wheels
    # still turning) — it must surface as near-lock time, kept separate
    # from sustained lock so neither number hides the other.
    assert 8.0 < trac["near_lock_s"] < 12.0, trac["near_lock_s"]
    assert trac["near_lock_pct_of_braking"] > 0


def _circuit_session(loops: float = 3.0, radius: float = 800.0,
                     snap_at_frac: float = None):
    """Synthetic staged circuit: the car drives `loops` circles at constant
    speed. DistanceTraveled shows one staged run (or two, if `snap_at_frac`
    injects a mid-race rewind snap)."""
    seconds = 240.0
    sd = _synthetic_session(seconds=seconds, hz=30.0)
    n = sd.n
    q = n // 12
    speed = np.full(n, 40.0)
    speed[:q] = 0.0                       # staged
    sd.columns["Speed"] = speed
    # Circle trajectory while driving; parked at the start point before.
    total_angle = 2 * np.pi * loops
    circumference_time = (n - q) / 30.0
    ang = np.zeros(n)
    ang[q:] = np.linspace(0, total_angle, n - q)
    # radius chosen so 40 m/s matches the arc: r = v * T / total_angle
    r = 40.0 * circumference_time / total_angle
    sd.columns["PositionX"] = r * np.sin(ang)
    sd.columns["PositionZ"] = r * (1 - np.cos(ang))
    dist = np.full(n, -238.0)
    dist[q:] = np.linspace(0, 8000, n - q)
    if snap_at_frac is not None:
        # A rewind snap-drops the distance, holds ~0 during the replay
        # scrub, then climbs again (the shape seen in the real capture).
        cut = q + int((n - q) * snap_at_frac)
        dist[cut:cut + 60] = 0.5
        dist[cut + 60:] = np.linspace(1, 4000, n - cut - 60)
    sd.columns["DistanceTraveled"] = dist
    for col in ("LapNumber", "CurrentLap", "BestLap", "LastLap"):
        sd.columns[col] = np.zeros(n)
    return sd


def test_position_gate_splits_circuit_into_laps():
    """A staged run that re-passes its start point with matching heading is
    a circuit — split it into laps (validated on a real 3-lap race)."""
    rep = lap_report(_circuit_session(loops=3.0))
    assert rep["lap_source"] == "position-gate"
    assert rep["has_laps"] and not rep["has_runs"]
    complete = [l for l in rep["laps"] if l["complete"]]
    assert len(complete) == 3
    routes = [l["route_m"] for l in complete]
    assert max(routes) / min(routes) < 1.1
    assert rep["best_lap_s"] == min(l["time_s"] for l in complete)


def test_position_gate_survives_mid_race_rewind_snap():
    """A rewind snaps DistanceTraveled and splits the event into two 'runs'
    — position laps must bridge the split (the real failure this fixes)."""
    rep = lap_report(_circuit_session(loops=3.0, snap_at_frac=0.4))
    assert rep["lap_source"] == "position-gate"
    assert len([l for l in rep["laps"] if l["complete"]]) == 3


def test_position_laps_survive_zeroed_staging_and_spur_start():
    """Two real-world hazards: staged events broadcast Position (0,0)
    while the world loads (a gate anchored there sits kilometres off the
    circuit), and the grid can sit on a run-in spur the loop never
    revisits. The gate must be discovered from the trajectory, and the
    spur reported as a leading partial."""
    sd = _circuit_session(loops=3.0)
    n = sd.n
    q = n // 12
    # Zero positions during staging (the Legends Isle failure).
    sd.columns["PositionX"][:q] = 0.0
    sd.columns["PositionZ"][:q] = 0.0
    # Splice a straight run-in spur that joins the circle continuously at
    # its top (position and heading both match at the join).
    spur = q + (n - q) // 6
    zr = float(np.max(sd.columns["PositionZ"]))  # circle top = 2r
    sd.columns["PositionX"][q:spur] = np.linspace(1500, 0, spur - q)
    sd.columns["PositionZ"][q:spur] = zr
    rep = lap_report(sd)
    assert rep["lap_source"] == "position-gate"
    complete = [l for l in rep["laps"] if l["complete"]]
    assert len(complete) >= 2
    assert not rep["laps"][0]["complete"], \
        "the run-in spur must surface as a leading partial, never ranked"
    assert rep["best_lap_s"] == min(l["time_s"] for l in complete)


def test_position_laps_rephase_to_event_end():
    """When the discovered phase strands a large untimed tail (event ends
    mid-loop relative to the discovered gate), the gate re-anchors near
    the event end so the real final lap is captured, not smeared."""
    sd = _circuit_session(loops=3.6)
    rep = lap_report(sd)
    assert rep["lap_source"] == "position-gate"
    complete = [l for l in rep["laps"] if l["complete"]]
    assert len(complete) == 3
    # The 0.6-loop leftover lands at the START as a partial (never ranked).
    partials = [l for l in rep["laps"] if not l["complete"]]
    assert partials and all(l["time_s"] is not None for l in partials)
    assert rep["best_lap_s"] == min(l["time_s"] for l in complete)


def test_point_to_point_run_gets_no_position_laps():
    """A run that never returns to its start stays a single timed run."""
    sd = _with_run_signature(_synthetic_session(seconds=200.0, hz=30.0))
    n = sd.n
    # Straight-line trajectory: never re-approaches the start point.
    sd.columns["PositionX"] = np.linspace(0, 20000, n)
    sd.columns["PositionZ"] = np.zeros(n)
    rep = lap_report(sd)
    assert rep["lap_source"] is None
    assert rep["has_runs"] and not rep["has_laps"]


def test_observed_peaks_ignore_partial_throttle():
    sd = _synthetic_session(seconds=20.0, hz=30.0)
    # Zero throttle everywhere -> no valid pulls -> peaks are None.
    sd.columns["Accel"] = np.zeros(sd.n)
    rep = lap_report(sd)
    assert rep["session"]["observed_peaks"]["power_kw"] is None
