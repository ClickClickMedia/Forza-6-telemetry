"""Two-session comparison and route tracing.

Produces:
    * headline metric deltas (run time, peak/avg speed, max lat/lon g)
    * aligned, downsampled channel traces (speed, throttle, brake, steer, gear,
      tyre slip, suspension travel) for overlay charts
    * an XY route trace built from PositionX / PositionZ, with a per-point
      colour channel (speed or combined rear slip)

Deliberately avoids any assumption that ``TrackOrdinal`` exists (FH6 has no such
field), and supports point-to-point runs where lap fields may be meaningless by
falling back to elapsed capture time as the run duration.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .session_data import SessionData

# Max points per downsampled trace sent to the browser.
TRACE_POINTS = 600
ROUTE_POINTS = 1500


def _downsample(arr: np.ndarray, n_out: int) -> np.ndarray:
    if arr.size <= n_out:
        return arr
    idx = np.linspace(0, arr.size - 1, n_out).astype(int)
    return arr[idx]


def _channel(sd: SessionData, name: str, n_out: int = TRACE_POINTS) -> List[float]:
    return [round(float(v), 4) for v in _downsample(sd.col(name), n_out)]


def _run_time(sd: SessionData) -> float:
    """Best available run duration.

    Uses the packet BestLap if a valid lap exists (circuit racing); otherwise
    falls back to total elapsed capture time (point-to-point runs).
    """
    from .packet import SANE_LAP_MAX_S
    best = sd.col("BestLap")
    valid = best[(best > 0) & (best < SANE_LAP_MAX_S)]
    if valid.size:
        return float(np.min(valid))
    t = sd.col("t_mono")
    return float(t[-1] - t[0]) if t.size > 1 else 0.0


def _summary(sd: SessionData) -> Dict[str, Any]:
    if sd.n == 0:
        return {"frames": 0}
    dt = sd.dt()
    speed_kmh = sd.col("Speed") * 3.6
    lat = np.abs(sd.col("AccelerationX"))
    lon = sd.col("AccelerationZ")
    return {
        "frames": sd.n,
        "run_time_s": round(_run_time(sd), 3),
        "peak_speed_kmh": round(float(np.max(speed_kmh)), 1),
        "avg_speed_kmh": round(float(np.average(speed_kmh, weights=dt)), 1),
        "max_lat_g": round(float(np.max(lat)) / 9.81, 2),
        "max_lon_accel_g": round(float(np.max(lon)) / 9.81, 2),
        "max_lon_brake_g": round(float(np.min(lon)) / 9.81, 2),
    }


def _traces(sd: SessionData) -> Dict[str, Any]:
    if sd.n == 0:
        return {}
    t = _downsample(sd.col("t_mono"), TRACE_POINTS)
    rear_slip = np.maximum(
        sd.col("TireCombinedSlipRearLeft"), sd.col("TireCombinedSlipRearRight")
    )
    front_slip = np.maximum(
        sd.col("TireCombinedSlipFrontLeft"), sd.col("TireCombinedSlipFrontRight")
    )
    susp_avg = (
        sd.col("NormalizedSuspensionTravelFrontLeft")
        + sd.col("NormalizedSuspensionTravelFrontRight")
        + sd.col("NormalizedSuspensionTravelRearLeft")
        + sd.col("NormalizedSuspensionTravelRearRight")
    ) / 4.0
    return {
        "t": [round(float(v), 3) for v in t],
        "speed_kmh": [round(float(v) * 3.6, 1) for v in _downsample(sd.col("Speed"), TRACE_POINTS)],
        "throttle": [round(float(v) / 255.0 * 100, 1) for v in _downsample(sd.col("Accel"), TRACE_POINTS)],
        "brake": [round(float(v) / 255.0 * 100, 1) for v in _downsample(sd.col("Brake"), TRACE_POINTS)],
        "steer": [round(float(v) / 127.0, 3) for v in _downsample(sd.col("Steer"), TRACE_POINTS)],
        "gear": [int(v) for v in _downsample(sd.col("Gear"), TRACE_POINTS)],
        "rear_slip": [round(float(v), 3) for v in _downsample(rear_slip, TRACE_POINTS)],
        "front_slip": [round(float(v), 3) for v in _downsample(front_slip, TRACE_POINTS)],
        "susp_travel": [round(float(v), 3) for v in _downsample(susp_avg, TRACE_POINTS)],
    }


def _route(sd: SessionData, colour_by: str = "speed") -> Dict[str, Any]:
    """XY route trace from PositionX/PositionZ with a per-point colour channel."""
    if sd.n == 0:
        return {"x": [], "z": [], "c": [], "colour_by": colour_by}
    x = _downsample(sd.col("PositionX"), ROUTE_POINTS)
    z = _downsample(sd.col("PositionZ"), ROUTE_POINTS)
    if colour_by == "rear_slip":
        base = np.maximum(
            sd.col("TireCombinedSlipRearLeft"), sd.col("TireCombinedSlipRearRight")
        )
        c = _downsample(base, ROUTE_POINTS)
    else:
        colour_by = "speed"
        c = _downsample(sd.col("Speed") * 3.6, ROUTE_POINTS)
    return {
        "x": [round(float(v), 2) for v in x],
        "z": [round(float(v), 2) for v in z],
        "c": [round(float(v), 2) for v in c],
        "colour_by": colour_by,
    }


def compare(
    sd_a: SessionData,
    sd_b: SessionData,
    meta_a: Dict[str, Any],
    meta_b: Dict[str, Any],
    colour_by: str = "speed",
) -> Dict[str, Any]:
    sum_a = _summary(sd_a)
    sum_b = _summary(sd_b)
    deltas: Dict[str, Any] = {}
    for key in ("run_time_s", "peak_speed_kmh", "avg_speed_kmh",
                "max_lat_g", "max_lon_accel_g", "max_lon_brake_g"):
        va, vb = sum_a.get(key), sum_b.get(key)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            deltas[key] = round(vb - va, 3)
    return {
        "a": {"meta": meta_a, "summary": sum_a, "traces": _traces(sd_a),
              "route": _route(sd_a, colour_by)},
        "b": {"meta": meta_b, "summary": sum_b, "traces": _traces(sd_b),
              "route": _route(sd_b, colour_by)},
        "delta_b_minus_a": deltas,
        "colour_by": colour_by,
    }


def single_route(sd: SessionData, colour_by: str = "speed") -> Dict[str, Any]:
    return _route(sd, colour_by)
