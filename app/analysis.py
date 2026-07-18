"""Per-session telemetry analysis.

All calculations operate on a :class:`~app.session_data.SessionData` (numpy
column arrays). Time-based metrics use per-frame ``dt`` derived from the
monotonic receive timestamps, so results are correct regardless of the exact
capture rate.

The four wheels are consistently ordered [FL, FR, RL, RR].
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

from .session_data import SessionData

WHEELS = ["FL", "FR", "RL", "RR"]

# --- Tunable thresholds (documented so results are interpretable) ----------
FULL_THROTTLE = 0.98         # fraction of 255 counted as "full throttle"
BRAKING_MIN = 0.05           # fraction of 255 counted as "braking"
COMBINED_SLIP_HIGH = 1.0     # combined slip considered "sliding"
REAR_SLIP_THROTTLE = 0.40    # throttle fraction for rear-slip-on-power events
FRONT_SLIP_STEER = 0.20      # |steer| fraction for front-slip-while-steering
HEAVY_BRAKE = 0.60           # brake fraction considered "heavy braking"
SUSP_BOTTOM_OUT = 0.98       # normalised compression -> bottom-out
SUSP_FULL_EXT = 0.02         # normalised compression -> full extension
BRAKE_LOCK_SLIP = -0.5       # slip ratio at/under this while braking -> lock
WHEELSPIN_SLIP = 0.5         # rear slip ratio above this on throttle -> spin
YAW_RATE_MIN = 0.15          # rad/s, "meaningful" yaw for over/understeer
EVENT_DEBOUNCE = 3           # min consecutive frames to count one event


def _count_events(mask: np.ndarray, min_len: int = EVENT_DEBOUNCE) -> int:
    """Count contiguous runs of True at least ``min_len`` frames long."""
    if mask.size == 0:
        return 0
    m = mask.astype(np.int8)
    # Rising edges.
    padded = np.concatenate(([0], m, [0]))
    diffs = np.diff(padded)
    starts = np.where(diffs == 1)[0]
    ends = np.where(diffs == -1)[0]
    return int(np.sum((ends - starts) >= min_len))


def _time_true(mask: np.ndarray, dt: np.ndarray) -> float:
    """Total seconds ``mask`` is True."""
    if mask.size == 0:
        return 0.0
    return float(np.sum(dt[mask]))


def _pct_true(mask: np.ndarray, dt: np.ndarray) -> float:
    total = float(np.sum(dt))
    if total <= 0:
        return 0.0
    return _time_true(mask, dt) / total * 100.0


def _wheel_cols(sd: SessionData, prefix: str) -> List[np.ndarray]:
    suffix = ["FrontLeft", "FrontRight", "RearLeft", "RearRight"]
    return [sd.col(prefix + s) for s in suffix]


def analyse(sd: SessionData) -> Dict[str, Any]:
    if sd.n == 0:
        return {"frames": 0, "empty": True}

    dt = sd.dt()
    duration = float(np.sum(dt))

    speed = sd.col("Speed")  # m/s
    speed_kmh = speed * 3.6
    accel = sd.col("Accel") / 255.0
    brake = sd.col("Brake") / 255.0
    steer = np.clip(sd.col("Steer") / 127.0, -1.0, 1.0)
    gear = sd.col("Gear").astype(int)
    rpm = sd.col("CurrentEngineRpm")
    lat = sd.col("AccelerationX")   # lateral (m/s^2)
    lon = sd.col("AccelerationZ")   # longitudinal (m/s^2)
    yaw_rate = sd.col("AngularVelocityY")

    combined = _wheel_cols(sd, "TireCombinedSlip")
    slip_ratio = _wheel_cols(sd, "TireSlipRatio")
    slip_angle = _wheel_cols(sd, "TireSlipAngle")
    temps = _wheel_cols(sd, "TireTemp")
    susp = _wheel_cols(sd, "NormalizedSuspensionTravel")

    out: Dict[str, Any] = {
        "frames": sd.n,
        "duration_s": round(duration, 2),
        "empty": False,
    }

    # --- Speed ---------------------------------------------------------
    out["speed"] = {
        "peak_kmh": round(float(np.max(speed_kmh)), 1),
        "avg_kmh": round(float(np.average(speed_kmh, weights=dt)), 1),
        "peak_ms": round(float(np.max(speed)), 2),
    }

    # --- Acceleration extremes ----------------------------------------
    out["acceleration"] = {
        "max_lat_g": round(float(np.max(np.abs(lat))) / 9.81, 2),
        "max_lon_accel_g": round(float(np.max(lon)) / 9.81, 2),
        "max_lon_brake_g": round(float(np.min(lon)) / 9.81, 2),
        "max_lat_ms2": round(float(np.max(np.abs(lat))), 2),
    }

    # --- Throttle / brake usage ---------------------------------------
    out["inputs"] = {
        "pct_full_throttle": round(_pct_true(accel >= FULL_THROTTLE, dt), 1),
        "pct_braking": round(_pct_true(brake >= BRAKING_MIN, dt), 1),
        "pct_coasting": round(
            _pct_true((accel < BRAKING_MIN) & (brake < BRAKING_MIN), dt), 1
        ),
    }

    # --- Gear usage ----------------------------------------------------
    gear_time: Dict[str, float] = {}
    for g in sorted(set(gear.tolist())):
        gear_time[str(int(g))] = round(_time_true(gear == g, dt), 2)
    out["gear_usage_s"] = gear_time

    # --- Shift RPM (upshifts) -----------------------------------------
    shift_rpms: List[float] = []
    if sd.n > 1:
        upshift_idx = np.where(np.diff(gear) > 0)[0]
        for i in upshift_idx:
            shift_rpms.append(float(rpm[i]))
    out["shift_rpm"] = {
        "count": len(shift_rpms),
        "avg": round(float(np.mean(shift_rpms)), 0) if shift_rpms else None,
        "min": round(float(np.min(shift_rpms)), 0) if shift_rpms else None,
        "max": round(float(np.max(shift_rpms)), 0) if shift_rpms else None,
    }

    # --- Tyre temperatures & slip-time by wheel -----------------------
    tyre: Dict[str, Any] = {}
    for w, temp_c, comb in zip(WHEELS, temps, combined):
        tyre[w] = {
            "temp_min": round(float(np.min(temp_c)), 1),
            "temp_avg": round(float(np.average(temp_c, weights=dt)), 1),
            "temp_max": round(float(np.max(temp_c)), 1),
            "time_over_combined_slip_1_s": round(
                _time_true(comb > COMBINED_SLIP_HIGH, dt), 2
            ),
        }
    out["tyres"] = tyre

    # --- Slip / handling events ---------------------------------------
    # Rear slip on power: either rear wheel sliding while throttle high.
    rear_comb = np.maximum(combined[2], combined[3])
    rear_slip_events = _count_events(
        (rear_comb > COMBINED_SLIP_HIGH) & (accel > REAR_SLIP_THROTTLE)
    )

    # Front slip while steering and not heavily braking.
    front_comb = np.maximum(combined[0], combined[1])
    front_slip_events = _count_events(
        (front_comb > COMBINED_SLIP_HIGH)
        & (np.abs(steer) > FRONT_SLIP_STEER)
        & (brake < HEAVY_BRAKE)
    )

    # Suspension bottom-out / full-extension per wheel (any wheel).
    any_bottom = np.zeros(sd.n, dtype=bool)
    any_ext = np.zeros(sd.n, dtype=bool)
    for s in susp:
        any_bottom |= s > SUSP_BOTTOM_OUT
        any_ext |= s < SUSP_FULL_EXT
    bottom_out_events = _count_events(any_bottom)
    full_ext_events = _count_events(any_ext)

    # Brake lock: any wheel slip ratio strongly negative while braking & moving.
    moving = speed > 3.0
    any_lock = np.zeros(sd.n, dtype=bool)
    for sr in slip_ratio:
        any_lock |= sr < BRAKE_LOCK_SLIP
    brake_lock_events = _count_events(any_lock & (brake > HEAVY_BRAKE) & moving)

    # Wheelspin: either rear wheel slip ratio high while on throttle.
    rear_spin = (slip_ratio[2] > WHEELSPIN_SLIP) | (slip_ratio[3] > WHEELSPIN_SLIP)
    wheelspin_events = _count_events(rear_spin & (accel > REAR_SLIP_THROTTLE))

    # Oversteer candidates: rear sliding, meaningful yaw, and driver applying
    # opposite-lock correction (steer sign opposes yaw sign).
    counter_steer = np.sign(steer) == -np.sign(yaw_rate)
    oversteer = (
        (rear_comb > COMBINED_SLIP_HIGH)
        & (np.abs(yaw_rate) > YAW_RATE_MIN)
        & counter_steer
        & (np.abs(steer) > FRONT_SLIP_STEER)
    )
    oversteer_events = _count_events(oversteer)

    # Understeer candidates: front sliding, steering applied, but yaw response
    # is small relative to steering input (car won't rotate).
    understeer = (
        (front_comb > COMBINED_SLIP_HIGH)
        & (np.abs(steer) > FRONT_SLIP_STEER)
        & (np.abs(yaw_rate) < YAW_RATE_MIN)
        & (brake < HEAVY_BRAKE)
    )
    understeer_events = _count_events(understeer)

    out["events"] = {
        "rear_slip_on_power": rear_slip_events,
        "front_slip_while_steering": front_slip_events,
        "suspension_bottom_out": bottom_out_events,
        "suspension_full_extension": full_ext_events,
        "brake_lock": brake_lock_events,
        "wheelspin": wheelspin_events,
        "oversteer_candidates": oversteer_events,
        "understeer_candidates": understeer_events,
    }

    # --- Lap summary (best from packet field, laps observed) ----------
    best_lap = sd.col("BestLap")
    best = best_lap[best_lap > 0]
    out["laps"] = {
        "best_lap_s": round(float(np.min(best)), 3) if best.size else None,
        "lap_count": int(np.max(sd.col("LapNumber"))) if sd.n else 0,
    }

    return out
