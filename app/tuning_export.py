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
from .sections import detect_sections
from .session_data import SessionData

# Display maps (presentation only — raw ints always shown too). Class letters
# follow the FH5-era Data Out convention; unverified for FH6, so exports show
# both letter and raw int.
CAR_CLASS = {0: "D", 1: "C", 2: "B", 3: "A", 4: "S1", 5: "S2", 6: "X"}
DRIVETRAIN = {0: "FWD", 1: "RWD", 2: "AWD"}

AI_PROMPT = """\
Analyse the telemetry evidence above together with the analysis context,
driver note (if any), setup values and tune lineage. This export is
evidence, not a diagnosis — the interpretation is yours.

Prioritise:
1. Lap time and like-for-like comparisons (same route, discipline and
   conditions).
2. The driver's described problem area.
3. Behaviour by section type: hairpin, turn, sweeper, transfer, straight.
4. The representative section samples, not only session-wide averages.
5. The smallest tune change that tests the strongest hypothesis.

Do not assume a lower understeer index is automatically faster.
Do not recommend changes from session-wide averages alone.
Distinguish circuit, touge, dirt and other disciplines from the evidence.
Treat wet, night or rewind-affected running separately where declared.
"No setup change recommended" is a valid answer when evidence is weak,
the clock is improving, or driver variance dominates.
Never invent data this export does not contain.
"""

# Condition words scanned in the driver's session note — Forza broadcasts
# NO weather or time-of-day (verified at packet level, including the one
# unmapped byte, through an actual rain-to-dry session), so conditions are
# user-declared and these keywords gate temperature confidence.
_WET_WORDS = ("rain", "wet", "storm", "snow", "drying", "damp")
_DARK_WORDS = ("night", "dusk", "dawn")


def _declared_conditions(notes: str):
    low = (notes or "").lower()
    wet = [w for w in _WET_WORDS if w in low]
    dark = [w for w in _DARK_WORDS if w in low]
    return wet, dark


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


def _handling_summary(add, session: Dict[str, Any], verdicts: Dict[str, Any],
                      notes: str = "") -> None:
    """Severity/confidence-ranked headline so the AI leads with what matters."""
    bal = (verdicts or {}).get("balance", {})
    temps = (verdicts or {}).get("tyre_temps", {})
    usi = bal.get("understeer_index", 0.0) or 0.0
    verdict = bal.get("verdict", "")
    if verdict in ("insufficient cornering data", ""):
        return
    delta = (temps.get("front", {}).get("avg_c", 0) or 0) - (temps.get("rear", {}).get("avg_c", 0) or 0)
    wet, dark = _declared_conditions(notes)
    thermally_consistent = (usi > 0 and delta > 5) or (usi < 0 and delta < -5)
    phases = bal.get("phases") or session.get("balance", {}).get("phases") or {}
    ranked = sorted(
        ((p["usi"], p.get("time_s", 0), pname)
         for pname, p in phases.items() if p.get("usi") is not None),
        key=lambda x: abs(x[0]), reverse=True)
    add("## Balance aggregate (session-wide — a summary of the section "
        "evidence above, not the headline)")
    add("")
    add(f"- Session-wide balance: **{verdict}** — understeer index "
        f"{usi:+.3f} (positive = front sliding more) · front axle at "
        f"{session.get('balance', {}).get('front_slip_angle_corner_avg', 0):.2f} of grip limit vs rear "
        f"{session.get('balance', {}).get('rear_slip_angle_corner_avg', 0):.2f} · "
        f"front−rear temp delta {delta:+.1f} °C "
        f"(thermal story {'agrees' if thermally_consistent else 'does not clearly agree'} "
        f"with the slip story)")
    if ranked:
        add("- Understeer index by corner phase (largest magnitude first): "
            + " · ".join(f"**{name}** {u:+.3f} ({ts:.0f}s)"
                         for u, ts, name in ranked))
    corner_sample = sum(ts for _, ts, _ in ranked)
    quality = ("thin" if corner_sample < 20 else
               "adequate" if corner_sample < 60 else "rich")
    cond_txt = (", ".join(wet + dark) + " (user-declared)") if (wet or dark) \
        else "not declared — assumed dry (note them on the Analysis page if not)"
    add(f"- Evidence quality: cornering sample {corner_sample:.0f} s "
        f"({quality}) · drift/opposite-lock excluded "
        f"{bal.get('pct_drifting', 0):.0f}% · conditions: {cond_txt}")
    if wet:
        add(f"- Wet/mixed running declared ({', '.join(wet)}): temperature "
            f"and grip figures include wet frames.")
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


def _num(v) -> Optional[float]:
    """Parse the leading number out of a free-text setup value ('2.3 bar',
    '37.4', '-2.0°') — None when there isn't one."""
    import re
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(v or ""))
    return float(m.group(1)) if m else None


def _setup_relationships(add, data: Dict[str, Any]) -> None:
    """Factual ratios between supplied setup values — they make unusual
    relationships visible without prescribing anything."""
    pairs = [
        ("ARB ratio F:R", "arb_f", "arb_r"),
        ("Spring ratio F:R", "spring_f", "spring_r"),
        ("Aero ratio F:R", "aero_f", "aero_r"),
        ("Rebound:bump ratio F", "reb_f", "bump_f"),
        ("Rebound:bump ratio R", "reb_r", "bump_r"),
    ]
    lines = []
    for label, ka, kb in pairs:
        a, b = _num(data.get(ka)), _num(data.get(kb))
        if a is not None and b:
            lines.append(f"{label} **{a / b:.2f}**")
    for label, kacc, kdec in (("Rear diff", "diff_r_accel", "diff_r_decel"),
                              ("Front diff", "diff_f_accel", "diff_f_decel")):
        acc, dec = _num(data.get(kacc)), _num(data.get(kdec))
        if acc is not None and dec is not None:
            inv = " (decel exceeds accel)" if dec > acc else ""
            lines.append(f"{label} **{acc:.0f}% accel / {dec:.0f}% decel**{inv}")
    if lines:
        add("- Setup relationships *(derived from the values above — "
            "factual, not judgements)*: " + " · ".join(lines))


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
        add("  - ABS on: lock-threshold braking time reflects the assist "
            "modulating, not wheels stopping.")
    elif abs_a == "Off":
        add("  - ABS off: lock-threshold time is driver threshold braking.")
    if tcs == "On":
        add("  - Traction control on: recorded wheelspin is what the assist "
            "could not contain (a floor, not the full traction picture).")
    filled = [(label, str(data.get(key)).strip())
              for key, label in SETUP_FIELDS
              if str(data.get(key) or "").strip()]
    if filled:
        add("")
        add(_md_table(["Setting", "Value"], [[l, v] for l, v in filled]))
        add("")
        add("- Units: pressures as entered (bar/psi); camber/toe/caster in "
            "degrees; springs, ride height, ARB, damping, aero and diff "
            "percentages in Forza's tuning-screen units.")
        _setup_relationships(add, data)
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


_SAMPLE_SKIP = {"t_start"}


def _fmt_sample(inst: Dict[str, Any]) -> str:
    bits = []
    for k, v in inst.items():
        if k in _SAMPLE_SKIP:
            continue
        if isinstance(v, float):
            v = f"{v:g}"
        elif isinstance(v, list):
            v = "/".join(str(x) for x in v)
        bits.append(f"{k} {v}")
    return " · ".join(bits)


def _section_evidence(add, sections: Dict[str, Any],
                      verbose: bool = True) -> None:
    """Per-category driving evidence with representative samples — what
    happened, where and how often; interpretation stays with the analyst."""
    cats = [c for c in ("hairpin", "turn", "sweeper", "transfer",
                        "straight", "launch")
            if sections.get(c, {}).get("count")]
    if not cats:
        return
    add("## Section evidence")
    add("")
    if verbose:
        add("*(Every cornering event in the session, classified by shape — "
            "session-wide averages hide how differently a car behaves in a "
            "hairpin versus a fast sweeper versus a flick. Categories are "
            "**mutually exclusive**: a transfer's two component corners count "
            "only under transfer, and one event spans contiguous "
            "same-direction cornering, so it may cover linked bends. `start` "
            "is session-relative mm:ss. Full instance list: the sections.json "
            "export.)*")
    else:
        add("*(Mutually exclusive categories; `start` is session-relative "
            "mm:ss; full instance list in sections.json.)*")
    add("")
    th = sections.get("thresholds", {})
    for cat in cats:
        b = sections[cat]
        add(f"### {cat.capitalize()} × {b['count']}"
            + (f"  *({th.get(cat)})*" if th.get(cat) else ""))
        add("")
        med = b.get("median_metrics") or {}
        if med:
            add("- Medians: " + " · ".join(
                f"{k} {v:g}" for k, v in med.items()
                if k not in ("t_start",)))
        if b.get("only"):
            add(f"- Only one qualifying instance detected: "
                f"{_fmt_sample(b['only'])}")
        else:
            add(f"- Samples ordered by **{b.get('ranked_by', '?')}** — "
                f"lowest/median/highest are factual positions on that "
                f"metric, not quality judgements:")
            for label in ("lowest", "median", "highest"):
                inst = b.get(label)
                if inst:
                    add(f"  - {label}: {_fmt_sample(inst)}")
        add("")


def _lineage_section(add, lineage: List[Dict[str, Any]],
                     current: Dict[str, Any] = None) -> None:
    """Earlier sessions with the same car: the before/after evidence a tune
    iteration is judged against."""
    rows = []
    note_lines = []
    for prev in lineage:
        s = prev.get("summary") or {}
        best = s.get("best_s") or prev.get("best_lap")
        best_txt = _fmt_lap_time(best) if best else "–"
        if best and s.get("timing") == "runs":
            best_txt += " (run)"
        rows.append([
            prev.get("name", "?"),
            (prev.get("created_at") or "")[:10],
            best_txt,
            f"{s['usi']:+.3f}" if s.get("usi") is not None else "–",
            (f"{s['spin_total_s']:.1f} ({s.get('spin_multi_s') or 0:.1f} multi)"
             if s.get("spin_total_s") is not None else "–"),
            f"{s['lock_s']:.1f}" if s.get("lock_s") is not None else "–",
            (f"{s['temp_f_c']:.0f}/{s['temp_r_c']:.0f}"
             if s.get("temp_f_c") is not None else "–"),
            f"{s['max_kmh']:.0f}" if s.get("max_kmh") is not None else "–",
            str(s.get("shifts")) if s.get("shifts") is not None else "–",
        ])
        note = (prev.get("notes") or "").strip()
        if note:
            note_lines.append(f"- {prev.get('name', '?')}: {note}")
    add("## Tune lineage — earlier sessions with this car")
    add("")
    add("*(Same Forza ordinal, newest first. \"(run)\" times cover a whole "
        "staged event, not one lap — only compare like with like, and only "
        "treat a row as a baseline if the route and build match; ask me if "
        "unsure.)*")
    add("")
    # Development read vs the most recent prior session with stored data.
    # Deltas are calculated ONLY when route equivalence is established
    # (same timing kind, timed-loop lengths within 5%) — a confidently
    # wrong cross-route "improvement" is worse than no comparison.
    prev = next((p for p in lineage if p.get("summary")), None)
    if prev and current:
        s = prev["summary"]
        pr, cr = s.get("lap_route_m"), current.get("lap_route_m")
        same_kind = s.get("timing") == current.get("timing")
        route_match = (same_kind and pr and cr
                       and abs(pr - cr) / max(pr, cr) <= 0.05)
        if route_match:
            bits = []
            pb, cb = s.get("best_s"), current.get("best_s")
            if pb and cb:
                bits.append(f"best {_fmt_lap_time(pb)} → {_fmt_lap_time(cb)} "
                            f"({cb - pb:+.3f} s)")
            pu, cu = s.get("usi"), current.get("usi")
            if pu is not None and cu is not None:
                bits.append(f"USI {pu:+.3f} → {cu:+.3f}")
            ps, cs = s.get("spin_total_s"), current.get("spin_total_s")
            if ps is not None and cs is not None:
                bits.append(f"wheelspin {ps:.1f} → {cs:.1f} s")
            if bits:
                add(f"**Since last session** ({prev.get('name', '?')}, "
                    f"timed-loop length matches within 5%): "
                    + " · ".join(bits)
                    + " *(conditions not verifiable — confirm)*")
                add("")
        else:
            add(f"Previous session found ({prev.get('name', '?')}), but no "
                f"performance delta calculated — route equivalence was not "
                f"established (timed-loop lengths differ, mismatch, or "
                f"older data without a route fingerprint).")
            add("")
    add(_md_table(
        ["Session", "Date", "Best", "USI", "Wheelspin s", "Lock s",
         "F/R °C", "Max km/h", "Shifts"], rows))
    add("")
    if note_lines:
        add("Driver notes on earlier sessions (authoritative results and "
            "tune versions live here):")
        for line in note_lines:
            add(line)
        add("")


def build_markdown(sd: SessionData, meta: Dict[str, Any], version: str,
                   setup: Dict[str, Any] = None,
                   include_fill_in: bool = True,
                   lineage: List[Dict[str, Any]] = None,
                   verbose: bool = True) -> str:
    """Render the full tuning report for one session.

    ``setup`` embeds saved tuning-screen values (replacing the blank
    fill-in block); ``include_fill_in=False`` produces the data-only
    variant ("copy data only"): telemetry, derived values and lineage, but
    no handling headline, no fill-in template and no AI prompt.
    ``lineage`` lists earlier same-car sessions with their stored
    summaries for the before/after table.
    """
    rep = lap_report(sd)
    session = rep["session"] or {}
    verdicts = rep["verdicts"] or {}
    balance = verdicts.get("balance", {})
    temps = verdicts.get("tyre_temps", {})
    try:
        sections = detect_sections(sd)
    except Exception:  # section evidence must never sink the whole report
        sections = None

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
    if verbose:
        add("**Data provenance** — three kinds of numbers appear below, labelled:")
        add("- *Telemetry*: read directly from the game's Data Out stream.")
        add("- *Estimated*: derived from telemetry with stated filters "
            "(observed peaks, detected runs, verdicts).")
        add("- *User-entered*: car identity beyond the ordinal, and every build/"
            "tune value — Forza does **not** broadcast upgrades, weight, tyre "
            "compound, pressures, or any tuning-screen setting.")
    else:
        add("*Compact evidence export — numbers are labelled telemetry / "
            "estimated / user-entered; methodology notes live in the "
            "detailed export.*")
    add("")

    data_only = not include_fill_in and setup is None

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
        if peaks.get("pct_at_peak_power") is not None:
            add(f"- Time at ≥90% of that observed peak: "
                f"**{peaks['pct_at_peak_power']:.1f}%** of moving time "
                f"*(utilisation relative to this session's own peak — low "
                f"values suggest gearing keeps the engine out of its best "
                f"range, or the track never lets it stretch)*")
    add("")

    # The same telemetry means different things on circuit, touge or dirt
    # — frame the evidence before any of it is read.
    sdata = (setup or {}).get("data") or {}
    notes = (meta.get("notes") or "").strip()
    wet_w, dark_w = _declared_conditions(notes)
    add("## Analysis context")
    add("")
    add(f"- Discipline: **{sdata.get('discipline') or 'not declared'}** "
        f"*(user-entered)*")
    if sdata.get("goal"):
        add(f"- Driver objective: {sdata['goal']}")
    add(f"- Driver note for this session: "
        + (notes if notes else "*(none — the ✎ result-note button on the "
           "Analysis page records felt problems, official times and "
           "conditions)*"))
    assists_bits = []
    if sdata.get("abs_assist"):
        assists_bits.append(f"ABS {sdata['abs_assist'].lower()}")
    if sdata.get("tcs_assist"):
        assists_bits.append(f"TCS {sdata['tcs_assist'].lower()}")
    add("- Assists: " + (", ".join(assists_bits) if assists_bits
                         else "not declared"))
    add("- Setup supplied: " + ("yes" if setup else "no"))
    add("- Conditions: " + (", ".join(wet_w + dark_w) + " (user-declared)"
                            if (wet_w or dark_w)
                            else "not declared — assumed dry"))
    add("")

    add("## Session")
    add("")
    add(f"- Recorded: {meta.get('created_at', '?')}")
    add(f"- Duration: {session.get('duration_s', 0):.0f} s · "
        f"Distance: {session.get('distance_m', 0) / 1000:.2f} km")
    best = rep.get("best_lap_s")
    n_complete = sum(1 for l in rep["laps"] if l.get("complete"))
    if rep.get("lap_source") == "position-gate":
        n_partial = sum(1 for l in rep["laps"] if not l.get("complete"))
        ev = rep.get("event_time_s")
        add(f"- Staged circuit with **no lap data on the wire**: total "
            f"event time **{_fmt_lap_time(ev) if ev else '?'}** · "
            f"**{n_complete} complete laps** split at gate returns (the car "
            f"repeatedly re-passed the same point within {25:.0f} m "
            f"travelling the same direction; crossing times interpolated "
            f"between frames)"
            + (f" · {n_partial} partial segment(s) — never ranked"
               if n_partial else "")
            + f" · best lap **{_fmt_lap_time(best)}** "
            f"*(estimated, line-to-line; a mid-race rewind stretches that "
            f"lap's time and route but never corrupts the others)*")
    elif rep["has_laps"]:
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

    if sections:
        _section_evidence(add, sections, verbose=verbose)

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
    meta_dt = DRIVETRAIN.get(meta.get("drivetrain"))
    trac_dt = trac.get("drivetrain")
    if meta_dt and trac_dt and trac_dt != "?" and meta_dt != trac_dt:
        # Never publish convincing-but-wrong traction evidence: if the car
        # metadata and the analyser disagree on the drivetrain, every
        # driven-wheel figure below would be watching the wrong axle.
        add(f"- **ERROR: drivetrain mismatch — car metadata says "
            f"{meta_dt}, traction analyser resolved {trac_dt}. All "
            f"driven-wheel traction findings for this session are "
            f"suppressed; treat section wheelspin values as the only "
            f"traction evidence and report this session's raw CSV as a "
            f"bug.**")
        trac = {}
    add(f"- Traction validation *(estimated)*: drivetrain "
        f"**{trac.get('drivetrain', '?')}**, driven wheels watched: "
        f"{', '.join(trac.get('driven_wheels', []))} · peak driven-wheel "
        f"slip ratio {trac.get('driven_slip_peak', 0):.2f} · p95 "
        f"{trac.get('driven_slip_p95', 0):.2f}")
    add(f"- Wheelspin (driven wheels): {trac.get('wheelspin_events', 0)} event(s) · "
        f"{trac.get('wheelspin_total_s', 0):.1f} s total · "
        f"longest {trac.get('wheelspin_longest_s', 0):.1f} s "
        f"(grouped: ≥100 ms, 300 ms recovery gap)"
        + (" — **TCS declared on: these figures are what the assist could "
           "not contain, a floor not the full picture**"
           if ((setup or {}).get("data") or {}).get("tcs_assist") == "On"
           else ""))
    byw = trac.get("wheelspin_by_wheel_s") or {}
    if byw and trac.get("wheelspin_total_s", 0) > 0:
        single = sum(byw.values())
        multi = trac.get("wheelspin_multi_s", 0) or 0
        turning = trac.get("wheelspin_turning_s", 0) or 0
        straight = trac.get("wheelspin_straight_s", 0) or 0
        which = ("mostly single-wheel flare" if single > multi * 1.5 else
                 "mostly all-driven-wheel spin" if multi > single * 1.5 else
                 "mixed single/all-wheel spin")
        where = ("mostly while turning" if turning > straight * 1.5 else
                 "mostly on straights" if straight > turning * 1.5 else
                 "split between corners and straights")
        add(f"- Wheelspin pattern *(estimated)*: **{which}, {where}**")
        add(f"- Wheelspin split (mutually exclusive; buckets sum to the total): "
            + " · ".join(f"{w} only {s:.1f} s" for w, s in byw.items())
            + f" · multiple driven wheels {multi:.1f} s — "
            f"while turning {turning:.1f} s / straight {straight:.1f} s")
    if (trac.get("drivetrain") == "FWD"
            and trac.get("wheelspin_total_s", 0) > 15
            and (peaks.get("power_kw") or 0) > 250):
        add(f"- Build-context observation *(estimated)*: "
            f"{trac.get('wheelspin_total_s', 0):.0f} s of driven-wheel "
            f"wheelspin with {peaks['power_kw']:.0f} kW observed through the "
            f"front axle.")
    add(f"- Sustained brake locks *(detector: {trac.get('brake_lock_method', 'wheel-speed deficit')})*: "
        f"front {trac.get('brake_lock_front_events', 0)} event(s) / "
        f"{trac.get('brake_lock_front_s', 0):.1f} s · rear "
        f"{trac.get('brake_lock_rear_events', 0)} event(s) / "
        f"{trac.get('brake_lock_rear_s', 0):.1f} s · "
        f"**{trac.get('lock_pct_of_braking', 0):.0f}% of braking time** "
        f"({trac.get('braking_time_s', 0):.0f} s under brakes; handbrake excluded)")
    add(f"- Braking at the lock threshold (ABS-style slip modulation, wheels "
        f"still turning): {trac.get('near_lock_s', 0):.1f} s = "
        f"**{trac.get('near_lock_pct_of_braking', 0):.0f}% of braking time**"
        + (" — **ABS declared on: this is the assist modulating, not wheels "
           "stopping**"
           if ((setup or {}).get("data") or {}).get("abs_assist") == "On"
           else " — with ABS on this is the assist modulating, not wheels "
                "stopping"))
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

    # Session-wide aggregate AFTER the section evidence: it summarises the
    # sections, it is not the headline conclusion.
    _handling_summary(add, session, verdicts,
                      notes=str(meta.get("notes") or ""))

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
    add(f"- Working window for reference: {window[0]:.0f}–{window[1]:.0f} °C — "
        f"a generic road-racing heuristic; actual targets vary by tyre "
        f"compound, which telemetry does not report")
    wet_w, dark_w = _declared_conditions(str(meta.get("notes") or ""))
    if wet_w or dark_w:
        add(f"- ⚠ Conditions declared in the session note "
            f"({', '.join(wet_w + dark_w)}): temperatures from wet or night "
            f"running read low.")
    if verbose:
        add("- Forza broadcasts **no weather or time-of-day** (verified at "
            "packet level through a rain-to-dry session, including the one "
            "unmapped byte) — conditions come from the driver's session note "
            "only.")
        add("- Honesty note: Forza's Data Out has **no tyre-pressure channel** and "
            "**one temperature per tyre** (no inner/middle/outer). Pressure and "
            "camber advice must come from the setup values filled in below plus "
            "these axle averages — never from fabricated sensor detail.")
    front = temps.get("front", {})
    rear = temps.get("rear", {})
    short_run = (session.get("duration_s") or 0) < 300
    if short_run:
        add(f"- Front axle active-driving avg **{front.get('avg_c', '?')} °C** · "
            f"rear **{rear.get('avg_c', '?')} °C** — short session: "
            f"steady-state may not have been reached, so no in/below/above-"
            f"window conclusion is drawn from this run alone")
    else:
        add(f"- Front axle avg **{front.get('avg_c', '?')} °C** → {front.get('verdict', '?')} · "
            f"Rear axle avg **{rear.get('avg_c', '?')} °C** → {rear.get('verdict', '?')}")
    add(f"- Front−rear delta: **{session.get('temp_fr_delta_c', 0):+.1f} °C** "
        f"(positive = fronts hotter / working harder · negative = rears "
        f"hotter / working harder)")
    add("")

    add("## Gearing")
    add("")
    g = session.get("gearing", {})
    add(f"- Top gear used: {g.get('top_gear', '?')} · "
        f"Upshifts: {g.get('shift_count', 0)} · "
        f"Avg shift RPM: {g.get('shift_rpm_avg') or '–'}")
    if g.get("shift_rpm_spread") is not None:
        add(f"- Shift-point spread (p10–p90 of upshift RPM): "
            f"**{g['shift_rpm_spread']:.0f} rpm** *(driver-variance signal)*")
    add(f"- Time on limiter (≥97% max RPM): {g.get('pct_on_limiter', 0):.1f} %")
    add("")

    if (rep["has_laps"] or rep.get("has_runs")) and rep["laps"]:
        add("## Timed runs (detected from event staging)" if rep.get("has_runs")
            else "## Laps (split at start-line returns)"
            if rep.get("lap_source") == "position-gate" else "## Laps")
        add("")
        rows = []
        for l in rep["laps"]:
            if l.get("run"):
                label = f"Run {l['run']}"
                extra = f" · {l.get('route_m', 0) / 1000:.1f} km route"
            else:
                label = str(l["lap"]) if l["lap"] is not None else "–"
                extra = (f" · {l['route_m'] / 1000:.2f} km"
                         if l.get("route_m") else "")
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
        complete = [l for l in rep["laps"]
                    if l.get("complete") and l.get("time_s")
                    and l.get("lap") is not None]
        if len(complete) >= 3:
            times = sorted(l["time_s"] for l in complete)
            spread = times[-1] - times[0]
            median = times[len(times) // 2]
            pct = spread / median * 100.0 if median else 0.0
            thr = [l["inputs"]["pct_full_throttle"] for l in complete]
            brk = [l["inputs"]["pct_braking"] for l in complete]
            add(f"- Lap consistency *(driver signal, {len(complete)} complete "
                f"laps)*: spread {spread:.3f} s = **{pct:.1f}% of the median "
                f"lap** · full-throttle {min(thr):.0f}–{max(thr):.0f}% · "
                f"braking {min(brk):.0f}–{max(brk):.0f}% "
                f"*(for scale: above ~2% spread, lap-to-lap variance "
                f"typically exceeds the effect of a single setup change)*")
            add("")

    if lineage:
        from .laps import compact_summary
        _lineage_section(add, lineage, current=compact_summary(rep) or {})

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

    if not data_only:
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
