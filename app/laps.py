"""Lap segmentation and per-lap tuning aggregates.

Turns a recorded session into the numbers a tuner actually reads:

* laps, detected from ``LapNumber`` transitions (Horizon increments it at the
  line and resets ``CurrentLap``); free-roam sessions with no laps fall back
  to a single whole-session "stint" so every session still gets aggregates;
* per-lap and per-session aggregates chosen for setup work — tyre temps and
  front/rear balance, slip-angle-based understeer index, slip-ratio traction
  events, suspension travel usage, shift RPM and limiter time.

All heuristics/thresholds are module constants so the community can argue
about them in one place.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .session_data import SessionData

WHEELS = ["FL", "FR", "RL", "RR"]
_WHEEL_SUFFIX = ["FrontLeft", "FrontRight", "RearLeft", "RearRight"]

# --- Tunable thresholds -----------------------------------------------------
MIN_LAP_S = 15.0          # ignore "laps" shorter than this (glitch guard)
CORNERING_LAT_G = 0.30    # |lateral g| above this counts as cornering
FULL_THROTTLE = 0.98      # fraction of 255
BRAKING_MIN = 0.05
LIMITER_RPM_FRAC = 0.97   # fraction of EngineMaxRpm counted as "on limiter"
COMBINED_SLIP_HIGH = 1.0  # combined slip beyond this = sliding
SUSP_BOTTOM_OUT = 0.98

# Tyre temperature working window (deg C) used for verdicts, calibrated to
# community practice (ForzaTune guide + Forza forums physics threads):
# optimal grip ~88-99 C (190-210 F), usable street band ~77-121 C
# (170-250 F). "In window" here = 77-99 C: warm enough to work, at or below
# the optimal band's top. One value per tyre on the wire (no
# inner/middle/outer split — that's the in-game HUD only, not Data Out).
# Keep aligned with TEMP in app/static/app.js.
TEMP_COLD_C = 77.0
TEMP_HOT_C = 99.0

# Balance thresholds on the understeer index (front minus rear mean
# normalized slip angle while cornering on grip; Forza slip channels are
# normalized, 1.0 = grip limit). The band is asymmetric because steered
# front wheels naturally carry more slip angle than rears even in a neutral
# car. Provisional values pending community calibration — argue here.
USI_UNDERSTEER = 0.15
USI_OVERSTEER = -0.05
MIN_CORNERING_PCT = 5.0   # below this, refuse to call a balance verdict


def _f_to_c(arr: np.ndarray) -> np.ndarray:
    return (arr - 32.0) * (5.0 / 9.0)


def _wheel_cols(sd: SessionData, prefix: str) -> List[np.ndarray]:
    return [sd.col(prefix + s) for s in _WHEEL_SUFFIX]


def _count_runs(mask: np.ndarray, min_len: int = 3) -> int:
    if mask.size == 0:
        return 0
    padded = np.concatenate(([0], mask.astype(np.int8), [0]))
    diffs = np.diff(padded)
    starts = np.where(diffs == 1)[0]
    ends = np.where(diffs == -1)[0]
    return int(np.sum((ends - starts) >= min_len))


def _pct(mask: np.ndarray, dt: np.ndarray) -> float:
    total = float(np.sum(dt))
    if total <= 0:
        return 0.0
    return float(np.sum(dt[mask])) / total * 100.0


def _seg_indices(sd: SessionData) -> List[Dict[str, Any]]:
    """Frame index ranges per lap. Falls back to one whole-session stint."""
    lap_no = sd.col("LapNumber").astype(int)
    n = sd.n
    segments: List[Dict[str, Any]] = []
    if n == 0:
        return segments

    changes = np.where(np.diff(lap_no) != 0)[0]
    has_laps = changes.size > 0 or int(lap_no.max()) > 0
    if not has_laps:
        return [{"lap": None, "i0": 0, "i1": n, "complete": False}]

    bounds = [0] + [int(i) + 1 for i in changes] + [n]
    for k in range(len(bounds) - 1):
        i0, i1 = bounds[k], bounds[k + 1]
        if i1 <= i0:
            continue
        segments.append({
            "lap": int(lap_no[i0]),
            "i0": i0,
            "i1": i1,
            # A segment is a complete lap when the lap number advanced at its
            # end (i.e. it is not the trailing partial segment).
            "complete": k < len(bounds) - 2,
        })
    return segments


def _lap_time(sd: SessionData, seg: Dict[str, Any]) -> Optional[float]:
    """Best available lap time for a segment.

    On completion Horizon writes the finished lap's time into ``LastLap`` of
    the *next* frames; ``CurrentLap`` at the last in-segment frame is the
    fallback (slightly short by up to one frame interval).
    """
    from .packet import sane_lap

    i1 = seg["i1"]
    if seg["complete"] and i1 < sd.n:
        last_lap = sane_lap(float(sd.col("LastLap")[min(i1 + 2, sd.n - 1)]))
        if last_lap > 0:
            return last_lap
    cur = sd.col("CurrentLap")[seg["i0"]:i1]
    if cur.size:
        t = sane_lap(float(np.max(cur)))
        if t > 0:
            return t
    return None


def _slice_stats(sd: SessionData, i0: int, i1: int) -> Dict[str, Any]:
    """Aggregates for one frame range [i0, i1)."""
    sl = slice(i0, i1)
    dt = sd.dt()[sl]
    speed_ms = sd.col("Speed")[sl]
    speed_kmh = speed_ms * 3.6
    accel = sd.col("Accel")[sl] / 255.0
    brake = sd.col("Brake")[sl] / 255.0
    handbrake = sd.col("HandBrake")[sl] / 255.0
    steer = np.clip(sd.col("Steer")[sl] / 127.0, -1.0, 1.0)
    yaw_rate = sd.col("AngularVelocityY")[sl]
    lat_g = np.abs(sd.col("AccelerationX")[sl]) / 9.81
    rpm = sd.col("CurrentEngineRpm")[sl]
    rpm_max = sd.col("EngineMaxRpm")[sl]
    gear = sd.col("Gear")[sl].astype(int)

    temps_c = [_f_to_c(t[sl]) for t in _wheel_cols(sd, "TireTemp")]
    slip_angle = [a[sl] for a in _wheel_cols(sd, "TireSlipAngle")]
    slip_ratio = [r[sl] for r in _wheel_cols(sd, "TireSlipRatio")]
    combined = [c[sl] for c in _wheel_cols(sd, "TireCombinedSlip")]
    susp = [s[sl] for s in _wheel_cols(sd, "NormalizedSuspensionTravel")]

    cornering = (lat_g > CORNERING_LAT_G) & (speed_ms > 8.0)

    def _wavg(arr: np.ndarray) -> float:
        return float(np.average(arr, weights=dt)) if arr.size else 0.0

    # Counter-steer (opposite lock): steering against the car's rotation.
    # During a drift the FRONT wheels carry huge slip angles, which would
    # fool a naive front-vs-rear comparison into calling it "understeer" —
    # so balance is judged only on grip-driving frames, and drift time is
    # reported as its own number.
    counter_steer = (
        (np.sign(steer) == -np.sign(yaw_rate))
        & (np.abs(steer) > 0.15)
        & (np.abs(yaw_rate) > 0.15)
    )
    drifting = counter_steer | (handbrake > 0.1)
    grip_corner = cornering & ~drifting

    # Understeer index: mean |front slip angle| minus mean |rear slip angle|
    # while cornering on grip (drift frames excluded). Positive = front tyres
    # sliding more = understeer; negative = oversteer tendency. (radians)
    front_sa = np.maximum(np.abs(slip_angle[0]), np.abs(slip_angle[1]))
    rear_sa = np.maximum(np.abs(slip_angle[2]), np.abs(slip_angle[3]))
    if np.any(grip_corner):
        usi = float(np.mean(front_sa[grip_corner]) - np.mean(rear_sa[grip_corner]))
        front_sa_corner = float(np.mean(front_sa[grip_corner]))
        rear_sa_corner = float(np.mean(rear_sa[grip_corner]))
    else:
        usi, front_sa_corner, rear_sa_corner = 0.0, 0.0, 0.0

    front_temps = np.concatenate([temps_c[0], temps_c[1]])
    rear_temps = np.concatenate([temps_c[2], temps_c[3]])

    rear_comb = np.maximum(combined[2], combined[3])
    front_comb = np.maximum(combined[0], combined[1])

    wheelspin = _count_runs(
        ((slip_ratio[2] > 0.5) | (slip_ratio[3] > 0.5)) & (accel > 0.4)
    )
    brake_lock = _count_runs(
        ((slip_ratio[0] < -0.5) | (slip_ratio[1] < -0.5)
         | (slip_ratio[2] < -0.5) | (slip_ratio[3] < -0.5))
        & (brake > 0.6) & (sd.col("Speed")[sl] > 3.0)
    )
    bottom_out = _count_runs(
        np.maximum.reduce([s for s in susp]) > SUSP_BOTTOM_OUT
    )

    on_limiter = (rpm_max > 0) & (rpm >= rpm_max * LIMITER_RPM_FRAC)

    upshift_idx = np.where(np.diff(gear) > 0)[0] if gear.size > 1 else np.array([])
    shift_rpms = rpm[upshift_idx] if upshift_idx.size else np.array([])

    return {
        "duration_s": round(float(np.sum(dt)), 2),
        # Integrated from speed rather than DistanceTraveled, which is
        # event-relative in Horizon (can sit negative or reset in free roam).
        "distance_m": round(float(np.sum(speed_ms * dt)), 1),
        "speed": {
            "avg_kmh": round(_wavg(speed_kmh), 1),
            "max_kmh": round(float(np.max(speed_kmh)) if speed_kmh.size else 0.0, 1),
        },
        "inputs": {
            "pct_full_throttle": round(_pct(accel >= FULL_THROTTLE, dt), 1),
            "pct_braking": round(_pct(brake >= BRAKING_MIN, dt), 1),
        },
        "tyres_c": {
            w: {
                "avg": round(_wavg(t), 1),
                "max": round(float(np.max(t)) if t.size else 0.0, 1),
            }
            for w, t in zip(WHEELS, temps_c)
        },
        "temp_front_avg_c": round(float(np.mean(front_temps)), 1),
        "temp_rear_avg_c": round(float(np.mean(rear_temps)), 1),
        "temp_fr_delta_c": round(
            float(np.mean(front_temps) - np.mean(rear_temps)), 1
        ),
        "balance": {
            "understeer_index": round(usi, 4),
            "front_slip_angle_corner_avg": round(front_sa_corner, 4),
            "rear_slip_angle_corner_avg": round(rear_sa_corner, 4),
            "pct_cornering": round(_pct(cornering, dt), 1),
            "pct_drifting": round(_pct(drifting, dt), 1),
            "front_slide_time_s": round(float(np.sum(dt[front_comb > COMBINED_SLIP_HIGH])), 2),
            "rear_slide_time_s": round(float(np.sum(dt[rear_comb > COMBINED_SLIP_HIGH])), 2),
        },
        "traction": {
            "wheelspin_events": wheelspin,
            "brake_lock_events": brake_lock,
        },
        "suspension": {
            w: {
                "avg": round(_wavg(s), 3),
                "max": round(float(np.max(s)) if s.size else 0.0, 3),
            }
            for w, s in zip(WHEELS, susp)
        },
        "suspension_bottom_out_events": bottom_out,
        "gearing": {
            "top_gear": int(np.max(gear)) if gear.size else 0,
            "pct_on_limiter": round(_pct(on_limiter, dt), 1),
            "shift_rpm_avg": round(float(np.mean(shift_rpms)), 0) if shift_rpms.size else None,
            "shift_count": int(shift_rpms.size),
        },
        "max_lat_g": round(float(np.max(lat_g)) if lat_g.size else 0.0, 2),
    }


def _temp_verdict(avg_c: float) -> str:
    if avg_c < TEMP_COLD_C:
        return "cold"
    if avg_c > TEMP_HOT_C:
        return "hot"
    return "in window"


def _balance_verdict(session_stats: Dict[str, Any]) -> Dict[str, Any]:
    b = session_stats["balance"]
    usi = b["understeer_index"]
    front_sa = b["front_slip_angle_corner_avg"]
    rear_sa = b["rear_slip_angle_corner_avg"]
    front_t, rear_t = b["front_slide_time_s"], b["rear_slide_time_s"]
    drift_pct = b.get("pct_drifting", 0.0)

    if b.get("pct_cornering", 0.0) < MIN_CORNERING_PCT:
        verdict = "insufficient cornering data"
    elif usi > USI_UNDERSTEER and front_sa > 0.6:
        verdict = "understeer"
    elif usi < USI_OVERSTEER or (rear_sa > 0.8 and usi < 0):
        verdict = "oversteer"
    elif rear_t > 2 * front_t and rear_t > 1.0:
        verdict = "oversteer"
    else:
        verdict = "neutral"
    return {"verdict": verdict, "understeer_index": usi,
            "front_slip_angle_corner_avg": front_sa,
            "rear_slip_angle_corner_avg": rear_sa,
            "front_slide_time_s": front_t, "rear_slide_time_s": rear_t,
            "pct_drifting": drift_pct,
            "caveat": ("significant drifting/opposite-lock in this session; "
                       "balance judged on grip-driving frames only")
            if drift_pct > 10.0 else None}


def lap_report(sd: SessionData) -> Dict[str, Any]:
    """Full lap breakdown + session aggregates + tuning verdicts."""
    if sd.n == 0:
        return {"has_laps": False, "laps": [], "session": None, "verdicts": None}

    segments = _seg_indices(sd)
    has_laps = any(s["lap"] is not None for s in segments)

    laps: List[Dict[str, Any]] = []
    for seg in segments:
        stats = _slice_stats(sd, seg["i0"], seg["i1"])
        t = _lap_time(sd, seg)
        if seg["lap"] is not None and seg["complete"] and (
            t is None or t < MIN_LAP_S
        ):
            continue  # glitch segment (e.g. restart)
        laps.append({
            "lap": seg["lap"],
            "complete": seg["complete"],
            "time_s": round(t, 3) if t else None,
            **stats,
        })

    session_stats = _slice_stats(sd, 0, sd.n)
    complete_times = [
        l["time_s"] for l in laps if l["complete"] and l["time_s"]
    ]
    verdicts = {
        "balance": _balance_verdict(session_stats),
        "tyre_temps": {
            "front": {
                "avg_c": session_stats["temp_front_avg_c"],
                "verdict": _temp_verdict(session_stats["temp_front_avg_c"]),
            },
            "rear": {
                "avg_c": session_stats["temp_rear_avg_c"],
                "verdict": _temp_verdict(session_stats["temp_rear_avg_c"]),
            },
            "window_c": [TEMP_COLD_C, TEMP_HOT_C],
        },
    }
    return {
        "has_laps": has_laps,
        "laps": laps,
        "best_lap_s": round(min(complete_times), 3) if complete_times else None,
        "session": session_stats,
        "verdicts": verdicts,
    }
