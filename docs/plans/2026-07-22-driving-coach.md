# Driving Coach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local, deterministic post-race "coach" that summarises the run, names the driver habits to fix, and flags when the car needs work — each tagged 🧍 you or 🔧 car.

**Architecture:** One pure function `coach_report(report, sections)` in a new `app/coach.py`, fed the dicts `lap_report()` and `detect_sections()` already produce. A thin `GET /api/sessions/{id}/coach` endpoint wires it to a session. The frontend renders the read as a card at the top of Analysis and on a new `/coach` page. All prose lives in Python and is unit-tested; the frontend only renders.

**Tech Stack:** Python 3.12, FastAPI, numpy (already used by the producers); vanilla-JS PWA frontend; pytest.

## Global Constraints

- **No AI, no network.** The coach composes its own words from local telemetry. The only network call the tool ever makes is the update check — do not add another. Copied verbatim from the spec: "No AI call. No network."
- **`app/coach.py` depends only on plain dicts** (the `lap_report()` / `detect_sections()` outputs) — never on `SessionData` or the DB. This keeps it unit-testable with hand-built dicts.
- **Every claim carries its real number.** Never fabricate a value the telemetry doesn't contain; ranking is a severity heuristic, not a seconds-lost claim.
- **Reads are defensive.** Every metric access uses `.get(...)` chains and skips its flag when the input is missing (sessions can be free-roam, short, or partial).
- Tone: blunt, specific, constructive, one clear priority. No "great job!" filler.

---

## Data contract (read once before starting)

`coach_report(report, sections=None)` consumes:

**`report`** = `app.laps.lap_report(sd)`:
- `report["has_laps"]` (bool), `report["best_lap_s"]` (float|None)
- `report["laps"]` = list of `{"time_s": float, "complete": bool, ...}`
- `report["session"]` = `session_stats`:
  - `["balance"]`: `understeer_index` (float, +understeer/−oversteer), `both_axles_saturated` (bool), `reversal_rate_per_min` (float|None), `phases` = `{"entry"|"mid"|"exit"|"lift": {"usi": float|None, "time_s": float}}`
  - `["traction"]`: `near_lock_pct_of_braking` (float 0–100|None), `slide_power_on_s` (float), `slide_off_throttle_s` (float)
  - `["full_lock_pct_of_cornering"]` (float 0–100|None)

**`sections`** = `app.sections.detect_sections(sd)` (may be `None`):
- keyed by category `"hairpin"|"turn"|"sweeper"|"transfer"|"straight"|"launch"`
- each value is `{"count": int, "median_metrics": {"throttle_reapply_s": float, "min_kmh": float, "exit_kmh": float, "braking_s": float, ...}, ...}`

Output:
```python
{
  "data_sufficient": bool,
  "headline": str,               # the single most important thing
  "summary": {"line": str, "laps": int, "best_lap_s": float|None,
              "consistency": str|None, "trend": str|None},
  "flags": [                     # sorted by severity, highest first
    {"tag": "you"|"car"|"both", "severity": float,
     "title": str, "detail": str, "metric": str, "value": Any},
    ...
  ],
}
```

---

### Task 1: `coach.py` — module, helpers, and race summary

**Files:**
- Create: `app/coach.py`
- Test: `tests/test_coach.py`

**Interfaces:**
- Produces: `coach_report(report: dict, sections: dict | None = None) -> dict` (summary-only for now); `_fmt(seconds) -> str`; `_race_summary(report: dict) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coach.py
from __future__ import annotations
from app.coach import coach_report, _fmt


def _laps(times):
    return [{"time_s": t, "complete": True} for t in times]


def test_fmt_matches_app_lap_format():
    assert _fmt(53.55) == "0:53.550"
    assert _fmt(None) == "—"


def test_summary_tight_and_improving():
    report = {"has_laps": True, "best_lap_s": 53.55,
              "laps": _laps([54.9, 54.6, 53.9, 53.7, 53.55]),
              "session": {"balance": {}, "traction": {}}}
    out = coach_report(report)
    s = out["summary"]
    assert s["laps"] == 5 and s["best_lap_s"] == 53.55
    assert s["consistency"] == "tight" or s["consistency"] == "workable"
    assert s["trend"] == "improving"
    assert "0:53.550" in s["line"]


def test_summary_loose_spread():
    report = {"has_laps": True, "best_lap_s": 50.0,
              "laps": _laps([50.0, 52.0, 51.5, 53.0]),
              "session": {"balance": {}, "traction": {}}}
    assert coach_report(report)["summary"]["consistency"] == "loose"


def test_summary_free_roam_has_no_lap_read():
    report = {"has_laps": False, "best_lap_s": None, "laps": [],
              "session": {"balance": {}, "traction": {}}}
    s = coach_report(report)["summary"]
    assert s["consistency"] is None
    assert "free roam" in s["line"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.coach'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/coach.py
"""Local, deterministic post-race driving coach.

Turns the metrics lap_report()/detect_sections() already compute into a
plain-language read: a race summary, the driver habits to fix, and the car
problems no driving fixes — each tagged you (driver) or car. No AI, no
network. All prose lives here so it is unit-tested."""
from __future__ import annotations

from statistics import median
from typing import Any, Dict, List, Optional

# --- thresholds (initial values; calibrated against real captures in the
#     final task — see docs/specs/2026-07-22-driving-coach-design.md) ---
CONSISTENCY_TIGHT = 0.010   # lap spread < 1% of best → tight
CONSISTENCY_LOOSE = 0.030   # > 3% → loose
MIN_TIMED_LAPS = 3          # fewer → no consistency/trend read
MIN_CORNERS = 4             # fewer cornering events → not enough to coach
CORNERING_TIME_FLOOR = 20.0  # or this many seconds of cornering


def _fmt(seconds: Optional[float]) -> str:
    if not seconds:
        return "—"
    m = int(seconds // 60)
    return f"{m}:{seconds - 60 * m:06.3f}"


def _race_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    laps = [l for l in (report.get("laps") or [])
            if l.get("complete") and l.get("time_s")]
    best = report.get("best_lap_s")
    if report.get("has_laps") and best and len(laps) >= MIN_TIMED_LAPS:
        times = [float(l["time_s"]) for l in laps]
        spread = (max(times) - min(times)) / best
        consistency = ("tight" if spread < CONSISTENCY_TIGHT
                       else "loose" if spread > CONSISTENCY_LOOSE
                       else "workable")
        k = max(1, len(times) // 3)
        early, late = median(times[:k]), median(times[-k:])
        margin = best * 0.005
        trend = ("improving" if late < early - margin
                 else "fading" if late > early + margin else "steady")
        pace = {"tight": "laps close together",
                "workable": "some scatter lap-to-lap",
                "loose": f"a {spread * 100:.1f}% spread"}[consistency]
        line = (f"{len(laps)} laps, best {_fmt(best)} — {pace}; pace {trend}.")
        return {"line": line, "laps": len(laps), "best_lap_s": best,
                "consistency": consistency, "trend": trend}
    return {"line": ("Free roam — no timed laps to compare. "
                     "Coaching from your inputs."),
            "laps": len(laps), "best_lap_s": best,
            "consistency": None, "trend": None}


def coach_report(report: Dict[str, Any],
                 sections: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    summary = _race_summary(report)
    return {"data_sufficient": True, "headline": summary["line"],
            "summary": summary, "flags": []}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/coach.py tests/test_coach.py
git commit -m "feat(coach): module scaffold + race summary block"
```

---

### Task 2: `coach.py` — car flags

**Files:**
- Modify: `app/coach.py`
- Test: `tests/test_coach.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_flag(tag, severity, title, detail, metric, value) -> dict`; `_car_flags(session: dict) -> List[dict]`. Tags used: `TAG_CAR = "car"`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_coach.py
from app.coach import _car_flags


def test_both_axles_saturated_is_a_build_ceiling_car_flag():
    flags = _car_flags({"balance": {"both_axles_saturated": True,
                                    "understeer_index": 0.6, "phases": {}},
                        "traction": {}})
    tags = {f["tag"] for f in flags}
    assert "car" in tags
    top = flags[0]
    assert top["metric"] == "both_axles_saturated"
    assert "grip" in top["title"].lower() or "ceiling" in top["title"].lower()
    # Balance is NOT separately nagged when both axles are saturated.
    assert not any(f["metric"] == "understeer_index" for f in flags)


def test_persistent_understeer_across_phases_is_a_car_flag():
    session = {"balance": {"both_axles_saturated": False,
                           "understeer_index": 0.5,
                           "reversal_rate_per_min": 3.0,
                           "phases": {"entry": {"usi": 0.586},
                                      "mid": {"usi": 0.74},
                                      "exit": {"usi": 0.42},
                                      "lift": {"usi": 0.70}}},
               "traction": {}}
    flags = _car_flags(session)
    f = next(f for f in flags if f["metric"] == "understeer_index")
    assert f["tag"] == "car"
    assert "understeer" in f["title"].lower()


def test_nervous_car_flag_fires_above_threshold_only():
    base = {"both_axles_saturated": False, "understeer_index": 0.0,
            "phases": {}}
    calm = _car_flags({"balance": dict(base, reversal_rate_per_min=12.0),
                       "traction": {}})
    wild = _car_flags({"balance": dict(base, reversal_rate_per_min=22.0),
                       "traction": {}})
    assert not any(f["metric"] == "reversal_rate_per_min" for f in calm)
    assert any(f["metric"] == "reversal_rate_per_min" for f in wild)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -k car -v`
Expected: FAIL with `ImportError: cannot import name '_car_flags'`

- [ ] **Step 3: Write minimal implementation**

Add the constants and functions to `app/coach.py`:

```python
# thresholds (append to the threshold block)
UNDERSTEER_STRONG = 0.35     # |index| that reads as a real balance bias
OSCILLATION_PER_MIN = 14.0   # limiting axle flips → nervous car

TAG_YOU = "you"
TAG_CAR = "car"
TAG_BOTH = "both"


def _flag(tag: str, severity: float, title: str, detail: str,
          metric: str, value: Any) -> Dict[str, Any]:
    return {"tag": tag, "severity": round(float(severity), 3),
            "title": title, "detail": detail, "metric": metric,
            "value": value}


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -k car -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/coach.py tests/test_coach.py
git commit -m "feat(coach): car flags — grip ceiling, nervous, persistent balance"
```

---

### Task 3: `coach.py` — driver flags and the driver-vs-car triage

This is the showcase: identical driver symptoms get opposite tags depending on what the chassis is doing.

**Files:**
- Modify: `app/coach.py`
- Test: `tests/test_coach.py`

**Interfaces:**
- Consumes: `_flag`, tag constants.
- Produces: `_section_median(sections, key, cats) -> float|None`; `_driver_flags(session: dict, sections: dict|None) -> List[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_coach.py
from app.coach import _driver_flags


def _sec(reapply=None, min_kmh=None, exit_kmh=None, braking_s=None):
    mm = {}
    if reapply is not None: mm["throttle_reapply_s"] = reapply
    if min_kmh is not None: mm["min_kmh"] = min_kmh
    if exit_kmh is not None: mm["exit_kmh"] = exit_kmh
    if braking_s is not None: mm["braking_s"] = braking_s
    return {"hairpin": {"count": 5, "median_metrics": mm}}


def test_lockups_are_a_driver_flag():
    flags, _ = _driver_flags(
        {"balance": {}, "traction": {"near_lock_pct_of_braking": 35.0},
         "full_lock_pct_of_cornering": 0.0}, None)
    f = next(f for f in flags if f["metric"] == "near_lock_pct_of_braking")
    assert f["tag"] == "you"


def test_full_lock_on_a_neutral_car_is_over_driving_you():
    flags, _ = _driver_flags(
        {"balance": {"understeer_index": 0.05}, "traction": {},
         "full_lock_pct_of_cornering": 55.0}, None)
    f = next(f for f in flags if f["metric"] == "full_lock_pct_of_cornering")
    assert f["tag"] == "you"
    assert "sawing" in f["title"].lower() or "over-driving" in f["detail"].lower()


def test_full_lock_with_understeer_flips_triage_to_the_car():
    flags, _ = _driver_flags(
        {"balance": {"understeer_index": 0.5}, "traction": {},
         "full_lock_pct_of_cornering": 55.0}, None)
    f = next(f for f in flags if f["metric"] == "full_lock_pct_of_cornering")
    assert f["tag"] == "both"
    assert "car" in f["title"].lower() or "won't rotate" in f["detail"].lower()


def test_late_throttle_is_you_when_the_car_takes_power():
    flags, _ = _driver_flags(
        {"balance": {}, "traction": {"slide_power_on_s": 2.0,
                                     "slide_off_throttle_s": 3.0},
         "full_lock_pct_of_cornering": 0.0},
        _sec(reapply=1.7))
    f = next(f for f in flags if f["metric"] == "throttle_reapply_s")
    assert f["tag"] == "you"


def test_late_throttle_flips_to_car_when_it_wont_take_power():
    flags, _ = _driver_flags(
        {"balance": {}, "traction": {"slide_power_on_s": 40.0,
                                     "slide_off_throttle_s": 5.0},
         "full_lock_pct_of_cornering": 0.0},
        _sec(reapply=1.7))
    f = next(f for f in flags if f["metric"] == "throttle_reapply_s")
    assert f["tag"] == "car"


def test_over_slowing_entries_is_a_driver_flag():
    flags, _ = _driver_flags(
        {"balance": {}, "traction": {}, "full_lock_pct_of_cornering": 0.0},
        _sec(min_kmh=60, exit_kmh=80, braking_s=1.2))
    assert any(f["metric"] == "min_kmh" and f["tag"] == "you" for f in flags)


def test_power_down_is_a_standalone_car_flag_without_late_throttle():
    flags, pl = _driver_flags(
        {"balance": {}, "traction": {"slide_power_on_s": 40.0,
                                     "slide_off_throttle_s": 5.0},
         "full_lock_pct_of_cornering": 0.0}, None)
    assert pl is True
    f = next(f for f in flags if f["metric"] == "slide_power_on_s")
    assert f["tag"] == "car"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -k "lock or throttle or slowing" -v`
Expected: FAIL with `ImportError: cannot import name '_driver_flags'`

- [ ] **Step 3: Write minimal implementation**

Add to `app/coach.py`:

```python
# thresholds (append)
LOCKUP_PCT = 20.0           # near-lock % of braking → over-braking
FULL_LOCK_PCT = 40.0        # % of cornering at full lock → over-driving / car won't turn
LATE_THROTTLE_S = 1.3       # median reapply delay after apex
POWER_DOWN_RATIO = 1.6      # power-on slide ≫ off-throttle → traction-limited on power
POWER_DOWN_FLOOR = 8.0      # ...and this many seconds, so noise doesn't trip it
OVERSLOW_KMH = 12.0         # exit − min corner speed gap that reads as over-slowed
CORNER_CATS = ("hairpin", "turn", "sweeper")


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

    min_k = _section_median(sections, "min_kmh", ("hairpin", "turn"))
    exit_k = _section_median(sections, "exit_kmh", ("hairpin", "turn"))
    brake_s = _section_median(sections, "braking_s", ("hairpin", "turn"))
    if (min_k is not None and exit_k is not None
            and (exit_k - min_k) >= OVERSLOW_KMH and (brake_s or 0) > 0.5):
        flags.append(_flag(
            TAG_YOU, min(1.0, (exit_k - min_k) / 30.0),
            "You're over-slowing the entries",
            f"You scrub to {min_k:g} km/h mid-corner then run out to "
            f"{exit_k:g} — braking too deep and too hard. Brake earlier and "
            "lighter and carry more entry speed.",
            "min_kmh", min_k))

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -k "lock or throttle or slowing or power" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/coach.py tests/test_coach.py
git commit -m "feat(coach): driver flags + driver-vs-car triage cross-checks"
```

---

### Task 4: `coach.py` — assemble `coach_report`

Combine summary + all flags, add the `data_sufficient` gate, the headline, and severity ordering.

**Files:**
- Modify: `app/coach.py` (rewrite `coach_report`)
- Test: `tests/test_coach.py`

**Interfaces:**
- Consumes: `_race_summary`, `_car_flags`, `_driver_flags`.
- Produces: final `coach_report(report, sections=None) -> dict` per the Data contract.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_coach.py
def test_report_orders_flags_by_severity_and_sets_headline():
    report = {"has_laps": True, "best_lap_s": 53.55,
              "laps": _laps([54.9, 54.2, 53.7, 53.55]),
              "session": {
                  "balance": {"both_axles_saturated": False,
                              "understeer_index": 0.5,
                              "reversal_rate_per_min": 3.0,
                              "phases": {"entry": {"usi": 0.5},
                                         "mid": {"usi": 0.6},
                                         "exit": {"usi": 0.4}}},
                  "traction": {"near_lock_pct_of_braking": 35.0},
                  "full_lock_pct_of_cornering": 55.0}}
    out = coach_report(report, _sec(reapply=1.7,
                                    min_kmh=60, exit_kmh=80, braking_s=1.2))
    assert out["data_sufficient"] is True
    sevs = [f["severity"] for f in out["flags"]]
    assert sevs == sorted(sevs, reverse=True)
    assert out["headline"] == out["flags"][0]["title"]
    assert {f["tag"] for f in out["flags"]} <= {"you", "car", "both"}


def test_clean_run_gets_no_manufactured_criticism():
    report = {"has_laps": True, "best_lap_s": 53.55,
              "laps": _laps([53.7, 53.6, 53.55, 53.6]),
              "session": {"balance": {"both_axles_saturated": False,
                                      "understeer_index": 0.1,
                                      "reversal_rate_per_min": 2.0,
                                      "phases": {}},
                          "traction": {"near_lock_pct_of_braking": 5.0},
                          "full_lock_pct_of_cornering": 10.0}}
    out = coach_report(report, _sec(reapply=0.4))
    assert out["flags"] == []
    assert "nothing" in out["headline"].lower() or "tidy" in out["headline"].lower()


def test_insufficient_data_gates_the_read():
    report = {"has_laps": False, "best_lap_s": None, "laps": [],
              "session": {"balance": {"phases": {}}, "traction": {},
                          "full_lock_pct_of_cornering": None}}
    out = coach_report(report, None)
    assert out["data_sufficient"] is False
    assert out["flags"] == []
    assert "few more laps" in out["headline"].lower() or "not enough" in out["headline"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -k "orders or clean or insufficient" -v`
Expected: FAIL (headline/data_sufficient assertions don't hold — `coach_report` still returns the Task 1 stub)

- [ ] **Step 3: Write minimal implementation**

Replace `coach_report` in `app/coach.py`:

```python
def _corner_count(sections: Optional[Dict[str, Any]]) -> int:
    if not sections:
        return 0
    return sum(int((sections.get(c) or {}).get("count", 0))
               for c in CORNER_CATS)


def coach_report(report: Dict[str, Any],
                 sections: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
```

- [ ] **Step 4: Run the whole coach suite**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/coach.py tests/test_coach.py
git commit -m "feat(coach): assemble report — data gate, headline, severity order"
```

---

### Task 5: endpoint + page route + integration test on synthetic data

**Files:**
- Modify: `app/main.py` (add endpoint near the other `/api/sessions/{id}/...` routes, ~line 653; add page route near `/analysis`, ~line 993)
- Test: `tests/test_coach.py`

**Interfaces:**
- Consumes: `app.laps.lap_report`, `app.sections.detect_sections`, `app.coach.coach_report`, existing `_session_or_404`, `_load_or_404`.
- Produces: `GET /api/sessions/{id}/coach` → the `coach_report` dict; `GET /coach` → serves `coach.html`.

- [ ] **Step 1: Write the failing integration test** (exercises the real producers on a synthetic session, so it protects the wiring/field-names without a DB)

```python
# add to tests/test_coach.py
from app.laps import lap_report
from app.sections import detect_sections
from tests.test_laps import _synthetic_session


def test_coach_report_runs_on_real_synthetic_analysis():
    sd = _synthetic_session(seconds=90.0)
    out = coach_report(lap_report(sd), detect_sections(sd))
    assert set(out) == {"data_sufficient", "headline", "summary", "flags"}
    assert isinstance(out["headline"], str) and out["headline"]
    for f in out["flags"]:
        assert set(f) >= {"tag", "severity", "title", "detail", "metric", "value"}
        assert f["tag"] in ("you", "car", "both")
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest tests/test_coach.py::test_coach_report_runs_on_real_synthetic_analysis -v`
Expected: PASS (this validates coach.py against real field names; if it fails, a field name in coach.py is wrong — fix it before continuing)

- [ ] **Step 3: Add the endpoint and page route**

In `app/main.py`, after the `session_analysis` endpoint (~line 665):

```python
@app.get("/api/sessions/{session_id}/coach")
async def session_coach(session_id: int) -> Dict[str, Any]:
    from .laps import lap_report
    from .sections import detect_sections
    from .coach import coach_report
    row = _session_or_404(session_id)
    sd = _load_or_404(row)
    return coach_report(lap_report(sd), detect_sections(sd))
```

In `app/main.py`, beside the other page routes (after `/analysis`, ~line 996):

```python
@app.get("/coach", response_class=HTMLResponse)
async def coach_page() -> str:
    return _static_html("coach.html")
```

(Use the same static-serving helper the neighbouring page routes use — match `/analysis`'s implementation exactly; if it inlines `FileResponse` or reads a file, copy that pattern rather than inventing `_static_html`.)

- [ ] **Step 4: Verify the endpoint against a real capture**

Run (live instance has real Cayman sessions 151/152):
```bash
curl -s http://192.168.86.205:8080/api/sessions/151/coach | python3 -m json.tool
```
Expected: JSON with `data_sufficient`, `headline`, `summary.line`, and `flags` — sanity-check that the Cayman shows a 🔧 understeer-flavoured read (front 0.94 / rear 0.44). Note: only valid **after** this build is deployed to the live instance; before then, run the local `pytest` integration test above.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_coach.py
git commit -m "feat(coach): /api/sessions/{id}/coach endpoint + /coach page route"
```

---

### Task 6: frontend — shared card renderer, Analysis card, Coach page, nav entry

No JS test harness exists in this repo; verify by loading pages against the live instance after deploy. Keep all logic in Python (done) — these steps only render.

**Files:**
- Modify: `app/static/app.js` (nav array ~line 483–493; exports ~line 543)
- Modify: `app/static/analysis.html` (inject card at top of the analysis render)
- Create: `app/static/coach.html`

**Interfaces:**
- Consumes: `GET /api/sessions/{id}/coach`, `GET /api/sessions` (to resolve latest).
- Produces: `coachCard(container, data)` render helper exported on the global `window.FH` object (the app's single export object, ~line 538). Pages destructure it: `const { ..., coachCard } = FH;`.

- [ ] **Step 1: Add the shared renderer to `app/static/app.js`**

Define `coachCard` inside the app.js IIFE alongside the other render helpers (e.g. `routeChart`), then add it to the `window.FH = { ... }` exports object (~line 538). `esc` is already defined at app.js module scope (~line 104), so it is in scope here:

```javascript
  // Coach's read: renders the /coach endpoint payload into `container`.
  function coachCard(container, data) {
    const chip = { you: "🧍 you", car: "🔧 car", both: "🧍🔧 you + car" };
    if (!data || data.data_sufficient === false) {
      container.innerHTML =
        '<div class="card coach"><div class="coach-head">Coach’s read</div>' +
        '<p class="coach-line">' +
        (data ? esc(data.headline) : "No read yet.") + "</p></div>";
      return;
    }
    const flags = (data.flags || []).map(function (f) {
      return '<div class="coach-flag"><span class="coach-tag tag-' + f.tag +
        '">' + chip[f.tag] + '</span><div><div class="coach-title">' +
        esc(f.title) + "</div><div class=\"coach-detail\">" +
        esc(f.detail) + "</div></div></div>";
    }).join("");
    container.innerHTML =
      '<div class="card coach"><div class="coach-head">Coach’s read</div>' +
      '<p class="coach-headline">' + esc(data.headline) + "</p>" +
      '<p class="coach-line">' + esc((data.summary || {}).line || "") + "</p>" +
      flags + "</div>";
  }
```

`window.FH = { ... }` at ~line 538 is where all helpers are exported; add `coachCard` to it.

- [ ] **Step 2: Inject the card at the top of Analysis**

`analysis.html` already destructures helpers at ~line 328:
`const { api, esc, fmt, fmtLapTime, fmtDuration, barChart, routeChart, toast } = FH;` — add `coachCard` to that list.

Add a mount point at the top of the content container in the HTML:

```html
<div id="coachMount"></div>
```

In the analysis page script, after the session id is resolved (it already reads `session=ID` via `qs`), fetch and render:

```javascript
try {
  const coach = await api("/api/sessions/" + sessionId + "/coach");
  coachCard(document.getElementById("coachMount"), coach);
} catch (e) { /* coach is best-effort; leave the mount empty */ }
```

- [ ] **Step 3: Create `app/static/coach.html`**

A minimal page that resolves the latest session, renders the card, and offers a picker. Copy the exact head/shell boilerplate of `garage.html` (the `<link rel="stylesheet" href="/static/styles.css">`, `<script src="/app.js">`, and the `const { ... } = FH; FH.shell("coach");` opener), then:

```html
<main class="wrap">
  <h1 class="page-title">Coach</h1>
  <select id="coachSel" class="picker"></select>
  <div id="coachMount"></div>
  <p class="muted">Finished a race? This reads your latest run. No data
    leaves your phone.</p>
</main>
<script>
  const { api, esc, coachCard, toast } = FH;
  FH.shell("coach");
  (async function () {
    const sessions = (await api("/api/sessions")).sessions || [];
    const drivable = sessions.filter(function (s) { return s.car_ordinal; });
    const sel = document.getElementById("coachSel");
    sel.innerHTML = drivable.map(function (s) {
      return '<option value="' + s.id + '">' +
        esc(s.car_name || ("Session " + s.id)) + " · " +
        esc((s.created_at || "").slice(0, 10)) + "</option>";
    }).join("");
    async function show(id) {
      coachCard(document.getElementById("coachMount"),
                await api("/api/sessions/" + id + "/coach"));
    }
    sel.addEventListener("change", function () { show(sel.value); });
    if (drivable.length) show(drivable[0].id);
  })();
</script>
```

(`api`, `esc`, `toast` are already on `FH`; `coachCard` is added in Step 1.)

- [ ] **Step 4: Add the Coach nav entry in `app/static/app.js`**

In the nav array (~line 483–493), add after the `garage` entry:

```javascript
    { href: "/coach", key: "coach", label: "Coach",
      icon: '<path d="M12 3l7 4v5c0 4-3 7-7 8-4-1-7-4-7-8V7l7-4z"/><path d="M9.5 12l1.8 1.8 3.2-3.6"/>' },
```

- [ ] **Step 5: Add minimal card styling**

In `app/static/styles.css` (the stylesheet every page links via `/static/styles.css`), next to the existing `.card` rules, add:

```css
.coach-head{font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;opacity:.6}
.coach-headline{font-weight:700;font-size:1.05rem;margin:.3rem 0}
.coach-line{opacity:.8;margin:.2rem 0 .6rem}
.coach-flag{display:flex;gap:.6rem;padding:.5rem 0;border-top:1px solid rgba(255,255,255,.08)}
.coach-tag{white-space:nowrap;font-size:.75rem;font-weight:600}
.coach-title{font-weight:600}
.coach-detail{opacity:.8;font-size:.9rem}
.tag-car{color:#f0a} .tag-you{color:#4cf} .tag-both{color:#fc6}
```

- [ ] **Step 6: Manual verification**

Deploy the new build to the live instance (rebuild the exe or run the dev server), then:
- open `/coach` → latest session shows a read with 🧍/🔧 chips;
- open `/analysis?session=151` → the coach card sits at the top;
- confirm the nav shows a Coach tab and it highlights when active.

- [ ] **Step 7: Commit**

```bash
git add app/static/app.js app/static/analysis.html app/static/coach.html app/static/styles.css
git commit -m "feat(coach): Coach nav tab, shared card renderer, Analysis card"
```

---

### Task 7: validate against real captures, calibrate, release notes

**Files:**
- Modify: `app/coach.py` (threshold tuning only, if needed)
- Modify: `CHANGELOG.md`, `app/__init__.py`
- Test: `tests/test_coach.py` (add a regression once thresholds are locked)

- [ ] **Step 1: Run the coach on the known captures**

With the build on the live instance:
```bash
for id in 151 152; do
  echo "== session $id =="
  curl -s http://192.168.86.205:8080/api/sessions/$id/coach \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['headline']);[print(' ',f['tag'],f['title']) for f in d['flags']]"
done
```
Expected (from the spec's validation set):
- the psychotic 628 kW AWD BMW session → 🔧 nervous and/or grip-ceiling leads;
- Cayman 151 → 🔧 understeer present (front 0.94 / rear 0.44); the 12×/min oscillation must **not** trip "nervous" (that is why `OSCILLATION_PER_MIN = 14`);
- a clean fast session → few/no flags, no manufactured criticism.

- [ ] **Step 2: Adjust thresholds if a verdict is wrong**

If a known session mis-reads, tune the constant (e.g. `OSCILLATION_PER_MIN`, `UNDERSTEER_STRONG`) and re-run. Keep changes to the threshold block only.

- [ ] **Step 3: Lock a regression test for the real Cayman read**

```python
# add to tests/test_coach.py — guards the calibrated behaviour
def test_cayman_like_session_reads_as_car_understeer_not_nervous():
    session = {"balance": {"both_axles_saturated": False,
                           "understeer_index": 0.499,
                           "reversal_rate_per_min": 12.0,
                           "phases": {"entry": {"usi": 0.586},
                                      "mid": {"usi": 0.74},
                                      "exit": {"usi": 0.42},
                                      "lift": {"usi": 0.70}}},
               "traction": {}, "full_lock_pct_of_cornering": 63.0}
    flags, _ = _driver_flags(session, None)
    car = _car_flags(session)
    # 12x/min must NOT read as nervous at the calibrated threshold.
    assert not any(f["metric"] == "reversal_rate_per_min" for f in car)
    # Understeer is the car verdict; full lock is triaged to the car.
    assert any(f["metric"] == "understeer_index" and f["tag"] == "car"
               for f in car)
    assert any(f["metric"] == "full_lock_pct_of_cornering" and f["tag"] == "both"
               for f in flags)
```

- [ ] **Step 4: Run the full suite**

Run: `/home/phil/.venvs/fh6/bin/python -m pytest -q`
Expected: PASS (all existing + new coach tests)

- [ ] **Step 5: CHANGELOG + version bump**

Add a `## [2.8.0]` section to `CHANGELOG.md` describing the local driving coach (summary + driver flags + car flags + honest triage, no AI/network), and bump `app/__init__.py` `__version__` to `2.8.0`. Do **not** create a git tag — release is Phil's call.

- [ ] **Step 6: Commit**

```bash
git add app/coach.py tests/test_coach.py CHANGELOG.md app/__init__.py
git commit -m "feat(coach): calibrate thresholds against real captures; v2.8.0 notes"
```

---

## Notes for the executor

- Run `pytest` with `/home/phil/.venvs/fh6/bin/python -m pytest`.
- The repo commits directly to `main` and pushes after meaningful steps (Phil reviews from origin). Push after each task.
- Do not add any dependency; `coach.py` uses only the stdlib (`statistics`) plus the dict outputs of existing modules.
- Frontend file:line anchors are approximate — confirm against the current file before editing; the nav array and exports object are the stable landmarks in `app.js`.
