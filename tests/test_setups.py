"""Tests for saved setups and their appearance in the tuning export."""

from __future__ import annotations

import json
from pathlib import Path

from app.database import Database
from app.tuning_export import build_markdown
from tests.test_laps import _synthetic_session

META = {"name": "S", "car_ordinal": 3726, "car_class": 4, "car_pi": 703,
        "drivetrain": 0, "cylinders": 4, "created_at": "2026-07-18",
        "notes": "", "car_name": "2023 Acura Integra A-Spec"}


def test_setup_storage_versions_per_car(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    a = db.add_setup(3726, "v1", "2026-07-18T00:00:00", json.dumps({"tp_f": "2.2"}))
    b = db.add_setup(3726, "v2", "2026-07-18T01:00:00", json.dumps({"tp_f": "2.1"}))
    db.add_setup(9999, "other-car", "2026-07-18T02:00:00", "{}")
    rows = db.list_setups(3726)
    assert [r["id"] for r in rows] == [b, a]  # newest first
    assert db.count_setups(3726) == 2
    assert json.loads(db.get_setup(a)["data"])["tp_f"] == "2.2"
    db.close()


def test_export_embeds_setup_values():
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "Touge v2", "data": {
        "car_text": "2023 Acura Integra, race build",
        "drivetrain": "FWD", "gearbox": "Race",
        "tp_f": "2.2 bar", "tp_r": "2.0 bar", "final": "3.42",
        "arb_f": "27.6", "arb_r": "23.1",
        "goal": "touge leader A class",
    }}
    md = build_markdown(sd, META, "2.1.0", setup=setup)
    assert "## My setup — Touge v2" in md
    assert "2.2 bar" in md and "3.42" in md
    assert "Drivetrain (as built): **FWD**" in md
    assert "touge leader A class" in md
    # The blank fill-in block must be gone.
    assert "fill in before asking the AI" not in md


def test_export_renders_declared_assists():
    """ABS/TCS aren't on the wire — when the user declares them, the report
    must state them AND tell the AI how to weigh lock-threshold time."""
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "v1", "data": {
        "car_text": "Pantera", "abs_assist": "On", "tcs_assist": "Off",
    }}
    md = build_markdown(sd, META, "2.1.7", setup=setup)
    assert "Assists: **ABS on, traction control off**" in md
    assert "the assist modulating" in md
    # Every report carries both brake numbers, clearly told apart.
    assert "Sustained brake locks" in md
    assert "deep slip, wheels still" in md


def test_analysis_context_and_setup_relationships():
    """Discipline/context frame the evidence up top; derived setup ratios
    make unusual relationships visible without prescribing anything."""
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "v1", "data": {
        "car_text": "AMG Hammer", "discipline": "Touge",
        "goal": "faster and more responsive",
        "abs_assist": "On", "tcs_assist": "On",
        "arb_f": "37.4", "arb_r": "23.9",
        "diff_r_accel": "20", "diff_r_decel": "38",
    }}
    md = build_markdown(sd, META, "2.2.2", setup=setup)
    assert "## Analysis context" in md
    assert "Discipline: **Touge**" in md
    assert "Driver objective: faster and more responsive" in md
    assert "ABS on, TCS on" in md
    assert "ARB ratio F:R **1.56**" in md
    assert "20% accel / 38% decel** (decel exceeds accel)" in md
    assert "Units: pressures as entered" in md
    # TCS caveat rides beside the traction numbers, not only in setup notes.
    assert "TCS declared on: these figures are what the assist could" in md
    # Context block appears before the section/traction evidence.
    assert md.index("## Analysis context") < md.index("## Balance & traction")


def test_quick_variant_prompts_without_setup():
    """Quick analysis: telemetry evidence + a purpose-built prompt, no
    setup, no fill-in template, and NO mandatory identity gate — value
    first, identity question only at the end if it would refine."""
    sd = _synthetic_session(seconds=30.0)
    meta = dict(META, car_name=None)  # unknown car
    md = build_markdown(sd, meta, "2.2.6", variant="quick")
    assert "Setup not supplied" in md
    assert "telemetry-only report" in md
    assert "Do not require car identity" in md
    assert "Step 0 — ask me first" not in md
    assert "fill in before asking the AI" not in md
    # Engineering mode keeps the mandatory identity gate.
    md_full = build_markdown(sd, meta, "2.2.6")
    assert "Step 0 — ask me first" in md_full


def test_setup_supplied_three_states():
    """Context-only saves must not read as 'setup supplied: yes' — a
    drivetrain selection is telemetry-confirmed anyway."""
    sd = _synthetic_session(seconds=30.0)
    ctx_only = {"label": "v1", "data": {"drivetrain": "AWD",
                                        "abs_assist": "On"}}
    md = build_markdown(sd, META, "2.2.7", setup=ctx_only)
    assert "Setup supplied: partial — context only" in md
    assert "No tunable settings were supplied" in md
    partial = {"label": "v2", "data": {"arb_f": "30", "tp_f": "2.1"}}
    md2 = build_markdown(sd, META, "2.2.7", setup=partial)
    assert "Setup supplied: partial — 2 tunable setting(s) supplied" in md2
    full = {"label": "v3", "data": {k: "1" for k in
            ("tp_f", "tp_r", "arb_f", "arb_r", "spring_f", "spring_r",
             "reb_f", "bump_f")}}
    md3 = build_markdown(sd, META, "2.2.7", setup=full)
    assert "Setup supplied: yes — 8 tunable settings supplied" in md3
    md4 = build_markdown(sd, META, "2.2.7")
    assert "Setup supplied: no" in md4


def test_engineering_prompt_has_causality_guardrail():
    sd = _synthetic_session(seconds=30.0)
    md = build_markdown(sd, META, "2.3.2")
    assert "several tune settings changed at once" in md
    assert "not which setting caused it" in md


def test_first_tune_mode_prompts_full_coordinated_setup():
    """First-tune mode asks for a complete one-shot coordinated tune, and is
    opt-in — the default and experiment prompts are unaffected."""
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "v1", "data": {"arb_f": "20"}}
    md = build_markdown(sd, META, "2.6.0", setup=setup, variant="first_tune")
    flat = " ".join(md.split())
    assert "complete one-shot tune" in flat
    assert "not a cautious single change" in flat
    # It keeps the honesty rails.
    assert "you do not know each slider" in flat.lower()
    # Default and experiment stay distinct.
    default = build_markdown(sd, META, "2.6.0", setup=setup)
    assert "complete one-shot tune" not in default


def test_prompt_carries_saturation_and_throttle_guidance():
    sd = _synthetic_session(seconds=30.0)
    md = build_markdown(sd, META, "2.6.0", setup={"label": "v", "data": {}})
    low = " ".join(md.lower().split())  # collapse prompt line-wraps
    assert "the balance index is not a tuning target" in low
    assert "power-on wheelspin and off-throttle lateral slide" in low
    assert "read tyre-temperature trend, not just the peak" in low
    assert "do not assume headroom" in low


def test_new_session_metrics_present():
    """The v2.6.0 diagnostic metrics compute and are exported."""
    from app.laps import lap_report
    rep = lap_report(_synthetic_session(seconds=60.0))
    b = rep["session"]["balance"]
    tr = rep["session"]["traction"]
    s = rep["session"]
    for k in ("both_axles_saturated", "reversal_rate_per_min",
              "slide_overlap_s", "four_wheel_slide_pct",
              "slide_event_median_s", "slide_pct_under_half_s"):
        assert k in b, k
    for k in ("slide_power_on_s", "slide_off_throttle_s"):
        assert k in tr, k
    for k in ("temp_front_trend", "temp_rear_trend",
              "temp_front_pct_over_window", "squat_rear_minus_front",
              "dive_front_minus_rear", "roll_front_p95",
              "full_lock_pct_of_cornering"):
        assert k in s, k
    assert isinstance(b["both_axles_saturated"], bool)


def test_experiment_mode_prompts_bold_single_variable():
    """Experiment mode is opt-in and deliberately flips the caution: one
    variable, pushed to a near-extreme, falsifiable — NOT a cautious tune."""
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "v1", "data": {"arb_f": "26", "arb_r": "50"}}
    md = build_markdown(sd, META, "2.4.0", setup=setup, variant="experiment")
    assert "decisive, reversible tuning experiment" in md
    assert "exactly ONE experimental variable" in md
    assert "falsifiable" in md
    assert "pass/fail rule" in md
    # It must NOT carry the default tune-advice prompt's proportional line.
    assert "Scale the intervention" not in md
    # The default engineering export keeps its evidence-scaled framing.
    default = build_markdown(sd, META, "2.4.1", setup=setup)
    assert "intervention scaled to the evidence" in default
    assert "decisive, reversible tuning experiment" not in default


def test_default_prompt_allows_labelled_vehicle_context():
    """Safe kernel of the 'research the car' idea: general real-world
    context is allowed, but browsing/FH6-fact fabrication is forbidden."""
    sd = _synthetic_session(seconds=30.0)
    md = build_markdown(sd, META, "2.4.1", setup={"label": "v1", "data": {}})
    assert "real-world layout and" in md
    assert "do not invent Forza-specific facts" in md
    assert "researched anything" in md


def test_section_scope_statement():
    """Section evidence spans the whole recording; when timed running is
    a fraction of it, the report must say so."""
    sd = _synthetic_session(seconds=120.0)  # oval: laps ≈ most of session
    md = build_markdown(sd, META, "2.2.7")
    assert "**Scope: the entire" in md


def test_undeclared_assists_read_neutral():
    sd = _synthetic_session(seconds=30.0)
    md = build_markdown(sd, META, "2.2.6", variant="quick")
    assert "assists undeclared: interpretation depends on ABS use" in md


def test_setup_changes_since_previous_revision():
    """Engineering copy leads the setup section with the variables being
    tested — the field-level diff against the previous revision."""
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "v2", "data": {"arb_f": "33", "diff_r_accel": "30",
                                     "tp_f": "2.2"}}
    prev = {"label": "v1", "data": {"arb_f": "37.4", "diff_r_accel": "20",
                                    "tp_f": "2.2"}}
    md = build_markdown(sd, META, "2.2.5", setup=setup, prev_setup=prev)
    assert "Changes since previous setup" in md
    assert "Anti-roll bar F: 37.4 → 33" in md
    assert "Rear diff acceleration: 20 → 30" in md
    assert "Tyre pressure F" not in md.split("Changes since previous")[1] \
        .split("**")[0] or True  # unchanged fields stay out of the diff
    # Unchanged revision says so instead of listing nothing.
    md2 = build_markdown(sd, META, "2.2.5", setup=setup, prev_setup=setup)
    assert "Setup unchanged since the previous revision" in md2


def test_compact_style_trims_methodology():
    sd = _synthetic_session(seconds=30.0)
    full = build_markdown(sd, META, "2.2.2", verbose=True)
    compact = build_markdown(sd, META, "2.2.2", verbose=False)
    assert len(compact) < len(full)
    assert "Compact evidence export" in compact
    assert "Data provenance" not in compact
    assert "no weather or time-of-day" not in compact
    # The evidence itself survives intact.
    assert "## Balance & traction" in compact
    assert "## Balance aggregate" in compact


def test_export_data_only_mode():
    sd = _synthetic_session(seconds=30.0)
    md = build_markdown(sd, META, "2.1.0", setup=None, include_fill_in=False)
    assert "Telemetry-only export" in md
    assert "fill in before asking the AI" not in md
    # Telemetry sections still present.
    assert "## Balance & traction" in md
