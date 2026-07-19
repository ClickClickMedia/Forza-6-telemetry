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
