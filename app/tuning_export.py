"""Export a session as an AI-ready tuning report (Markdown) or lap CSV.

The Markdown export is designed to be pasted straight into Claude or ChatGPT:
compact tables, explicit units, the tuning verdicts this tool computed, a
fill-in section for the player's current setup (telemetry cannot see spring
rates or pressures), and a prompt that tells the model exactly what to do
with all of it.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .laps import WHEELS, lap_report
from .session_data import SessionData

# Display maps (presentation only — raw ints always shown too). Class letters
# follow the FH5-era Data Out convention; unverified for FH6, so exports show
# both letter and raw int.
CAR_CLASS = {0: "D", 1: "C", 2: "B", 3: "A", 4: "S1", 5: "S2", 6: "X"}
DRIVETRAIN = {0: "FWD", 1: "RWD", 2: "AWD"}

AI_PROMPT = """\
You are an experienced Forza Horizon tuner. Above is telemetry from my
session, exported by the open-source FH6 telemetry tool, plus my current
setup values where I've filled them in.

Work through it in this order:
1. Read the balance verdict, understeer index (positive = understeer,
   negative = oversteer), and front/rear slide times. Diagnose the car's
   dominant handling problem, citing the specific numbers.
2. Check tyre temperatures against the working window shown. Cold = not
   enough load/camber/pressure too high for that axle; hot = overworked axle
   (too much roll stiffness there, pressure too low, or driving style).
   Front-vs-rear delta indicates which end is doing the work.
3. Check traction events (wheelspin, brake lock) against drivetrain type,
   and suspension travel / bottom-out counts for spring & ride-height issues.
4. Check gearing: time on limiter and average shift RPM.
5. Propose specific setup changes, most impactful first. For each: the
   setting, direction and rough magnitude, and which telemetry number
   justifies it. Stay within what Forza's tuning screen exposes (tyre
   pressure, gearing, camber/toe/caster, anti-roll bars, springs, ride
   height, damping, aero, diff, brake balance/pressure).
6. If my setup values are missing for a setting you want to change, say what
   to look at in-game and give your best directional advice anyway.
7. Finish with the single highest-priority change and what I should feel
   from the driver's seat if it works.
"""


def _fmt_lap_time(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "–"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}:{s:06.3f}"


def _md_table(headers: List[str], rows: List[List[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _handling_summary(add, session: Dict[str, Any], verdicts: Dict[str, Any]) -> None:
    """Severity/confidence-ranked headline so the AI leads with what matters."""
    bal = (verdicts or {}).get("balance", {})
    temps = (verdicts or {}).get("tyre_temps", {})
    usi = bal.get("understeer_index", 0.0) or 0.0
    verdict = bal.get("verdict", "")
    if verdict in ("insufficient cornering data", ""):
        return
    delta = (temps.get("front", {}).get("avg_c", 0) or 0) - (temps.get("rear", {}).get("avg_c", 0) or 0)
    severity = "Severe" if abs(usi) > 0.30 else "Moderate" if abs(usi) > 0.15 else "Mild"
    # Confidence rises when the thermal story agrees with the slip story.
    thermally_consistent = (usi > 0 and delta > 5) or (usi < 0 and delta < -5)
    confidence = "High" if thermally_consistent and abs(usi) > 0.15 else "Medium"
    phases = bal.get("phases") or session.get("balance", {}).get("phases") or {}
    worst_phase = None
    worst_val = 0.0
    for pname, p in phases.items():
        if p.get("usi") is not None and abs(p["usi"]) > abs(worst_val):
            worst_val = p["usi"]
            worst_phase = pname
    add("## Handling summary")
    add("")
    add(f"- **Primary issue:** {verdict} · Severity **{severity}** · "
        f"Confidence **{confidence}**")
    add(f"- Evidence: understeer index {usi:+.3f} · front axle at "
        f"{session.get('balance', {}).get('front_slip_angle_corner_avg', 0):.2f} of grip limit vs rear "
        f"{session.get('balance', {}).get('rear_slip_angle_corner_avg', 0):.2f} · "
        f"front−rear temp delta {delta:+.1f} °C")
    if worst_phase:
        add(f"- Worst corner phase: **{worst_phase}** ({worst_val:+.3f}) — "
            f"target the fix there first")
    if bal.get("caveat"):
        add(f"- ⚠ {bal['caveat']}")
    add("")


# Setup fields rendered in the report, in tuning-screen order. Values are
# stored as free strings (unit-agnostic — enter what the game shows).
SETUP_FIELDS = [
    ("tp_f", "Tyre pressure F"), ("tp_r", "Tyre pressure R"),
    ("final", "Final drive"), ("gears", "Per-gear ratios"),
    ("camber_f", "Camber F"), ("camber_r", "Camber R"),
    ("toe_f", "Toe F"), ("toe_r", "Toe R"), ("caster", "Caster"),
    ("arb_f", "Anti-roll bar F"), ("arb_r", "Anti-roll bar R"),
    ("spring_f", "Springs F"), ("spring_r", "Springs R"),
    ("ride_f", "Ride height F"), ("ride_r", "Ride height R"),
    ("reb_f", "Rebound damping F"), ("reb_r", "Rebound damping R"),
    ("bump_f", "Bump damping F"), ("bump_r", "Bump damping R"),
    ("aero_f", "Aero F"), ("aero_r", "Aero R"),
    ("diff_f_accel", "Front diff acceleration"), ("diff_f_decel", "Front diff deceleration"),
    ("diff_r_accel", "Rear diff acceleration"), ("diff_r_decel", "Rear diff deceleration"),
    ("diff_accel", "Diff acceleration"), ("diff_decel", "Diff deceleration"),  # legacy
    ("diff_centre", "Diff centre balance"),
    ("brake_bal", "Brake balance"), ("brake_pres", "Brake pressure"),
]


def _setup_section(add, setup: Dict[str, Any]) -> None:
    data = setup.get("data") or {}
    add(f"## My setup — {setup.get('label', 'current')} *(user-entered)*")
    add("")
    if data.get("car_text"):
        add(f"- Car & build: **{data['car_text']}**")
    if data.get("drivetrain"):
        add(f"- Drivetrain (as built): **{data['drivetrain']}**")
    if data.get("gearbox"):
        add(f"- Gearbox upgrade: **{data['gearbox']}**")
    abs_a, tcs = data.get("abs_assist"), data.get("tcs_assist")
    if abs_a or tcs:
        parts = ([f"ABS {abs_a.lower()}"] if abs_a else []) + \
                ([f"traction control {tcs.lower()}"] if tcs else [])
        add(f"- Assists: **{', '.join(parts)}**")
    if abs_a == "On":
        add("  - With ABS on, time at the lock threshold is the assist "
            "working as intended — judge brake pressure only on sustained "
            "locks, stopping instability, or overshot corners.")
    elif abs_a == "Off":
        add("  - ABS is off: lock-threshold time is driver threshold "
            "braking; sustained locks point at brake pressure/balance.")
    if tcs == "On":
        add("  - With traction control on, recorded wheelspin is what the "
            "assist could not contain — treat it as a floor, not the full "
            "traction picture.")
    filled = [(label, str(data.get(key)).strip())
              for key, label in SETUP_FIELDS
              if str(data.get(key) or "").strip()]
    if filled:
        add("")
        add(_md_table(["Setting", "Value"], [[l, v] for l, v in filled]))
    # Core settings a tuner will miss if absent — say so explicitly rather
    # than leaving the AI to guess whether they were omitted or don't exist.
    core = ["tp_f", "tp_r", "camber_f", "camber_r", "toe_f", "toe_r",
            "arb_f", "arb_r", "spring_f", "spring_r", "aero_f", "aero_r"]
    labels = dict((k, l) for k, l in SETUP_FIELDS)
    missing = [labels[k] for k in core if not str(data.get(k) or "").strip()]
    if missing:
        add("")
        add(f"- Not provided: {', '.join(missing)}")
    if data.get("goal"):
        add("")
        add(f"- **What I want:** {data['goal']}")
    add("")


def build_markdown(sd: SessionData, meta: Dict[str, Any], version: str,
                   setup: Dict[str, Any] = None,
                   include_fill_in: bool = True) -> str:
    """Render the full tuning report for one session.

    ``setup`` embeds saved tuning-screen values (replacing the blank
    fill-in block); ``include_fill_in=False`` produces the telemetry-only
    variant ("copy data only").
    """
    rep = lap_report(sd)
    session = rep["session"] or {}
    verdicts = rep["verdicts"] or {}
    balance = verdicts.get("balance", {})
    temps = verdicts.get("tyre_temps", {})

    cls_raw = meta.get("car_class")
    cls = CAR_CLASS.get(cls_raw, "?") if cls_raw is not None else "?"
    dt_raw = meta.get("drivetrain")
    drivetrain = DRIVETRAIN.get(dt_raw, "?") if dt_raw is not None else "?"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: List[str] = []
    add = lines.append

    add(f"# Forza Horizon 6 tuning report — {meta.get('name', 'session')}")
    add("")
    add(f"Generated by [Forza-6-telemetry](https://github.com/ClickClickMedia/Forza-6-telemetry) "
        f"v{version} on {generated}. Telemetry units: temps °C (converted "
        f"from the game's °F), speed km/h, slip channels normalized "
        f"(1.0 = grip limit), suspension travel normalised 0–1.")
    add("")
    add("**Data provenance** — three kinds of numbers appear below, labelled:")
    add("- *Telemetry*: read directly from the game's Data Out stream.")
    add("- *Estimated*: derived from telemetry with stated filters "
        "(observed peaks, detected runs, verdicts).")
    add("- *User-entered*: car identity beyond the ordinal, and every build/"
        "tune value — Forza does **not** broadcast upgrades, weight, tyre "
        "compound, pressures, or any tuning-screen setting.")
    add("")

    # --- Handling summary: the one thing to fix first, with evidence -----
    _handling_summary(add, session, verdicts)

    add("## Car")
    add("")
    car_name = (meta.get("car_name") or "").strip()
    setup_car = str(((setup or {}).get("data") or {}).get("car_text") or "").strip()
    if not car_name and setup_car:
        car_name = setup_car  # the saved setup already knows the car
    if car_name:
        add(f"- Car: **{car_name}** (Forza ordinal {meta.get('car_ordinal', '?')}) "
            f"*(name user-entered/community; ordinal from telemetry)*")
    else:
        add(f"- Car: **Unknown car — ordinal {meta.get('car_ordinal', '?')}** "
            f"*(Forza sends only the ordinal; name it once on the Analysis "
            f"page and it fills in automatically)*")
    add(f"- Class: **{cls}** (raw {cls_raw}) · PI **{meta.get('car_pi', '?')}** *(telemetry)*")
    add(f"- Drivetrain: **{drivetrain}** · Cylinders: {meta.get('cylinders', '?')} *(telemetry)*")
    peaks = session.get("observed_peaks") or {}
    if peaks.get("power_kw") is not None:
        add(f"- Observed peak power during session: **{peaks['power_kw']:.0f} kW** · "
            f"observed peak torque: **{peaks['torque_nm']:.0f} N·m** "
            f"*(estimated from {peaks.get('samples', 0)} valid-pull samples "
            f"covering {peaks.get('coverage_s', 0):.1f} s — throttle ≥95%, "
            f"sustained; NOT the garage's rated build figures)*")
    add("")

    add("## Session")
    add("")
    add(f"- Recorded: {meta.get('created_at', '?')}")
    add(f"- Duration: {session.get('duration_s', 0):.0f} s · "
        f"Distance: {session.get('distance_m', 0) / 1000:.2f} km")
    best = rep.get("best_lap_s")
    n_complete = sum(1 for l in rep["laps"] if l.get("complete"))
    if rep["has_laps"]:
        add(f"- Laps completed: {n_complete} · Best lap: **{_fmt_lap_time(best)}**")
    elif rep.get("has_runs"):
        add(f"- Staged event with **no lap data on the wire**: "
            f"**{n_complete} timed run(s)** · best **{_fmt_lap_time(best)}** "
            f"*(estimated from the start-line staging signature — Horizon "
            f"sends no lap fields for this event type. If this was a "
            f"multi-lap circuit, the time covers the full race; per-lap "
            f"splitting for such events is on the roadmap)*")
    else:
        add("- Free roam / point-to-point (no lap markers in this session)")
    add(f"- Max speed: {session.get('speed', {}).get('max_kmh', 0):.0f} km/h *(telemetry)*")
    add(f"- Sustained cornering grip: **{session.get('lat_g_sustained', 0):.2f} g** "
        f"(best lateral-G held 0.4 s while steering at speed; spikes and "
        f"airborne excluded — banking and compressions may still "
        f"contribute) · frame p99 {session.get('lat_g_p99', 0):.2f} g · "
        f"raw single-frame max {session.get('max_lat_g', 0):.2f} g — raw max "
        f"excluded from handling analysis (collisions/kerbs/landings)")
    notes = (meta.get("notes") or "").strip()
    if notes:
        add(f"- Driver notes: {notes}")
    add("")

    add("## Tyres")
    add("")
    tyres = session.get("tyres_c", {})
    add(_md_table(
        ["Tyre", "Avg °C", "Median °C", "Max °C"],
        [[w, tyres.get(w, {}).get("avg", "–"),
          tyres.get(w, {}).get("median", "–"),
          tyres.get(w, {}).get("max", "–")]
         for w in WHEELS],
    ))
    add("")
    add("- Averages/medians use **active-driving frames only** (≥40 km/h) — "
        "stationary and cool-down time excluded.")
    if session.get("traction", {}).get("rear_temps_wire_identical"):
        add("- Note: the game broadcasts **identical rear-left and rear-right "
            "temperatures** for this car (verified at the packet level) — "
            "Forza models the rear axle jointly here; it is not a sensor or "
            "parser fault.")
    window = temps.get("window_c", [77, 99])
    add(f"- Working window used for verdicts: {window[0]:.0f}–{window[1]:.0f} °C — "
        f"a generic road-racing heuristic (community optimal 88–99 °C, usable "
        f"77–121 °C); actual targets vary by tyre compound, which telemetry "
        f"does not report")
    add("- Honesty note: Forza's Data Out has **no tyre-pressure channel** and "
        "**one temperature per tyre** (no inner/middle/outer). Pressure and "
        "camber advice must come from the setup values filled in below plus "
        "these axle averages — never from fabricated sensor detail.")
    front = temps.get("front", {})
    rear = temps.get("rear", {})
    add(f"- Front axle avg **{front.get('avg_c', '?')} °C** → {front.get('verdict', '?')} · "
        f"Rear axle avg **{rear.get('avg_c', '?')} °C** → {rear.get('verdict', '?')}")
    add(f"- Front−rear delta: **{session.get('temp_fr_delta_c', 0):+.1f} °C** "
        f"(positive = fronts hotter / working harder · negative = rears "
        f"hotter / working harder)")
    add("")

    add("## Balance & traction")
    add("")
    add(f"- Balance verdict: **{balance.get('verdict', '?')}** · "
        f"understeer index {balance.get('understeer_index', 0):+.3f} "
        f"(mean normalized |front slip angle| − |rear|, cornering on grip; "
        f"front axle ran {balance.get('front_slip_angle_corner_avg', 0):.2f} "
        f"of grip limit, rear {balance.get('rear_slip_angle_corner_avg', 0):.2f})")
    if balance.get("caveat"):
        add(f"- ⚠ {balance['caveat']} "
            f"(drift/opposite-lock time: {balance.get('pct_drifting', 0):.0f} % of session)")
    sb = session.get("balance", {})
    for axle in ("front", "rear"):
        ev, tot, lng = (sb.get(f"{axle}_slide_events", 0),
                        sb.get(f"{axle}_slide_time_s", 0),
                        sb.get(f"{axle}_slide_longest_s", 0))
        if (ev == 0) != (tot == 0) or tot + 1e-6 < lng:
            add(f"- ⚠ Data-quality warning: {axle} slide metrics failed an "
                f"internal consistency check and were omitted.")
            break
    else:
        add(f"- Slides (hysteresis 1.05 enter / 0.90 exit, ≥100 ms): "
            f"front {sb.get('front_slide_events', 0)}× · "
            f"{sb.get('front_slide_time_s', 0):.1f} s total · "
            f"longest {sb.get('front_slide_longest_s', 0):.1f} s — "
            f"rear {sb.get('rear_slide_events', 0)}× · "
            f"{sb.get('rear_slide_time_s', 0):.1f} s total · "
            f"longest {sb.get('rear_slide_longest_s', 0):.1f} s")
    phases = sb.get("phases") or {}
    phase_bits = []
    for pname in ("entry", "mid", "exit", "lift"):
        p = phases.get(pname) or {}
        if p.get("usi") is not None:
            phase_bits.append(f"{pname} {p['usi']:+.3f} ({p['time_s']:.0f}s)")
    if phase_bits:
        add("- Balance by corner phase (understeer index per phase; positive "
            "= front sliding more): " + " · ".join(phase_bits))
    trac = session.get("traction", {})
    add(f"- Traction validation *(estimated)*: drivetrain "
        f"**{trac.get('drivetrain', '?')}**, driven wheels watched: "
        f"{', '.join(trac.get('driven_wheels', []))} · peak driven-wheel "
        f"slip ratio {trac.get('driven_slip_peak', 0):.2f} · p95 "
        f"{trac.get('driven_slip_p95', 0):.2f}")
    add(f"- Wheelspin (driven wheels): {trac.get('wheelspin_events', 0)} event(s) · "
        f"{trac.get('wheelspin_total_s', 0):.1f} s total · "
        f"longest {trac.get('wheelspin_longest_s', 0):.1f} s "
        f"(grouped: ≥100 ms, 300 ms recovery gap)")
    byw = trac.get("wheelspin_by_wheel_s") or {}
    if byw and trac.get("wheelspin_total_s", 0) > 0:
        add(f"- Wheelspin split (mutually exclusive; buckets sum to the total): "
            + " · ".join(f"{w} only {s:.1f} s" for w, s in byw.items())
            + f" · multiple driven wheels {trac.get('wheelspin_multi_s', 0):.1f} s — "
            f"while turning {trac.get('wheelspin_turning_s', 0):.1f} s / "
            f"straight {trac.get('wheelspin_straight_s', 0):.1f} s "
            f"(one-wheel flare → more diff lock; all-wheel spin → less "
            f"power or more tyre, not more lock)")
    if (trac.get("drivetrain") == "FWD"
            and trac.get("wheelspin_total_s", 0) > 15
            and (peaks.get("power_kw") or 0) > 250):
        add(f"- **Build-level signal** *(estimated)*: "
            f"{trac.get('wheelspin_total_s', 0):.0f} s of driven-wheel "
            f"wheelspin with {peaks['power_kw']:.0f} kW observed through the "
            f"front axle — the chassis/tyres may not be able to deploy this "
            f"output. Worth asking whether tyre compound/width upgrades (or "
            f"trading power away) fit the goal better than tune changes alone.")
    add(f"- Sustained brake locks *(detector: {trac.get('brake_lock_method', 'wheel-speed deficit')})*: "
        f"front {trac.get('brake_lock_front_events', 0)} event(s) / "
        f"{trac.get('brake_lock_front_s', 0):.1f} s · rear "
        f"{trac.get('brake_lock_rear_events', 0)} event(s) / "
        f"{trac.get('brake_lock_rear_s', 0):.1f} s · "
        f"**{trac.get('lock_pct_of_braking', 0):.0f}% of braking time** "
        f"({trac.get('braking_time_s', 0):.0f} s under brakes; handbrake excluded)")
    add(f"- Braking at the lock threshold (ABS-style slip modulation, wheels "
        f"still turning): {trac.get('near_lock_s', 0):.1f} s = "
        f"**{trac.get('near_lock_pct_of_braking', 0):.0f}% of braking time** — "
        f"with ABS on this is normal threshold braking, not a fault; only "
        f"recommend brake-pressure changes on sustained lock or instability")
    inputs = session.get("inputs", {})
    add(f"- Full throttle: {inputs.get('pct_full_throttle', 0):.1f} % of session · "
        f"Braking: {inputs.get('pct_braking', 0):.1f} %")
    add("")

    add("## Suspension (normalised travel, 0 = full extension, 1 = bottomed)")
    add("")
    susp = session.get("suspension", {})
    add(_md_table(
        ["Corner", "Avg", "Max"],
        [[w, susp.get(w, {}).get("avg", "–"), susp.get(w, {}).get("max", "–")]
         for w in WHEELS],
    ))
    add("")
    add(f"- Bottom-out: {session.get('suspension_bottom_out_events', 0)} "
        f"sustained event(s) (≥0.98 for ≥3 frames) · "
        f"{session.get('suspension_bottom_raw_crossings', 0)} raw threshold "
        f"crossing(s) · {session.get('suspension_time_at_bottom_s', 0):.2f} s "
        f"total at ≥0.98 · longest continuous "
        f"{session.get('suspension_bottom_longest_s', 0):.2f} s · "
        f"99th-percentile travel {session.get('suspension_travel_p99', 0):.2f} "
        f"(brief single-frame spikes can touch 1.00 without a sustained event)")
    add("")

    add("## Gearing")
    add("")
    g = session.get("gearing", {})
    add(f"- Top gear used: {g.get('top_gear', '?')} · "
        f"Upshifts: {g.get('shift_count', 0)} · "
        f"Avg shift RPM: {g.get('shift_rpm_avg') or '–'}")
    add(f"- Time on limiter (≥97% max RPM): {g.get('pct_on_limiter', 0):.1f} %")
    add("")

    if (rep["has_laps"] or rep.get("has_runs")) and rep["laps"]:
        add("## Timed runs (detected from event staging)" if rep.get("has_runs")
            else "## Laps")
        add("")
        rows = []
        for l in rep["laps"]:
            if l.get("run"):
                label = f"Run {l['run']}"
                extra = f" · {l.get('route_m', 0) / 1000:.1f} km route"
            else:
                label = str(l["lap"]) if l["lap"] is not None else "–"
                extra = ""
            rows.append([
                label,
                _fmt_lap_time(l.get("time_s"))
                + ("" if l.get("complete") else " *(partial)*") + extra,
                f"{l['speed']['avg_kmh']:.0f}",
                f"{l['speed']['max_kmh']:.0f}",
                f"{l['inputs']['pct_full_throttle']:.0f}",
                f"{l['inputs']['pct_braking']:.0f}",
                f"{l['balance']['understeer_index']:+.3f}",
                f"{l['temp_fr_delta_c']:+.1f}",
            ])
        add(_md_table(
            ["Lap", "Time", "Avg km/h", "Max km/h", "Full-thr %", "Brake %",
             "USI", "F−R °C"],
            rows,
        ))
        add("")

    if setup is not None:
        _setup_section(add, setup)
    elif include_fill_in:
        add("## My current setup (fill in before asking the AI)")
        add("")
        add("```")
        add("Tyre pressure  F: ___    R: ___   (bar or psi, as the game shows)")
        add("Gearing        final: ___   (per-gear if custom)")
        add("Camber         F: ___°      R: ___°")
        add("Toe            F: ___°      R: ___°")
        add("Caster         ___°")
        add("Anti-roll bars F: ___       R: ___")
        add("Springs        F: ___       R: ___   Ride height F/R: ___ / ___")
        add("Damping rebound F/R: ___ / ___   bump F/R: ___ / ___")
        add("Aero           F: ___       R: ___")
        add("Differential   accel/decel: ___ / ___  (centre balance if AWD)")
        add("Brakes         balance: ___  pressure: ___")
        add("What I want    e.g. \"less mid-corner understeer, keep exit traction\"")
        add("```")
        add("")
    else:
        add("## Setup values")
        add("")
        add("*(Telemetry-only export — no setup values provided. If you want "
            "setup-specific advice, ask me for my current settings.)*")
        add("")

    add("## Prompt for the AI")
    add("")
    if not car_name:
        add(f"**Step 0 — ask me first:** this tool only knows the car as "
            f"Forza ordinal {meta.get('car_ordinal', '?')}. Before any "
            f"analysis, ask me the year, make, model and my key build "
            f"choices (engine/aspiration swaps, tyre compound, aero), and "
            f"use my answer as the car identity throughout.")
        add("")
    add(AI_PROMPT)
    return "\n".join(lines)


# --- Per-lap CSV -----------------------------------------------------------
_LAP_CSV_HEADER = [
    "lap", "run", "route_m", "complete", "time_s", "duration_s", "distance_m",
    "avg_kmh", "max_kmh", "pct_full_throttle", "pct_braking",
    "understeer_index", "front_slide_time_s", "rear_slide_time_s",
    "wheelspin_events", "brake_lock_events",
    "temp_fl_avg_c", "temp_fr_avg_c", "temp_rl_avg_c", "temp_rr_avg_c",
    "temp_front_avg_c", "temp_rear_avg_c", "temp_fr_delta_c",
    "susp_fl_max", "susp_fr_max", "susp_rl_max", "susp_rr_max",
    "bottom_out_events", "top_gear", "shift_rpm_avg", "pct_on_limiter",
    "max_lat_g",
]


def build_laps_csv(sd: SessionData) -> str:
    rep = lap_report(sd)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_LAP_CSV_HEADER)
    for l in rep["laps"]:
        t = l.get("tyres_c", {})
        s = l.get("suspension", {})
        writer.writerow([
            l.get("lap"), l.get("run"), l.get("route_m"),
            int(bool(l.get("complete"))), l.get("time_s"),
            l.get("duration_s"), l.get("distance_m"),
            l["speed"]["avg_kmh"], l["speed"]["max_kmh"],
            l["inputs"]["pct_full_throttle"], l["inputs"]["pct_braking"],
            l["balance"]["understeer_index"],
            l["balance"]["front_slide_time_s"],
            l["balance"]["rear_slide_time_s"],
            l["traction"]["wheelspin_events"],
            l["traction"]["brake_lock_events"],
            t.get("FL", {}).get("avg"), t.get("FR", {}).get("avg"),
            t.get("RL", {}).get("avg"), t.get("RR", {}).get("avg"),
            l.get("temp_front_avg_c"), l.get("temp_rear_avg_c"),
            l.get("temp_fr_delta_c"),
            s.get("FL", {}).get("max"), s.get("FR", {}).get("max"),
            s.get("RL", {}).get("max"), s.get("RR", {}).get("max"),
            l.get("suspension_bottom_out_events"),
            l["gearing"]["top_gear"], l["gearing"]["shift_rpm_avg"],
            l["gearing"]["pct_on_limiter"],
            l.get("max_lat_g"),
        ])
    return buf.getvalue()
