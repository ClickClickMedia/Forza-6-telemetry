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
MIN_LAP_DIST_FRAC = 0.5   # a complete lap must cover >= this x the longest lap's
MIN_LAP_DIST_FLOOR_M = 200.0  # ...distance (kills ~0 m Rivals-restart phantoms)
CORNERING_LAT_G = 0.30    # |lateral g| above this counts as cornering
FULL_THROTTLE = 0.98      # fraction of 255
SHIFT_ON_POWER = 0.90     # throttle floor for an "on-power" upshift (shift point)
BRAKING_MIN = 0.05
LIMITER_RPM_FRAC = 0.97   # fraction of EngineMaxRpm counted as "on limiter"
SUSP_BOTTOM_OUT = 0.98

# Sliding is judged with hysteresis so threshold chatter doesn't inflate
# slide time: a slide starts when combined slip exceeds SLIDE_ENTER, ends
# when it falls below SLIDE_EXIT, and only counts if it lasts SLIDE_MIN_S.
SLIDE_ENTER = 1.05
SLIDE_EXIT = 0.90
SLIDE_MIN_S = 0.10
COMBINED_SLIP_HIGH = 1.0  # legacy single threshold (analysis page events)

# Discrete traction events are grouped: a new event requires the condition
# to hold for EVENT_MIN_S and a EVENT_GAP_S recovery since the previous one.
EVENT_MIN_S = 0.10
EVENT_GAP_S = 0.30

# Observed peaks (power/torque) count only "valid pull" samples: near-full
# throttle, engine well above idle, sustained a few frames — so a collision
# spike or downshift transient can't masquerade as the car's output.
PEAK_THROTTLE = 0.95
PEAK_SUSTAIN_FRAMES = 3

# Lateral-G reporting: raw max is polluted by collisions/landings, so the
# headline "cornering max" is the 99th percentile of spike-filtered samples
# (frame-to-frame |Δlat| above LAT_SPIKE_G excluded, airborne excluded).
LAT_SPIKE_G = 2.0
AIRBORNE_SUSP = 0.02      # all four corners below this = wheels off ground

# Free-roam time-attack run detection (validated against real captures):
# entering an event grids the car behind the start line with NEGATIVE
# DistanceTraveled; it crosses 0 at launch and snap-resets on event exit.
RUN_NEG_DIST_M = -5.0     # staged-behind-line signature
RUN_SNAP_DROP_M = -50.0   # a frame-to-frame drop this large = event boundary
RUN_MIN_S = 30.0          # ignore shorter "runs"
RUN_MIN_ROUTE_M = 500.0   # ignore trivial route distances

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


def _mask_spans(mask: np.ndarray) -> List[tuple]:
    """Contiguous True spans of ``mask`` as (start, end) index pairs."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([0], mask.astype(np.int8), [0]))
    diffs = np.diff(padded)
    return list(zip(np.where(diffs == 1)[0], np.where(diffs == -1)[0]))


def _grouped_events(mask: np.ndarray, dt: np.ndarray,
                    min_s: float = EVENT_MIN_S,
                    gap_s: float = EVENT_GAP_S) -> Dict[str, Any]:
    """Group a boolean condition into discrete events with durations.

    An event must last at least ``min_s``; consecutive spans separated by
    less than ``gap_s`` merge into one event (recovery period). Returns
    count, total time, and the longest single event — the numbers that make
    "180 wheelspin events" actually interpretable.
    """
    spans = _mask_spans(mask)
    if not spans:
        return {"events": 0, "total_s": 0.0, "longest_s": 0.0}
    cum = np.concatenate(([0.0], np.cumsum(dt)))
    t0 = lambda i: cum[i]  # noqa: E731
    merged: List[List[float]] = []
    for s, e in spans:
        start, end = t0(s), t0(min(e, len(cum) - 1))
        if merged and start - merged[-1][1] < gap_s:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    durations = [e - s for s, e in merged if (e - s) >= min_s]
    return {
        "events": len(durations),
        "total_s": round(float(sum(durations)), 2),
        "longest_s": round(float(max(durations)) if durations else 0.0, 2),
    }


def _hysteresis_mask(x: np.ndarray, enter: float, exit_: float) -> np.ndarray:
    """True while ``x`` is in a slide: enters above ``enter``, exits below
    ``exit_``. Prevents threshold chatter from counting one slide many times."""
    out = np.zeros(x.size, dtype=bool)
    active = False
    for i, v in enumerate(x):
        if active:
            active = v > exit_
        else:
            active = v >= enter
        out[i] = active
    return out


def detect_runs(sd: SessionData) -> List[Dict[str, Any]]:
    """Detect free-roam time-attack runs from DistanceTraveled behaviour.

    Validated against real FH6 captures: entering an event stages the car
    behind the start line with *negative* DistanceTraveled, which crosses 0
    at launch and snap-resets when the event ends (or on a restart). Lap
    fields stay 0 in these events, so this is the only run signal.
    """
    if sd.n < 10:
        return []
    dist = sd.col("DistanceTraveled")
    t = sd.col("t_mono")
    d = np.diff(dist)
    boundaries = sorted(np.where(d < RUN_SNAP_DROP_M)[0] + 1)

    # DistanceTraveled is used ONLY for boundary detection (staging and the
    # end-of-event snap). Its magnitude is not trustworthy as route length —
    # a real circuit capture showed it advancing ~2.9x faster than the car
    # actually moved — so route length is integrated from speed instead.
    speed_dt = sd.col("Speed") * sd.dt()

    runs: List[Dict[str, Any]] = []

    def _emit(start: int, end: int) -> None:
        duration = float(t[end - 1] - t[start])
        route = float(np.sum(speed_dt[start:end])) if end > start else 0.0
        if duration >= RUN_MIN_S and route >= RUN_MIN_ROUTE_M:
            runs.append({"i0": int(start), "i1": int(end),
                         "time_s": round(duration, 3),
                         "route_m": round(route, 1)})

    # Variant 1 — staged behind the line: a contiguous negative-distance
    # span dipping below -50 m; the run starts where the span ends (the
    # start-line crossing — distance climbs gradually through 0, so the
    # frame before crossing may be only -3 m). The span must contain a
    # near-stationary moment: driving PAST an event entry point also sends
    # negative distance (route preview), but at road speed.
    speed_col = sd.col("Speed")
    covered: List[int] = []
    for s, e in _mask_spans(dist < 0):
        if e >= sd.n:
            continue
        if float(np.min(dist[s:e])) > -50.0:
            continue  # jitter around zero, not a staged start
        if float(np.min(speed_col[s:e])) > 3.0:
            continue  # drove past an entry point — never actually staged
        start = e
        after = [b for b in boundaries if b > start]
        end = after[0] if after else sd.n
        _emit(start, end)
        covered.append(start)

    # Variant 2 — staged AT zero (observed on route-blueprint time attacks):
    # the event teleports the car in with DistanceTraveled pinned to 0, it
    # holds 0 while staged, climbs through the run, and snap-resets at the
    # finish. Detect: a snap-boundary segment that OPENS below 1 m, holds
    # there ≥ 0.5 s, then climbs. Run starts at the end of the hold.
    seg_starts = [0] + boundaries
    seg_ends = boundaries + [sd.n]
    for s0, s1 in zip(seg_starts, seg_ends):
        if s1 - s0 < 60 or float(dist[s0]) >= 1.0:
            continue
        if any(s0 <= c < s1 for c in covered):
            continue  # already found via variant 1
        hold = s0
        while hold < s1 and dist[hold] < 1.0:
            hold += 1
        if hold - s0 < 30 or hold >= s1:  # need a real staging hold (~0.5 s)
            continue
        _emit(hold - 1, s1)

    runs.sort(key=lambda r: r["i0"])
    return runs


# Position-gate lap splitting: a staged run that repeatedly revisits its
# own path travelling the same direction is a circuit, and those returns
# are the lap boundaries. Position is continuous truth — unlike
# DistanceTraveled it survives rewinds, which snap distance and falsely
# split a race into "runs". The gate is DISCOVERED from the trajectory
# (earliest revisited point), never anchored at the launch frame: staged
# events broadcast Position (0,0) while loading, grids can sit on a spur
# off the racing loop, and a recording may begin mid-lap. Validated on
# real captures incl. a 5-lap Legends Isle event whose reconstructed best
# landed 0.090 s from the driver's manually-read lap time.
GATE_RADIUS_M = 25.0          # gate capture radius
GATE_HEADING_DOT = 0.5        # ±60° of the gate-pass direction
GATE_MIN_SPEED = 8.0          # m/s — must be driving through, not staged
GATE_COOLDOWN_ROUTE_M = 500.0  # min route between line crossings
LAP_ROUTE_RATIO_MAX = 1.25    # complete laps must be this consistent
PARTIAL_MIN_FRACTION = 0.3    # leading/trailing remainder worth a partial row
_LOOP_SCAN_STRIDE = 3         # discovery scan decimation (crossings full-res)


def detect_position_laps(sd: SessionData,
                         runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Split staged runs into laps at repeated same-direction path returns.

    The gate is discovered as the earliest trajectory point the car
    revisits (same direction, at speed, >= 500 m of route in between) —
    robust to zeroed staging positions, off-loop grids and recordings
    that begin mid-lap. Crossing times are interpolated between frames.
    Returns [] unless >= 2 consistent complete loops exist — a
    point-to-point run never revisits its path and is left untouched.
    """
    if not runs:
        return []
    px, pz = sd.col("PositionX"), sd.col("PositionZ")
    speed = sd.col("Speed")
    t = sd.col("t_mono")
    route = np.cumsum(speed * sd.dt())

    i0, end = runs[0]["i0"], runs[-1]["i1"]

    # Per-frame heading over a ~0.5 s forward window.
    w = 30
    fx = np.empty(sd.n)
    fz = np.empty(sd.n)
    fx[:-w], fz[:-w] = px[w:] - px[:-w], pz[w:] - pz[:-w]
    fx[-w:], fz[-w:] = fx[-w - 1], fz[-w - 1]
    fnorm = np.hypot(fx, fz)

    # Frames that count: driving, with a real heading, and a real position
    # (staged events broadcast exactly (0,0) while the world loads).
    valid = (speed > GATE_MIN_SPEED) & (fnorm > 1.0) \
        & ~((px == 0.0) & (pz == 0.0))

    # --- Gate discovery: earliest revisited point (grid-hashed scan) -----
    R = GATE_RADIUS_M
    cells: Dict[tuple, List[int]] = {}
    gate_i = -1
    for j in range(i0, end, _LOOP_SCAN_STRIDE):
        if not valid[j]:
            continue
        cx, cz = int(px[j] // R), int(pz[j] // R)
        for dxz in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1),
                    (-1, -1), (-1, 1), (1, -1), (1, 1)):
            for i in cells.get((cx + dxz[0], cz + dxz[1]), ()):
                if (route[j] - route[i] >= GATE_COOLDOWN_ROUTE_M
                        and np.hypot(px[j] - px[i], pz[j] - pz[i]) < R
                        and (fx[j] * fx[i] + fz[j] * fz[i])
                        / (fnorm[j] * fnorm[i]) > GATE_HEADING_DOT):
                    gate_i = i
                    break
            if gate_i >= 0:
                break
        if gate_i >= 0:
            break
        cells.setdefault((cx, cz), []).append(j)
    if gate_i < 0:
        return []

    def _crossings_at(anchor: int):
        """All same-direction passes of the anchor point, with sub-frame
        crossing times (along-track coordinate zero-crossing)."""
        gx, gz = float(px[anchor]), float(pz[anchor])
        ghx = fx[anchor] / fnorm[anchor]
        ghz = fz[anchor] / fnorm[anchor]
        dist_gate = np.hypot(px - gx, pz - gz)
        dot = (fx * ghx + fz * ghz) / np.maximum(fnorm, 1e-9)
        ok = (dist_gate < R) & (dot > GATE_HEADING_DOT) & valid
        ok[:i0] = False
        ok[end:] = False

        def _cross_time(c: int) -> float:
            for a, b in ((c - 1, c), (c, c + 1)):
                if 0 <= a and b < sd.n:
                    s0 = (px[a] - gx) * ghx + (pz[a] - gz) * ghz
                    s1 = (px[b] - gx) * ghx + (pz[b] - gz) * ghz
                    if s0 <= 0.0 < s1:
                        frac = -s0 / (s1 - s0) if s1 != s0 else 0.0
                        return float(t[a] + frac * (t[b] - t[a]))
            s_here = (px[c] - gx) * ghx + (pz[c] - gz) * ghz
            return float(t[c]) - (float(s_here) / max(float(speed[c]), 0.1))

        crossings: List[int] = []
        cross_t: List[float] = []
        for s, e in _mask_spans(ok):
            c = int(s + np.argmin(dist_gate[s:e]))
            if not crossings or route[c] - route[crossings[-1]] >= GATE_COOLDOWN_ROUTE_M:
                crossings.append(c)
                cross_t.append(_cross_time(c))
        return crossings, cross_t

    # Phase choice: any fixed loop point yields true full-loop times, but
    # the phase decides how the event's edges are attributed. The
    # discovered gate is kept when its final crossing lands near the event
    # end (the phase is already finish-aligned — typical when the grid
    # sits on the start/finish line). When it instead strands a large
    # untimed tail, re-anchor near the event END — the finish is the one
    # point the game pins — which rescues the real final lap from being
    # mis-attributed (validated on a 5-lap event where the driver's
    # manually-read final lap confirmed the finish-phased split).
    crossings, cross_t = _crossings_at(gate_i)
    if len(crossings) >= 3:
        lap_routes = np.diff([route[c] for c in crossings])
        tail = float(route[end - 1] - route[crossings[-1]])
        if tail > 0.25 * float(np.median(lap_routes)):
            last_valid = end - 1
            while last_valid > gate_i and not valid[last_valid]:
                last_valid -= 1
            back = last_valid
            while back > gate_i and t[last_valid] - t[back] < 1.5:
                back -= 1
            while back > gate_i and not valid[back]:
                back -= 1
            if back > gate_i:
                cand_cross, cand_t = _crossings_at(back)
                if len(cand_cross) >= len(crossings):
                    crossings, cross_t = cand_cross, cand_t
    if len(crossings) < 3:  # need >= 2 complete loops to call it a circuit
        return []

    # A lap spanning the gap between two detected runs contains a rewind
    # (the snap that split the runs) — its elapsed time includes re-driven
    # road and is invalid for performance comparison, though the telemetry
    # remains real.
    rewind_marks = [r["i1"] for r in runs[:-1]]
    laps: List[Dict[str, Any]] = []
    for (a, ta), (b, tb) in zip(zip(crossings, cross_t),
                                zip(crossings[1:], cross_t[1:])):
        laps.append({
            "i0": a, "i1": b,
            "time_s": round(tb - ta, 3),
            "route_m": round(float(route[b] - route[a]), 1),
            "complete": True,
            "rewind_affected": any(a < m <= b for m in rewind_marks),
        })
    routes = [l["route_m"] for l in laps]
    if min(routes) < RUN_MIN_ROUTE_M or any(
            l["time_s"] < MIN_LAP_S for l in laps):
        return []
    if max(routes) / max(min(routes), 1.0) > LAP_ROUTE_RATIO_MAX:
        return []  # inconsistent loop lengths — not the same circuit
    median_route = float(np.median(routes))

    # Leading remainder: the recording (or the launch spur) before the
    # first gate pass — a real partial lap when it covers real distance.
    lead_start = i0
    while lead_start < crossings[0] and not valid[lead_start]:
        lead_start += 1
    lead_route = float(route[crossings[0]] - route[lead_start])
    if lead_route > PARTIAL_MIN_FRACTION * median_route:
        laps.insert(0, {
            "i0": int(lead_start), "i1": crossings[0],
            "time_s": round(cross_t[0] - float(t[lead_start]), 3),
            "route_m": round(lead_route, 1),
            "complete": False,
        })

    # Trailing remainder after the last crossing: a real partial lap, or
    # just the few frames between the finish line and the event snap.
    remainder = float(route[end - 1] - route[crossings[-1]])
    if remainder > PARTIAL_MIN_FRACTION * median_route:
        laps.append({
            "i0": crossings[-1], "i1": end,
            "time_s": round(float(t[end - 1]) - cross_t[-1], 3),
            "route_m": round(remainder, 1),
            "complete": False,
        })
    return laps


def canonical_drivetrain(sd: SessionData) -> int:
    """THE drivetrain value for a session: the modal DrivetrainType over
    identity-valid frames (CarOrdinal != 0).

    Never sample a single frame for this — loading/menu/results frames
    zero every identity field, and DrivetrainType 0 means FWD, so one bad
    frame silently pointed the traction analyser at the wrong axle of an
    RWD car (real capture: 240 zeroed frames bracketing 13k honest ones).
    """
    if "DrivetrainType" not in sd or sd.n == 0:
        return -1
    dtt = sd.col("DrivetrainType")
    if "CarOrdinal" in sd:
        valid = dtt[sd.col("CarOrdinal") != 0]
        if valid.size:
            dtt = valid
    vals, counts = np.unique(dtt.astype(int), return_counts=True)
    return int(vals[np.argmax(counts)])


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
        # A complete lap is one whose lap number ADVANCED at its end: not the
        # trailing partial, and not a segment cut short by a LapNumber RESET
        # (Rivals restarts drop it back to 0 — that truncated lap isn't real).
        complete = (k < len(bounds) - 2
                    and int(lap_no[i1]) > int(lap_no[i0]))
        segments.append({
            "lap": int(lap_no[i0]),
            "i0": i0,
            "i1": i1,
            "complete": complete,
        })
    return segments


def _lap_time(sd: SessionData, seg: Dict[str, Any]) -> Optional[float]:
    """Best available lap time for a segment.

    On completion Horizon writes the finished lap's time into ``LastLap`` of
    the *next* frames; ``CurrentLap`` at the last in-segment frame is the
    fallback (slightly short by up to one frame interval).

    (Rivals restart phantoms — ~0-distance segments carrying a stale ``LastLap``
    — are filtered by the distance/restart guards in ``lap_report``, not here;
    ``CurrentLap`` can't be used to reject them because some genuine laps ship
    with ``CurrentLap`` pinned at 0.)
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
    t_all = sd.col("t_mono")
    t_base = float(t_all[0]) if sd.n else 0.0
    t_start = round(float(t_all[i0]) - t_base, 2) if i1 > i0 else 0.0
    t_end = round(float(t_all[max(i1 - 1, i0)]) - t_base, 2) if i1 > i0 else 0.0
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

    # Driven wheels follow the drivetrain — watching the wrong axle scored
    # a FWD car "0 wheelspin" while its fronts spun for 30 s (real capture).
    dt_type = canonical_drivetrain(sd)
    driven_idx = {0: [0, 1], 1: [2, 3], 2: [0, 1, 2, 3]}.get(dt_type, [0, 1, 2, 3])
    driven_slip = np.maximum.reduce([slip_ratio[i] for i in driven_idx])

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

    # Balance by corner phase: WHERE the understeer lives changes the fix
    # (entry -> brake balance/damping, mid -> ARB/springs/aero, power-on ->
    # diff/pressures, lift -> rebound/decel diff).
    phase_masks = {
        "entry": grip_corner & (brake > 0.15) & (accel < 0.2),
        "mid": grip_corner & (brake < 0.10) & (accel >= 0.05) & (accel < 0.5),
        "exit": grip_corner & (accel >= 0.5),
        "lift": grip_corner & (accel < 0.05) & (brake < 0.05),
    }
    phases: Dict[str, Any] = {}
    for pname, pmask in phase_masks.items():
        if int(np.sum(pmask)) >= 30:  # ~0.5 s of evidence minimum
            phases[pname] = {
                "usi": round(float(np.mean(front_sa[pmask]) - np.mean(rear_sa[pmask])), 3),
                "time_s": round(float(np.sum(dt[pmask])), 1),
            }
        else:
            phases[pname] = {"usi": None, "time_s": round(float(np.sum(dt[pmask])), 1)}

    # Tyre-temp statistics use ACTIVE-DRIVING frames only (>=40 km/h):
    # stationary time, menus and cool-down laps otherwise drag the averages
    # away from what the tyres do when they matter. Falls back to all frames
    # if the session has almost no active driving.
    active = speed_ms >= 11.1
    if float(np.sum(dt[active])) < 0.05 * float(np.sum(dt)):
        active = np.ones(speed_ms.size, dtype=bool)
    dt_active = dt[active]

    def _awavg(arr: np.ndarray) -> float:
        return float(np.average(arr[active], weights=dt_active)) if dt_active.size else 0.0

    def _amed(arr: np.ndarray) -> float:
        return float(np.median(arr[active])) if dt_active.size else 0.0

    front_temps = np.concatenate([temps_c[0][active], temps_c[1][active]])
    rear_temps = np.concatenate([temps_c[2][active], temps_c[3][active]])

    rear_comb = np.maximum(combined[2], combined[3])
    front_comb = np.maximum(combined[0], combined[1])

    # Slide time with hysteresis (enter 1.05 / exit 0.90) so chatter around
    # the grip limit doesn't inflate the numbers.
    front_slide = _hysteresis_mask(front_comb, SLIDE_ENTER, SLIDE_EXIT)
    rear_slide = _hysteresis_mask(rear_comb, SLIDE_ENTER, SLIDE_EXIT)
    front_slide_g = _grouped_events(front_slide, dt, min_s=SLIDE_MIN_S)
    rear_slide_g = _grouped_events(rear_slide, dt, min_s=SLIDE_MIN_S)

    moving = speed_ms > 3.0
    no_handbrake = handbrake < 0.1
    spin_gate = (accel > 0.4) & moving & no_handbrake
    any_spin = (driven_slip > 0.5) & spin_gate
    wheelspin_g = _grouped_events(any_spin, dt)

    # Wheelspin buckets are MUTUALLY EXCLUSIVE raw-mask times so they
    # reconcile exactly: sum(per-wheel-only) + multiple == total, and
    # turning + straight == total. (Grouped values are kept only for the
    # burst count/longest.) The diff question hinges on the split:
    # one-wheel flare wants MORE diff lock; all-wheel spin wants less
    # power or more tyre.
    spin_total = round(float(np.sum(dt[any_spin])), 1)
    wheel_spin_masks = {i: (slip_ratio[i] > 0.5) & spin_gate for i in driven_idx}
    n_spinning = np.zeros(speed_ms.size, dtype=int)
    for m in wheel_spin_masks.values():
        n_spinning += m.astype(int)
    spin_by_wheel = {
        WHEELS[i]: round(float(np.sum(dt[wheel_spin_masks[i] & (n_spinning == 1)])), 1)
        for i in driven_idx
    }
    spin_multi = round(float(np.sum(dt[any_spin & (n_spinning >= 2)])), 1)
    turning = np.abs(steer) > 0.15
    spin_turning = round(float(np.sum(dt[any_spin & turning])), 1)
    spin_straight = round(float(np.sum(dt[any_spin & ~turning])), 1)

    # Inside vs outside driven-wheel single-wheel flare — THE diff-tuning
    # signal. A wheel is "inside" when its side matches the steer
    # direction (left wheels while steering left, right wheels while
    # steering right). Inside-wheel flare on exit argues for more diff
    # acceleration lock; outside/both-wheel argues power vs tyre instead.
    single_turn = (n_spinning == 1) & turning
    left_wheels = {0, 2}
    spin_inside = spin_outside = 0.0
    for i in driven_idx:
        side_is_left = i in left_wheels
        inside = single_turn & wheel_spin_masks[i] & (
            (steer < 0) if side_is_left else (steer > 0))
        outside = single_turn & wheel_spin_masks[i] & ~(
            (steer < 0) if side_is_left else (steer > 0))
        spin_inside += float(np.sum(dt[inside]))
        spin_outside += float(np.sum(dt[outside]))
    spin_inside = round(spin_inside, 1)
    spin_outside = round(spin_outside, 1)

    # --- Absolute saturation + balance oscillation -----------------------
    # A near-zero understeer index can sit on a violently bimodal signal:
    # when both axles are past the grip limit the limiting end flips
    # front<->rear many times a second, which the driver feels as
    # "psychotic". Count those reversals (hysteresis ±0.5 on the axle slip
    # difference so chatter doesn't inflate it) over cornering time.
    both_axles_saturated = bool(front_sa_corner > 1.0 and rear_sa_corner > 1.0)
    bal_diff = front_comb - rear_comb
    lim = np.zeros(bal_diff.size, dtype=np.int8)
    lim[bal_diff > 0.5] = 1
    lim[bal_diff < -0.5] = -1
    nz = lim[cornering]
    nz = nz[nz != 0]
    reversals = int(np.sum(np.abs(np.diff(nz)) == 2)) if nz.size > 1 else 0
    corner_time = float(np.sum(dt[cornering]))
    reversal_rate = (round(reversals / (corner_time / 60.0), 1)
                     if corner_time > 5.0 else 0.0)

    # --- Slide time split by throttle state (opposite fixes) -------------
    # Power-on slide is the engine overwhelming the tyres (diff/throttle
    # fix); off-throttle slide is an entry/trail-braking balance slide
    # (alignment/ARB fix). Blending them into one number hides the fork.
    any_axle_slide = front_slide | rear_slide
    slide_power_on_s = round(
        float(np.sum(dt[any_axle_slide & (accel > 0.4) & moving])), 1)
    slide_off_throttle_s = round(
        float(np.sum(dt[any_axle_slide & (accel < 0.05) & moving])), 1)

    # --- Front/rear slide overlap + event-duration shape -----------------
    both_slide = front_slide & rear_slide
    slide_overlap_s = round(float(np.sum(dt[both_slide])), 1)
    _move_t = float(np.sum(dt[moving]))
    four_wheel_slide_pct = (round(float(np.sum(dt[both_slide & moving]))
                                  / _move_t * 100.0, 1) if _move_t > 0 else 0.0)
    _spans = _mask_spans(any_axle_slide)
    _durs = np.array([float(np.sum(dt[s:e])) for s, e in _spans]) \
        if _spans else np.array([])
    slide_event_median_s = round(float(np.median(_durs)), 2) if _durs.size else 0.0
    slide_pct_under_half = (round(float(np.mean(_durs < 0.5)) * 100.0, 0)
                            if _durs.size else 0.0)

    # --- Tyre temperature trend across the session -----------------------
    tm = sd.col("t_mono")[sl]

    def _axle_temp_slope(a: int, b: int):  # °C per minute over active driving
        y = (temps_c[a][active] + temps_c[b][active]) / 2.0
        x = tm[active]
        if x.size < 30 or float(x[-1] - x[0]) < 30.0:
            return None
        return round(float(np.polyfit(x - x[0], y, 1)[0]) * 60.0, 1)

    front_temp_slope = _axle_temp_slope(0, 1)
    rear_temp_slope = _axle_temp_slope(2, 3)
    _front_axle_active = (temps_c[0][active] + temps_c[1][active]) / 2.0
    _rear_axle_active = (temps_c[2][active] + temps_c[3][active]) / 2.0
    front_pct_over_window = (round(float(np.mean(_front_axle_active > TEMP_HOT_C))
                                   * 100.0, 0) if _front_axle_active.size else 0.0)
    rear_pct_over_window = (round(float(np.mean(_rear_axle_active > TEMP_HOT_C))
                                  * 100.0, 0) if _rear_axle_active.size else 0.0)

    def _temp_tag(slope):
        if slope is None:
            return "unknown"
        if slope >= 3.0:
            return "runaway (still climbing)"
        if slope <= -3.0:
            return "cooling"
        return "steady"

    # --- Body-control state: squat / dive / roll -------------------------
    # Pitch and roll excursions the avg/max travel numbers don't reveal —
    # the metrics that show whether stiffer springs/bars/dampers actually
    # calmed the platform. (0 = extended, 1 = bottomed.)
    susp_f = (susp[0] + susp[1]) / 2.0
    susp_r = (susp[2] + susp[3]) / 2.0
    on_power = (accel > 0.6) & moving
    on_brake = (brake > 0.3) & moving
    squat_rear_minus_front = (round(float(np.mean((susp_r - susp_f)[on_power])), 3)
                              if int(np.sum(on_power)) else None)
    dive_front_minus_rear = (round(float(np.mean((susp_f - susp_r)[on_brake])), 3)
                             if int(np.sum(on_brake)) else None)
    corner_m = cornering & moving
    roll_front_p95 = (round(float(np.percentile(np.abs(susp[0] - susp[1])[corner_m], 95)), 3)
                      if int(np.sum(corner_m)) else None)
    roll_rear_p95 = (round(float(np.percentile(np.abs(susp[2] - susp[3])[corner_m], 95)), 3)
                     if int(np.sum(corner_m)) else None)

    # --- Steering saturation: over-driving / full-lock exposure ----------
    # Time spent at near-full steering lock while cornering; a high share
    # is the driver at the limit (or the car forcing it), not a setup lever.
    full_lock = (np.abs(steer) >= 0.95) & cornering
    full_lock_pct = (round(float(np.sum(dt[full_lock])) / corner_time * 100.0, 1)
                     if corner_time > 5.0 else 0.0)

    # Brake lock via WHEEL-SPEED DEFICIT — the honest detector. Forza's
    # normalized slip ratio crosses -0.5 during ordinary hard braking with
    # no lockup (verified: a session with 5-6 s of slip<-0.5 showed 0.0 s
    # of actual wheel-stoppage), so slip thresholds massively over-count.
    # Per-wheel rolling constant k = rot/speed calibrated on free-rolling
    # frames; lock = wheel turning at <40% of expected for the road speed.
    wheel_rot = _wheel_cols(sd, "WheelRotationSpeed")
    wheel_rot = [w[sl] for w in wheel_rot]
    free_roll = (brake < 0.05) & (accel < 0.05) & (speed_ms > 8.0)
    braking_time_s = float(np.sum(dt[brake >= BRAKING_MIN]))
    lock_masks: List[np.ndarray] = []
    lock_method = "wheel-speed deficit"
    if int(np.sum(free_roll)) >= 50:
        for w in wheel_rot:
            k = float(np.median(w[free_roll] / speed_ms[free_roll]))
            expected = k * np.maximum(speed_ms, 0.1)
            deficit = 1.0 - (w / expected)
            lock_masks.append(
                (deficit > 0.6) & (brake > 0.3) & (speed_ms > 5.0) & no_handbrake
            )
    else:  # not enough coasting to calibrate — legacy approximation
        lock_method = "slip-ratio approximation (insufficient coasting to calibrate)"
        for r in slip_ratio:
            lock_masks.append((r < -0.9) & (brake > 0.6) & moving & no_handbrake)
    lock_front_mask = lock_masks[0] | lock_masks[1]
    lock_rear_mask = lock_masks[2] | lock_masks[3]
    lock_front_g = _grouped_events(lock_front_mask, dt)
    lock_rear_g = _grouped_events(lock_rear_mask, dt)
    lock_any_raw = round(float(np.sum(dt[lock_front_mask | lock_rear_mask])), 2)
    lock_pct_of_braking = round(
        min(100.0, lock_any_raw / braking_time_s * 100.0), 1
    ) if braking_time_s > 0.5 else 0.0

    # Time braking AT the lock threshold (deep slip excursions with the
    # wheels still turning) — the ABS-modulation signature. Not a fault:
    # with ABS on it means braking at the grip ceiling. Reported separately
    # from sustained lock so neither masquerades as the other.
    near_lock_mask = (
        np.logical_or.reduce([r < -0.5 for r in slip_ratio])
        & (brake > 0.6) & moving & no_handbrake
    )
    near_lock_s = round(float(np.sum(dt[near_lock_mask])), 1)
    near_lock_pct = round(
        min(100.0, near_lock_s / braking_time_s * 100.0), 1
    ) if braking_time_s > 0.5 else 0.0

    # Channels the game broadcasts identically (e.g. rear tyre temps on
    # many cars) — worth disclosing so nobody chases a phantom asymmetry.
    rear_temps_identical = bool(np.mean(
        sd.col("TireTempRearLeft")[sl] == sd.col("TireTempRearRight")[sl]
    ) > 0.995) if i1 > i0 else False

    max_travel = np.maximum.reduce([s for s in susp])
    bottom_mask = max_travel > SUSP_BOTTOM_OUT
    bottom_out = _count_runs(bottom_mask)  # sustained >=3 frames = ~50 ms
    bottom_raw_crossings = _count_runs(bottom_mask, min_len=1)
    bottom_longest = _grouped_events(bottom_mask, dt, min_s=0.0, gap_s=0.0)["longest_s"]
    time_at_bottom = round(float(np.sum(dt[bottom_mask])), 2)
    travel_p99 = round(float(np.percentile(max_travel, 99)), 3) if max_travel.size else 0.0

    # Lateral G: raw max is kept but excluded from handling headlines —
    # collisions/landings spike it. Two filtered views:
    #   * p99 of clean frames (legacy), and
    #   * SUSTAINED cornering grip — the best lateral-G held continuously
    #     for 0.4 s while actually steering at speed. Kerbs, banking blips
    #     and compressions can't reach it; this is the tuning-grade number.
    airborne = np.minimum.reduce([s for s in susp]) < AIRBORNE_SUSP
    spike = np.concatenate(([False], np.abs(np.diff(lat_g)) > LAT_SPIKE_G))
    lat_clean = lat_g[~(airborne | spike)]
    lat_p99 = round(float(np.percentile(lat_clean, 99)), 2) if lat_clean.size else 0.0

    corner_valid = (~(airborne | spike)) & (np.abs(steer) > 0.08) & (speed_ms > 16.7)
    lat_gated = np.where(corner_valid, lat_g, 0.0)
    med_dt = float(np.median(dt)) if dt.size else 1 / 60
    w = max(2, int(round(0.4 / max(med_dt, 1e-3))))
    if lat_gated.size >= w:
        windows = np.lib.stride_tricks.sliding_window_view(lat_gated, w)
        lat_sustained = round(float(windows.min(axis=1).max()), 2)
    else:
        lat_sustained = 0.0

    # Observed peaks: instantaneous wire power/torque during valid pulls
    # only (near-full throttle, engine well above idle, sustained). These
    # are session observations, NOT the garage's rated build figures.
    power_w = sd.col("Power")[sl]
    torque_nm = sd.col("Torque")[sl]
    idle = sd.col("EngineIdleRpm")[sl]
    pull = (accel >= PEAK_THROTTLE) & (rpm > np.maximum(idle * 1.2, 1500.0))
    if PEAK_SUSTAIN_FRAMES > 1 and pull.size >= PEAK_SUSTAIN_FRAMES:
        sustained = pull.copy()
        for k in range(1, PEAK_SUSTAIN_FRAMES):
            sustained[k:] &= pull[:-k]
    else:
        sustained = pull
    peak_power_kw = round(float(np.max(power_w[sustained])) / 1000.0, 1) if np.any(sustained) else None
    peak_torque = round(float(np.max(torque_nm[sustained])), 0) if np.any(sustained) else None

    on_limiter = (rpm_max > 0) & (rpm >= rpm_max * LIMITER_RPM_FRAC)

    upshift_idx = np.where(np.diff(gear) > 0)[0] if gear.size > 1 else np.array([])
    shift_rpms = rpm[upshift_idx] if upshift_idx.size else np.array([])
    # The tuning-relevant number is the ON-POWER shift point: part-throttle
    # short-shifts out of slow corners drag the plain mean well below where the
    # driver actually shifts at full throttle, so gate on throttle and report
    # the median (a tight cluster) separately from the all-upshift average.
    ft_up = upshift_idx[accel[upshift_idx] >= SHIFT_ON_POWER] if upshift_idx.size else upshift_idx
    ft_shift_rpms = rpm[ft_up] if ft_up.size else np.array([])
    shift_rpm_full = (round(float(np.median(ft_shift_rpms)), 0)
                      if ft_shift_rpms.size >= 3 else None)
    # Shift-point consistency: p10–p90 spread of upshift RPM. A wide spread
    # is a driver signal (inconsistent shift points), not a tune signal.
    shift_rpm_spread = (round(float(np.percentile(shift_rpms, 90)
                                    - np.percentile(shift_rpms, 10)), 0)
                        if shift_rpms.size >= 5 else None)

    # Share of moving time spent at >=90% of the session's own observed
    # peak power — honest utilisation (relative to what was demonstrated,
    # never to unbroadcast garage figures).
    pct_at_peak_power = None
    if peak_power_kw:
        at_peak = power_w >= 0.9 * peak_power_kw * 1000.0
        moving_time = float(np.sum(dt[speed_ms > 3.0]))
        if moving_time > 1.0:
            pct_at_peak_power = round(
                float(np.sum(dt[at_peak & (speed_ms > 3.0)]))
                / moving_time * 100.0, 1)

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
                "avg": round(_awavg(t), 1),
                "median": round(_amed(t), 1),
                "max": round(float(np.max(t)) if t.size else 0.0, 1),
            }
            for w, t in zip(WHEELS, temps_c)
        },
        "temps_active_driving_only": True,
        "temp_front_avg_c": round(float(np.mean(front_temps)), 1),
        "temp_rear_avg_c": round(float(np.mean(rear_temps)), 1),
        "temp_fr_delta_c": round(
            float(np.mean(front_temps) - np.mean(rear_temps)), 1
        ),
        "temp_front_slope_c_per_min": front_temp_slope,
        "temp_rear_slope_c_per_min": rear_temp_slope,
        "temp_front_pct_over_window": front_pct_over_window,
        "temp_rear_pct_over_window": rear_pct_over_window,
        "temp_front_trend": _temp_tag(front_temp_slope),
        "temp_rear_trend": _temp_tag(rear_temp_slope),
        "balance": {
            "understeer_index": round(usi, 4),
            "front_slip_angle_corner_avg": round(front_sa_corner, 4),
            "rear_slip_angle_corner_avg": round(rear_sa_corner, 4),
            "both_axles_saturated": both_axles_saturated,
            "reversal_rate_per_min": reversal_rate,
            "pct_cornering": round(_pct(cornering, dt), 1),
            "pct_drifting": round(_pct(drifting, dt), 1),
            "front_slide_time_s": front_slide_g["total_s"],
            "rear_slide_time_s": rear_slide_g["total_s"],
            "front_slide_events": front_slide_g["events"],
            "rear_slide_events": rear_slide_g["events"],
            "front_slide_longest_s": front_slide_g["longest_s"],
            "rear_slide_longest_s": rear_slide_g["longest_s"],
            "slide_overlap_s": slide_overlap_s,
            "four_wheel_slide_pct": four_wheel_slide_pct,
            "slide_event_median_s": slide_event_median_s,
            "slide_pct_under_half_s": slide_pct_under_half,
            "phases": phases,
        },
        "traction": {
            "drivetrain": {0: "FWD", 1: "RWD", 2: "AWD"}.get(dt_type, "?"),
            "driven_wheels": [WHEELS[i] for i in driven_idx],
            "driven_slip_peak": round(float(np.max(driven_slip)) if driven_slip.size else 0.0, 2),
            "driven_slip_p95": round(float(np.percentile(driven_slip, 95)) if driven_slip.size else 0.0, 2),
            "wheelspin_events": wheelspin_g["events"],
            "wheelspin_total_s": spin_total,
            "wheelspin_longest_s": wheelspin_g["longest_s"],
            "wheelspin_by_wheel_s": spin_by_wheel,
            "wheelspin_multi_s": spin_multi,
            "wheelspin_turning_s": spin_turning,
            "wheelspin_straight_s": spin_straight,
            "wheelspin_inside_s": spin_inside,
            "wheelspin_outside_s": spin_outside,
            "brake_lock_events": lock_front_g["events"] + lock_rear_g["events"],
            "brake_lock_front_events": lock_front_g["events"],
            "brake_lock_front_s": lock_front_g["total_s"],
            "brake_lock_rear_events": lock_rear_g["events"],
            "brake_lock_rear_s": lock_rear_g["total_s"],
            "brake_lock_method": lock_method,
            "braking_time_s": round(braking_time_s, 1),
            "lock_pct_of_braking": lock_pct_of_braking,
            "near_lock_s": near_lock_s,
            "near_lock_pct_of_braking": near_lock_pct,
            "rear_temps_wire_identical": rear_temps_identical,
            "slide_power_on_s": slide_power_on_s,
            "slide_off_throttle_s": slide_off_throttle_s,
        },
        "observed_peaks": {
            "power_kw": peak_power_kw,
            "torque_nm": peak_torque,
            "samples": int(np.sum(sustained)),
            "coverage_s": round(float(np.sum(dt[sustained])), 1),
            "pct_at_peak_power": pct_at_peak_power,
            "note": "session observations during valid pulls (throttle ≥95%, "
                    "sustained), not the garage's rated figures",
        },
        "suspension": {
            w: {
                "avg": round(_wavg(s), 3),
                "max": round(float(np.max(s)) if s.size else 0.0, 3),
            }
            for w, s in zip(WHEELS, susp)
        },
        "suspension_bottom_out_events": bottom_out,
        "suspension_bottom_raw_crossings": bottom_raw_crossings,
        "suspension_bottom_longest_s": bottom_longest,
        "suspension_time_at_bottom_s": time_at_bottom,
        "suspension_travel_p99": travel_p99,
        "squat_rear_minus_front": squat_rear_minus_front,
        "dive_front_minus_rear": dive_front_minus_rear,
        "roll_front_p95": roll_front_p95,
        "roll_rear_p95": roll_rear_p95,
        "full_lock_pct_of_cornering": full_lock_pct,
        "gearing": {
            # Forza emits gear 0 for reverse and >=11 for neutral; only real
            # forward gears count as "top gear".
            "top_gear": int(np.max(gear[(gear >= 1) & (gear <= 10)]))
            if np.any((gear >= 1) & (gear <= 10)) else 0,
            "pct_on_limiter": round(_pct(on_limiter, dt), 1),
            "shift_rpm_avg": round(float(np.mean(shift_rpms)), 0) if shift_rpms.size else None,
            "shift_rpm_full_throttle": shift_rpm_full,
            "shift_rpm_spread": shift_rpm_spread,
            "shift_count": int(shift_rpms.size),
        },
        "max_lat_g": round(float(np.max(lat_g)) if lat_g.size else 0.0, 2),
        "lat_g_p99": lat_p99,
        "lat_g_sustained": lat_sustained,
        # Session-relative bounds: lets an analyst map this slice onto
        # section-sample timestamps and the raw CSV.
        "t_start": t_start,
        "t_end": t_end,
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
    has_runs = False
    lap_source = "wire" if has_laps else None
    event_time_s = None
    if has_laps:
        prepared = [(seg, _slice_stats(sd, seg["i0"], seg["i1"]), _lap_time(sd, seg))
                    for seg in segments]
        # A real game lap covers real distance. Rivals restarts leave
        # near-zero-distance staging/restart segments carrying a stale LastLap
        # (observed: a phantom "44.311" with 0 m behind it winning best_lap).
        # Circuit laps are all ~one length, so half the longest valid lap
        # cleanly separates the phantoms; reject them so they cannot pass as a
        # lap or set best_lap.
        good_dists = [s["distance_m"] for seg, s, t in prepared
                      if seg["complete"] and t and t >= MIN_LAP_S]
        min_lap_dist = (max(MIN_LAP_DIST_FLOOR_M,
                            MIN_LAP_DIST_FRAC * max(good_dists))
                        if good_dists else 0.0)
        for seg, stats, t in prepared:
            phantom = seg["complete"] and (
                t is None or t < MIN_LAP_S or stats["distance_m"] < min_lap_dist)
            if seg["lap"] is not None and phantom:
                continue  # restart phantom / glitch segment
            laps.append({
                "lap": seg["lap"],
                "complete": seg["complete"],
                "time_s": round(t, 3) if t else None,
                **stats,
            })
    else:
        # No lap markers — look for free-roam time-attack runs (staged
        # negative DistanceTraveled crossing 0 at launch).
        runs = detect_runs(sd)
        pos_laps = detect_position_laps(sd, runs)
        if pos_laps:
            # The run returned to its own start point repeatedly: a circuit.
            # These splits beat the run detector — position survives the
            # rewinds and mid-race snaps that break DistanceTraveled.
            has_laps = True
            lap_source = "position-gate"
            event_time_s = round(sum(r["time_s"] for r in runs), 3)
            for n, pl in enumerate(pos_laps, start=1):
                stats = _slice_stats(sd, pl["i0"], pl["i1"])
                laps.append({
                    "lap": n,
                    "complete": pl["complete"],
                    "time_s": pl["time_s"],
                    "route_m": pl["route_m"],
                    "rewind_affected": pl.get("rewind_affected", False),
                    **stats,
                })
        elif runs:
            has_runs = True
            for n, run in enumerate(runs, start=1):
                stats = _slice_stats(sd, run["i0"], run["i1"])
                laps.append({
                    "lap": None,
                    "run": n,
                    "complete": True,
                    "time_s": run["time_s"],
                    "route_m": run["route_m"],
                    **stats,
                })
        else:
            for seg in segments:
                stats = _slice_stats(sd, seg["i0"], seg["i1"])
                laps.append({
                    "lap": None,
                    "complete": False,
                    "time_s": None,
                    **stats,
                })

    session_stats = _slice_stats(sd, 0, sd.n)
    complete_times = [
        l["time_s"] for l in laps
        if l["complete"] and l["time_s"] and l.get("lap") is not None
        and not l.get("rewind_affected")
    ]
    if has_runs:
        complete_times = [l["time_s"] for l in laps if l.get("run") and l["time_s"]]
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
        "has_runs": has_runs,
        "lap_source": lap_source,
        "event_time_s": event_time_s,
        "laps": laps,
        "best_lap_s": round(min(complete_times), 3) if complete_times else None,
        "session": session_stats,
        "verdicts": verdicts,
    }


def compact_summary(rep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Boil a lap_report down to the handful of numbers a later session
    compares against (persisted per session; drives the tune-lineage table).
    Keep this small and stable — it is stored, not recomputed."""
    session = rep.get("session")
    if not session:
        return None
    trac = session.get("traction") or {}
    bal = (rep.get("verdicts") or {}).get("balance") or {}
    g = session.get("gearing") or {}
    lap_routes = [l.get("route_m") or l.get("distance_m")
                  for l in rep.get("laps", [])
                  if l.get("complete")
                  and (l.get("route_m") or l.get("distance_m"))]
    return {
        "best_s": rep.get("best_lap_s"),
        "timing": ("laps" if rep.get("has_laps")
                   else "runs" if rep.get("has_runs") else None),
        # Median timed-loop length: the route fingerprint that gates
        # cross-session performance deltas (never compare across routes).
        "lap_route_m": (round(float(np.median(lap_routes)), 1)
                        if lap_routes else None),
        "usi": bal.get("understeer_index"),
        "spin_total_s": trac.get("wheelspin_total_s"),
        "spin_multi_s": trac.get("wheelspin_multi_s"),
        "lock_s": round(float(trac.get("brake_lock_front_s") or 0)
                        + float(trac.get("brake_lock_rear_s") or 0), 1),
        "near_lock_pct": trac.get("near_lock_pct_of_braking"),
        "temp_f_c": session.get("temp_front_avg_c"),
        "temp_r_c": session.get("temp_rear_avg_c"),
        "max_kmh": (session.get("speed") or {}).get("max_kmh"),
        "shifts": g.get("shift_count"),
        "limiter_pct": g.get("pct_on_limiter"),
        "lat_g": session.get("lat_g_sustained"),
        "duration_s": session.get("duration_s"),
        "distance_km": round((session.get("distance_m") or 0) / 1000.0, 2),
    }
