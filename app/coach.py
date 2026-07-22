"""Local, deterministic post-race driving coach.

Turns the metrics lap_report()/detect_sections() already compute into a
plain-language read: a race summary, the driver habits to fix, and the car
problems no driving fixes — each tagged you (driver) or car. No AI, no
network. All prose lives here so it is unit-tested."""
from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Optional

# --- thresholds (initial values; calibrated against real captures — see
#     docs/specs/2026-07-22-driving-coach-design.md) ---
CONSISTENCY_TIGHT = 0.010    # lap spread < 1% of best → tight
CONSISTENCY_LOOSE = 0.030    # > 3% → loose
CLEAN_LAP_MULT = 1.15        # a "clean" lap is within 15% of your best; the
#                              rest (spins, parked/paused laps) are excluded
MIN_TIMED_LAPS = 3           # fewer → no consistency/trend read
MIN_CORNERS = 4              # fewer cornering events → not enough to coach
CORNERING_TIME_FLOOR = 20.0  # ...or this many seconds of cornering

UNDERSTEER_STRONG = 0.35     # |index| that reads as a real balance bias
OSCILLATION_PER_MIN = 14.0   # limiting axle flips → nervous car

LOCKUP_PCT = 20.0            # near-lock % of braking → over-braking
FULL_LOCK_PCT = 40.0         # % of cornering at full lock → over-driving / car won't turn
LATE_THROTTLE_S = 1.3        # median reapply delay after apex
POWER_DOWN_RATIO = 1.6       # power-on slide ≫ off-throttle → traction-limited
POWER_DOWN_FLOOR = 8.0       # ...and this many seconds, so noise doesn't trip it
CORNER_CATS = ("hairpin", "turn", "sweeper")

TAG_YOU = "you"
TAG_CAR = "car"
TAG_BOTH = "both"


def _fmt(seconds: Optional[float]) -> str:
    if not seconds:
        return "—"
    m = int(seconds // 60)
    return f"{m}:{seconds - 60 * m:06.3f}"


def _flag(tag: str, severity: float, title: str, detail: str,
          metric: str, value: Any) -> Dict[str, Any]:
    return {"tag": tag, "severity": round(float(severity), 3),
            "title": title, "detail": detail, "metric": metric,
            "value": value}


def _race_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    laps = [l for l in (report.get("laps") or [])
            if l.get("complete") and l.get("time_s")]
    best = report.get("best_lap_s")
    if report.get("has_laps") and best and laps:
        raw = [float(l["time_s"]) for l in laps]           # chronological
        keep = [t for t in raw if t <= best * CLEAN_LAP_MULT]
        excluded = len(raw) - len(keep)
        excl = (f" ({excluded} scruffy lap{'s' if excluded != 1 else ''} "
                "excluded)") if excluded else ""
        if len(keep) >= MIN_TIMED_LAPS:
            spread = (max(keep) - min(keep)) / best
            consistency = ("tight" if spread < CONSISTENCY_TIGHT
                           else "loose" if spread > CONSISTENCY_LOOSE
                           else "workable")
            k = max(1, len(keep) // 3)
            early, late = median(keep[:k]), median(keep[-k:])
            margin = best * 0.005
            trend = ("improving" if late < early - margin
                     else "fading" if late > early + margin else "steady")
            pace = {"tight": "laps close together",
                    "workable": "some scatter lap-to-lap",
                    "loose": f"a {spread * 100:.1f}% spread"}[consistency]
            line = (f"{len(keep)} clean laps, best {_fmt(best)}{excl} — "
                    f"{pace}; pace {trend}.")
            return {"line": line, "laps": len(keep), "best_lap_s": best,
                    "consistency": consistency, "trend": trend}
        return {"line": (f"Best clean lap {_fmt(best)}{excl} — too few clean "
                         "laps to read consistency yet."),
                "laps": len(keep), "best_lap_s": best,
                "consistency": None, "trend": None}
    return {"line": ("Free roam — no timed laps to compare. "
                     "Coaching from your inputs."),
            "laps": len(laps), "best_lap_s": best,
            "consistency": None, "trend": None}


def _car_flags(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    bal = session.get("balance") or {}
    flags: List[Dict[str, Any]] = []

    both_sat = bal.get("both_axles_saturated")
    if both_sat:
        flags.append(_flag(
            TAG_CAR, 1.0, "You're at the grip ceiling",
            "Both axles are past the grip limit — that's the build (tyres, "
            "then power), not the tune and not your driving. A tune can only "
            "calm it, it can't add grip.",
            "both_axles_saturated", True))

    osc = bal.get("reversal_rate_per_min")
    if osc is not None and osc >= OSCILLATION_PER_MIN:
        flags.append(_flag(
            TAG_CAR, min(1.0, osc / 20.0), "The car's nervous",
            f"The limiting axle is flipping front-to-rear {osc:g}x a minute — "
            "the car's unstable underneath you. Calm the balance in the tune; "
            "it isn't something you can drive around.",
            "reversal_rate_per_min", osc))

    usi = bal.get("understeer_index")
    if not both_sat and usi is not None and abs(usi) >= UNDERSTEER_STRONG:
        phases = bal.get("phases") or {}
        phase_usis = [p.get("usi") for p in phases.values()
                      if p.get("usi") is not None]
        if phase_usis and all((u > 0) == (usi > 0) for u in phase_usis):
            word = ("pushes wide (understeer)" if usi > 0
                    else "steps out (oversteer)")
            flags.append(_flag(
                TAG_CAR, min(1.0, abs(usi) / 0.6), f"The car {word} everywhere",
                f"The balance sits at {usi:+.2f} across entry, mid and exit — "
                "a bias no line fixes. That's a tune (shift the balance), not "
                "a you problem.",
                "understeer_index", usi))
    return flags


def _section_median(sections: Optional[Dict[str, Any]], key: str,
                    cats) -> Optional[float]:
    if not sections:
        return None
    vals = []
    for c in cats:
        mm = (sections.get(c) or {}).get("median_metrics") or {}
        if mm.get(key) is not None:
            vals.append(float(mm[key]))
    return round(median(vals), 2) if vals else None


def _driver_flags(session: Dict[str, Any],
                  sections: Optional[Dict[str, Any]]):
    bal = session.get("balance") or {}
    trac = session.get("traction") or {}
    usi = bal.get("understeer_index")
    flags: List[Dict[str, Any]] = []

    # Is the car traction-limited on power? Gates the late-throttle triage.
    on = trac.get("slide_power_on_s")
    off = trac.get("slide_off_throttle_s")
    power_limited = bool(on and off is not None and on > POWER_DOWN_FLOOR
                         and on >= POWER_DOWN_RATIO * max(off, 0.1))

    lock = trac.get("near_lock_pct_of_braking")
    if lock is not None and lock >= LOCKUP_PCT:
        flags.append(_flag(
            TAG_YOU, min(1.0, lock / 40.0), "You're locking the brakes",
            f"{lock:g}% of your braking sits right on the lock threshold — "
            "brake a touch earlier and lighter, then trail off the pedal as "
            "you turn in.",
            "near_lock_pct_of_braking", lock))

    full = session.get("full_lock_pct_of_cornering")
    if full is not None and full >= FULL_LOCK_PCT:
        if usi is not None and usi >= UNDERSTEER_STRONG:
            flags.append(_flag(
                TAG_BOTH, min(1.0, full / 70.0), "Full lock — but that's the car",
                f"You're at full steering lock for {full:g}% of the corners, "
                f"but the balance ({usi:+.2f}) says the car won't rotate — "
                "you're holding lock because it won't turn. Fix the car's "
                "understeer first, then see if it's still you.",
                "full_lock_pct_of_cornering", full))
        else:
            flags.append(_flag(
                TAG_YOU, min(1.0, full / 70.0), "You're sawing at the wheel",
                f"Full steering lock for {full:g}% of the corners with a "
                "balanced car — you're over-driving it. Smoother, earlier "
                "inputs and let the car take a set.",
                "full_lock_pct_of_cornering", full))

    reapply = _section_median(sections, "throttle_reapply_s", CORNER_CATS)
    if reapply is not None and reapply >= LATE_THROTTLE_S:
        if power_limited:
            flags.append(_flag(
                TAG_CAR, min(1.0, reapply / 3.0),
                "Late to power — because the car won't take it",
                f"You're back on throttle {reapply:g}s after the apex, but the "
                "car is sliding on power (it can't put it down). That's a "
                "diff/gearing job — the hesitation is the car, not you.",
                "throttle_reapply_s", reapply))
        else:
            flags.append(_flag(
                TAG_YOU, min(1.0, reapply / 3.0), "You're late to the throttle",
                f"Back on power ~{reapply:g}s after the apex on a car that'll "
                "take it — pick the throttle up sooner and feed it in.",
                "throttle_reapply_s", reapply))

    # (An "over-slowing entries" driver flag was considered but cut: from
    # aggregate section data, exit − min corner speed is naturally large on
    # every corner, so there is no honest threshold without a per-corner
    # reference speed. Deferred to v2.)

    # Power-down is a car problem in its own right. If the late-throttle
    # symptom already surfaced it (triaged to the car), don't repeat it;
    # otherwise flag it standalone so it's never silently swallowed.
    if power_limited and not any(f["metric"] == "throttle_reapply_s"
                                 for f in flags):
        flags.append(_flag(
            TAG_CAR, min(1.0, on / 60.0), "The car won't put the power down",
            f"On-throttle it slides {on:g}s versus {off:g}s off the throttle — "
            "it can't deploy what it's got. That's diff / gearing / traction "
            "(a tune or build job), not your right foot.",
            "slide_power_on_s", on))

    return flags, power_limited


def _corner_count(sections: Optional[Dict[str, Any]]) -> int:
    if not sections:
        return 0
    return sum(int((sections.get(c) or {}).get("count", 0))
               for c in CORNER_CATS)


def coach_report(report: Dict[str, Any],
                 sections: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Structured, plain-language coach read over an existing analysis.

    ``report`` is a lap_report() output; ``sections`` a detect_sections()
    output (or None). Returns {data_sufficient, headline, summary, flags}."""
    summary = _race_summary(report)
    session = report.get("session") or {}
    phases = (session.get("balance") or {}).get("phases") or {}
    cornering_time = sum(float((phases.get(p) or {}).get("time_s", 0) or 0)
                         for p in ("entry", "mid", "exit", "lift"))
    sufficient = (_corner_count(sections) >= MIN_CORNERS
                  or cornering_time >= CORNERING_TIME_FLOOR)

    if not sufficient:
        return {"data_sufficient": False,
                "headline": "Not enough clean running to coach yet — "
                            "do a few more laps.",
                "summary": summary, "flags": []}

    driver, _ = _driver_flags(session, sections)
    flags = _car_flags(session) + driver
    flags.sort(key=lambda f: f["severity"], reverse=True)

    if flags:
        headline = flags[0]["title"]
    else:
        headline = ("Nothing's jumping out — tidy driving and the car's "
                    f"behaving. Best lap {_fmt(summary.get('best_lap_s'))}.")
    return {"data_sufficient": True, "headline": headline,
            "summary": summary, "flags": flags}


def coach_markdown(report: Dict[str, Any],
                   sections: Optional[Dict[str, Any]] = None) -> str:
    """Render the coach read as a Markdown block to lead an evidence export:
    the deterministic verdict before the wall of numbers. Non-prescriptive —
    it reports what the telemetry shows, it does not recommend a tune."""
    out = coach_report(report, sections)
    tag = {TAG_YOU: "🧍 you", TAG_CAR: "🔧 car", TAG_BOTH: "🧍🔧 you + car"}
    lines = ["## The read (deterministic)", "",
             f"**{out['headline']}**", ""]
    flags = out.get("flags") or []
    if out.get("data_sufficient") and flags:
        for f in flags:
            lines.append(f"- **{tag.get(f['tag'], f['tag'])}** — "
                         f"{f['title']}: {f['detail']}")
        lines.append("")
        lines.append("*A computed read from the telemetry, not a tune "
                     "recommendation. A 🧍 finding can be driver variance "
                     "rather than the setup — weigh it against repeatable laps "
                     "before tuning around it.*")
    else:
        lines.append(f"*{(out.get('summary') or {}).get('line', '')}*")
    return "\n".join(lines)
