"""Corner-section evidence: classify driving into hairpins, turns,
sweepers, transfers and straights, with per-category aggregates and
representative samples (best / median / worst by a documented metric).

This layer reports what happened, where and how often — it never issues
tuning verdicts. Session-wide averages hide how differently a car behaves
in a hairpin versus a fast sweeper versus a chicane flick; these buckets
are what an analyst (human or AI) actually reasons about.

Detection channels: signed lateral acceleration (sled, local X), Yaw
(wire heading), Speed, Steer, throttle/brake, per-wheel slip and
suspension travel. All thresholds are documented constants — argue in
this file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from .session_data import SessionData

CORNER_LAT_G = 0.30          # cornering begins (matches laps.py)
CORNER_MIN_S = 0.4
CORNER_GAP_S = 0.5           # merge cornering blips closer than this
SMOOTH_S = 0.25              # lateral-g smoothing window
TRANSFER_LINK_S = 1.2        # opposite corners linked within this = transfer
TRANSFER_MAX_REVERSAL_S = 3.0  # peak-to-peak flick time; slower = two corners
TRANSFER_MIN_AVG_KMH = 60.0  # slower "flicks" are staging/recovery noise
TRANSFER_MIN_KMH = 40.0      # slowest moment allowed inside a transfer
SECTION_MAX_LAT_G = 3.0      # p95 above this = impact/landing contamination
SLIP_CLIP = 2.5              # combined slip beyond this is impact noise
LAUNCH_MAX_START_KMH = 30.0  # a "straight" starting below this is a launch
HAIRPIN_MAX_MIN_KMH = 60.0   # a hairpin bottoms out below this
HAIRPIN_MIN_HEADING_DEG = 100.0
SWITCHBACK_MIN_HEADING_DEG = 150.0  # ≥ this counts as hairpin up to 90 km/h
SWITCHBACK_MAX_MIN_KMH = 90.0
SWEEPER_MIN_AVG_KMH = 120.0
SWEEPER_MIN_KMH = 90.0       # a sweeper never drops below this
SWEEPER_MIN_S = 2.5
TURN_MIN_HEADING_DEG = 25.0  # less net heading change than this = a kink
STRAIGHT_MIN_M = 200.0
WHEELS = ("FL", "FR", "RL", "RR")

# The metric each category's samples are ordered by. Purely factual —
# samples are labelled lowest/median/highest on this metric; whether low
# or high was GOOD is the analyst's call (a negative slip delta can be
# useful rotation or excess oversteer; the ranking never decides).
RANK_METRIC = {
    "hairpin": ("wheelspin_s", "driven-wheel wheelspin inside the corner"),
    "turn": ("slip_delta", "mean front-minus-rear combined slip"),
    "sweeper": ("slip_delta", "mean front-minus-rear combined slip"),
    "transfer": ("susp_travel_max", "peak suspension travel across the flick"),
    "straight": ("wheelspin_s", "driven-wheel wheelspin"),
    "launch": ("wheelspin_s", "driven-wheel wheelspin"),
}


def _fmt_t(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m:02d}:{seconds - m * 60:05.2f}"


def _spans(mask: np.ndarray) -> List[tuple]:
    if mask.size == 0:
        return []
    diff = np.diff(mask.astype(np.int8))
    starts = list(np.where(diff == 1)[0] + 1)
    ends = list(np.where(diff == -1)[0] + 1)
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(mask.size)
    return list(zip(starts, ends))


def _smooth(x: np.ndarray, frames: int) -> np.ndarray:
    if frames <= 1 or x.size < frames:
        return x
    kernel = np.ones(frames) / frames
    return np.convolve(x, kernel, mode="same")


def detect_sections(sd: SessionData,
                    timed_windows: Optional[List[tuple]] = None
                    ) -> Optional[Dict[str, Any]]:
    """Classify the whole session. Returns None when there is no steering
    data worth splitting (parked sessions, tiny captures).

    ``timed_windows`` — session-relative (start, end) spans of timed
    laps/runs. Every instance is labelled ``timed`` true/false, and
    representative samples are chosen from timed instances when any exist:
    an 18 km/h staging shuffle must never be a category's "lowest" sample.
    """
    n = sd.n
    if n < 300:
        return None
    t = sd.col("t_mono")
    t0 = float(t[0])
    dt = sd.dt()
    hz = 1.0 / max(float(np.median(dt)), 1e-3)
    speed = sd.col("Speed")
    kmh = speed * 3.6
    lat = _smooth(sd.col("AccelerationX") / 9.81, int(SMOOTH_S * hz))
    yaw = np.unwrap(sd.col("Yaw"))
    accel = sd.col("Accel") / 255.0
    brake = sd.col("Brake") / 255.0
    route = np.cumsum(speed * dt)

    # Combined slip spikes past 15 on kerb strikes and collisions — clip
    # before any section statistic so one impact frame cannot poison an
    # evidence table (an unclipped +18.84 "slip delta" once did).
    slips = [np.minimum(sd.col(f"TireCombinedSlip{w}"), SLIP_CLIP) for w in
             ("FrontLeft", "FrontRight", "RearLeft", "RearRight")]
    slip_f = np.maximum(slips[0], slips[1])
    slip_r = np.maximum(slips[2], slips[3])
    susp = [sd.col(f"NormalizedSuspensionTravel{w}") for w in
            ("FrontLeft", "FrontRight", "RearLeft", "RearRight")]
    susp_max_all = np.maximum.reduce(susp)

    from .laps import canonical_drivetrain
    dtt = canonical_drivetrain(sd)
    driven = {0: (0, 1), 1: (2, 3), 2: (0, 1, 2, 3)}.get(dtt, (2, 3))
    slip_ratio = [sd.col(f"TireSlipRatio{w}") for w in
                  ("FrontLeft", "FrontRight", "RearLeft", "RearRight")]
    spin_mask = np.zeros(n, dtype=bool)
    for i in driven:
        spin_mask |= slip_ratio[i] > 0.5
    spin_mask &= (accel > 0.4) & (speed > 3.0)

    cornering = (np.abs(lat) > CORNER_LAT_G) & (speed > 5.0)

    # Merge nearby cornering spans — but only same-direction ones: a
    # chicane's opposite lobes must stay separate for transfer pairing.
    raw = _spans(cornering)
    merged: List[List[int]] = []
    for s, e in raw:
        d = 1.0 if float(np.mean(lat[s:e])) > 0 else -1.0
        if merged and (t[s] - t[merged[-1][1] - 1]) < CORNER_GAP_S \
                and merged[-1][2] == d:
            merged[-1][1] = e
        else:
            merged.append([s, e, d])
    events = [(s, e) for s, e, _ in merged if t[e - 1] - t[s] >= CORNER_MIN_S]

    def _event(s: int, e: int) -> Dict[str, Any]:
        sl = slice(s, e)
        min_i = s + int(np.argmin(speed[sl]))
        heading = abs(float(np.degrees(yaw[e - 1] - yaw[s])))
        # Throttle semantics (self-consistent): "never_lifted" ONLY when
        # throttle stayed ≥50% for the whole section; otherwise the time
        # from the deepest lift to the first ≥50% reapplication.
        if float(np.min(accel[sl])) >= 0.5:
            reapply = "never_lifted"
        else:
            lift_i = s + int(np.argmin(accel[sl]))
            after = np.where(accel[lift_i:e] >= 0.5)[0]
            reapply = (round(float(t[lift_i + after[0]] - t[lift_i]), 2)
                       if after.size else "not_reached")
        return {
            "t_start": round(float(t[s] - t0), 2),
            "start": _fmt_t(float(t[s] - t0)),
            "duration_s": round(float(t[e - 1] - t[s]), 2),
            "direction": "right" if float(np.mean(lat[sl])) > 0 else "left",
            "heading_deg": round(heading, 0),
            "entry_kmh": round(float(kmh[s]), 0),
            "min_kmh": round(float(kmh[min_i]), 0),
            "exit_kmh": round(float(kmh[e - 1]), 0),
            "lat_g_p95": round(float(np.percentile(np.abs(lat[sl]), 95)), 2),
            "slip_f": round(float(np.mean(slip_f[sl])), 2),
            "slip_r": round(float(np.mean(slip_r[sl])), 2),
            "slip_delta": round(float(np.mean(slip_f[sl] - slip_r[sl])), 2),
            "braking_s": round(float(np.sum(dt[sl][brake[sl] > 0.1])), 2),
            "throttle_at_entry_pct": round(float(accel[s]) * 100, 0),
            "throttle_min_pct": round(float(np.min(accel[sl])) * 100, 0),
            "throttle_reapply_s": reapply,
            "wheelspin_s": round(float(np.sum(dt[sl][spin_mask[sl]])), 2),
            "susp_travel_max": round(float(np.max(susp_max_all[sl])), 2),
            "_s": s, "_e": e,
        }

    evs = [_event(s, e) for s, e in events]

    # Transfers: adjacent opposite-direction corners linked tightly.
    transfers: List[Dict[str, Any]] = []
    in_transfer = set()
    for k in range(len(evs) - 1):
        a, b = evs[k], evs[k + 1]
        gap = float(t[b["_s"]] - t[a["_e"] - 1])
        if a["direction"] != b["direction"] and gap < TRANSFER_LINK_S:
            s, e = a["_s"], b["_e"]
            sl = slice(s, e)
            pa = a["_s"] + int(np.argmax(np.abs(lat[a["_s"]:a["_e"]])))
            pb = b["_s"] + int(np.argmax(np.abs(lat[b["_s"]:b["_e"]])))
            if float(t[pb] - t[pa]) > TRANSFER_MAX_REVERSAL_S:
                continue  # two linked corners, not a flick — classify each
            if float(np.mean(kmh[sl])) < TRANSFER_MIN_AVG_KMH:
                continue  # staging/recovery/spin noise, not a chassis flick
            if float(np.min(kmh[sl])) < TRANSFER_MIN_KMH:
                continue  # near-stop inside the pair — spin/recovery
            if float(np.percentile(np.abs(lat[sl]), 95)) > SECTION_MAX_LAT_G:
                continue  # impact/landing contamination
            transfers.append({
                "t_start": round(float(t[s] - t0), 2),
                "start": _fmt_t(float(t[s] - t0)),
                "duration_s": round(float(t[e - 1] - t[s]), 2),
                "speed_kmh": f"{np.min(kmh[sl]):.0f}–{np.max(kmh[sl]):.0f}",
                "reversal_s": round(float(t[pb] - t[pa]), 2),
                "slip_delta_first": a["slip_delta"],
                "slip_delta_second": b["slip_delta"],
                "susp_travel_max": round(float(np.max(susp_max_all[sl])), 2),
                "throttle_min": round(float(np.min(accel[sl])), 2),
                "lat_g_p95": round(float(np.percentile(np.abs(lat[sl]), 95)), 2),
            })
            in_transfer.update((k, k + 1))

    hairpins, sweepers, turns = [], [], []
    for k, ev in enumerate(evs):
        if k in in_transfer:
            continue
        if ev["heading_deg"] < TURN_MIN_HEADING_DEG:
            continue  # a kink, not a corner
        if ev["lat_g_p95"] > SECTION_MAX_LAT_G:
            continue  # impact/landing contamination, not cornering
        avg_kmh = (ev["entry_kmh"] + ev["exit_kmh"] + ev["min_kmh"]) / 3.0
        if ((ev["min_kmh"] < HAIRPIN_MAX_MIN_KMH
                and ev["heading_deg"] >= HAIRPIN_MIN_HEADING_DEG)
                or (ev["heading_deg"] >= SWITCHBACK_MIN_HEADING_DEG
                    and ev["min_kmh"] < SWITCHBACK_MAX_MIN_KMH)):
            hairpins.append(ev)
        elif (ev["duration_s"] >= SWEEPER_MIN_S
                and avg_kmh >= SWEEPER_MIN_AVG_KMH
                and ev["min_kmh"] >= SWEEPER_MIN_KMH):
            sweepers.append(ev)
        else:
            turns.append(ev)

    # Straights: gaps between CLASSIFIED corners. Kinks (< the turn
    # threshold) live inside straights — a flat-out curve is a straight.
    # A "straight" that begins near-stationary is a LAUNCH: standing-start
    # acceleration is not comparable with a flying straight's gearing.
    straights: List[Dict[str, Any]] = []
    launches: List[Dict[str, Any]] = []
    classified = sorted(
        [(ev["_s"], ev["_e"]) for ev in hairpins + sweepers + turns]
        + [(evs[k]["_s"], evs[k]["_e"]) for k in in_transfer])
    bounds = [(0, 0)] + classified + [(n, n)]
    gear = sd.col("Gear")
    for (pa, pe), (nb, _) in zip(bounds[:-1], bounds[1:]):
        s, e = pe, nb
        if e - s < 10:
            continue
        length = float(route[e - 1] - route[s])
        if length < STRAIGHT_MIN_M or float(np.mean(speed[s:e])) < 5.0:
            continue
        sl = slice(s, e)
        g_used = sorted({int(g) for g in gear[sl] if 0 < g < 11})
        (launches if float(kmh[s]) < LAUNCH_MAX_START_KMH
         else straights).append({
            "t_start": round(float(t[s] - t0), 2),
            "start": _fmt_t(float(t[s] - t0)),
            "length_m": round(length, 0),
            "speed_kmh": f"{kmh[s]:.0f}→{np.max(kmh[sl]):.0f}",
            "full_throttle_pct": round(
                float(np.sum(dt[sl][accel[sl] >= 0.95]))
                / max(float(np.sum(dt[sl])), 1e-6) * 100.0, 0),
            "gears": g_used,
            "shifts": int(np.sum(np.diff(gear[sl]) > 0)),
            "wheelspin_s": round(float(np.sum(dt[sl][spin_mask[sl]])), 2),
            "susp_travel_max": round(float(np.max(susp_max_all[sl])), 2),
        })

    def _in_timed(t_start: float) -> Optional[bool]:
        if not timed_windows:
            return None
        return any(a - 1.0 <= t_start <= b + 1.0 for a, b in timed_windows)

    def _bucket(instances: List[Dict[str, Any]], cat: str) -> Dict[str, Any]:
        key, key_doc = RANK_METRIC[cat]
        clean = [dict((k, v) for k, v in i.items() if not k.startswith("_"))
                 for i in instances]
        for i in clean:
            timed = _in_timed(i.get("t_start", 0.0))
            if timed is not None:
                i["timed"] = timed
        out: Dict[str, Any] = {"count": len(clean), "ranked_by": key_doc}
        if timed_windows:
            timed_only = [i for i in clean if i.get("timed")]
            if timed_only and len(timed_only) < len(clean):
                out["outside_timed"] = len(clean) - len(timed_only)
                clean = timed_only  # samples/medians from timed driving only
        if not clean:
            return out
        if len(clean) == 1:
            out["only"] = clean[0]
            return out
        vals = [(i.get(key) if isinstance(i.get(key), (int, float)) else 0.0, i)
                for i in clean]
        vals.sort(key=lambda x: x[0])
        if len(vals) == 2:
            # No invented "median" member from an even pair.
            out["lower"] = vals[0][1]
            out["higher"] = vals[1][1]
        else:
            out["lowest"] = vals[0][1]
            out["median"] = vals[len(vals) // 2][1]
            out["highest"] = vals[-1][1]
        numeric: Dict[str, List[float]] = {}
        for i in clean:
            for k, v in i.items():
                if isinstance(v, (int, float)):
                    numeric.setdefault(k, []).append(float(v))
        out["median_metrics"] = {
            k: round(float(np.median(v)), 2) for k, v in numeric.items()
            if k not in ("t_start",)
        }
        return out

    return {
        "hairpin": _bucket(hairpins, "hairpin"),
        "turn": _bucket(turns, "turn"),
        "sweeper": _bucket(sweepers, "sweeper"),
        "transfer": _bucket(transfers, "transfer"),
        "straight": _bucket(straights, "straight"),
        "launch": _bucket(launches, "launch"),
        "classification": "mutually exclusive — a transfer's two component "
                          "corners are counted only under transfer; an "
                          "event spans contiguous same-direction cornering "
                          "and may cover linked bends",
        "thresholds": {
            "cornering_lat_g": CORNER_LAT_G,
            "slip_clip": SLIP_CLIP,
            "hairpin": f"min speed < {HAIRPIN_MAX_MIN_KMH:.0f} km/h with "
                       f"heading ≥ {HAIRPIN_MIN_HEADING_DEG:.0f}°, or "
                       f"heading ≥ {SWITCHBACK_MIN_HEADING_DEG:.0f}° below "
                       f"{SWITCHBACK_MAX_MIN_KMH:.0f} km/h",
            "sweeper": f"≥ {SWEEPER_MIN_S}s at avg ≥ {SWEEPER_MIN_AVG_KMH:.0f} km/h",
            "transfer": f"opposite-direction corners linked within "
                        f"{TRANSFER_LINK_S}s, reversal < "
                        f"{TRANSFER_MAX_REVERSAL_S}s, avg ≥ "
                        f"{TRANSFER_MIN_AVG_KMH:.0f} km/h, never below "
                        f"{TRANSFER_MIN_KMH:.0f} km/h",
            "straight": f"non-cornering ≥ {STRAIGHT_MIN_M:.0f} m, flying "
                        f"start (≥ {LAUNCH_MAX_START_KMH:.0f} km/h)",
            "launch": f"non-cornering ≥ {STRAIGHT_MIN_M:.0f} m from a "
                      f"standing/near-standing start",
            "lat_g_p95": f"per-section lateral G is the 95th percentile "
                         f"inside the section (single-frame spikes "
                         f"excluded); sections with p95 > "
                         f"{SECTION_MAX_LAT_G:.0f} g are dropped as "
                         f"impact-contaminated",
        },
    }
