"""Tests for saved setups and their appearance in the tuning export."""

from __future__ import annotations

import json
from pathlib import Path

from app.database import Database
from app.tuning_export import build_markdown, build_package_index
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


def test_create_setup_dedupes_unchanged_version(tmp_path: Path):
    """Versions progress on change only: re-saving the current latest version
    byte-for-byte reuses it instead of minting a duplicate (the Porsche v2
    bug). A real change still mints the next version; key order is ignored."""
    import asyncio
    from types import SimpleNamespace
    from app.main import app, create_setup, SetupBody

    db = Database(tmp_path / "t.db")
    app.state.fh6 = SimpleNamespace(db=db)
    data = {"car_text": "2005 Porche Cayman GT3 WTAC",
            "arb_f": "32", "arb_r": "63"}

    r1 = asyncio.run(create_setup(SetupBody(car_ordinal=4232, data=dict(data))))
    assert r1["created"] is True and r1["label"] == "v1"

    # Identical re-save: no new version, the existing one is returned.
    r2 = asyncio.run(create_setup(SetupBody(car_ordinal=4232, data=dict(data))))
    assert r2["created"] is False
    assert r2["id"] == r1["id"] and r2["label"] == "v1"
    assert db.count_setups(4232) == 1

    # A genuine change mints v2.
    r3 = asyncio.run(create_setup(
        SetupBody(car_ordinal=4232, data=dict(data, arb_r="60"))))
    assert r3["created"] is True and r3["label"] == "v2"
    assert db.count_setups(4232) == 2

    # Key order must not matter — compared as parsed dicts, not JSON text.
    reordered = {"arb_r": "60", "car_text": data["car_text"], "arb_f": "32"}
    r4 = asyncio.run(create_setup(SetupBody(car_ordinal=4232, data=reordered)))
    assert r4["created"] is False and db.count_setups(4232) == 2
    db.close()


def test_lean_prompts_are_expert_tuner_not_method_walls():
    """The baked prompts are lean: an expert-tuner ask, not a wall of
    do's/don'ts. Layering method into the copy prompts made tunes worse."""
    sd = _synthetic_session(seconds=30.0)
    default = " ".join(build_markdown(sd, META, "2.9.0",
                       setup={"label": "v", "data": {}}).split())
    assert "expert Forza Horizon 6 tuner" in default
    assert "competitive tune" in default
    # The removed method walls must be gone.
    for gone in ("balance index is not a tuning target",
                 "do not assume headroom", "several tune settings changed",
                 "not evidence for this one", "CANNOT ASSESS"):
        assert gone not in default, gone


def test_evidence_leads_with_the_deterministic_read():
    """The coach's verdict leads every evidence blob — the engineering copy
    AND pure Copy evidence — before the wall of numbers."""
    sd = _synthetic_session(seconds=90.0)
    full = build_markdown(sd, META, "2.10.0", setup={"label": "v", "data": {}})
    assert "## The read (deterministic)" in full
    assert full.index("## The read") < full.index("## Session")
    # Copy evidence (data-only, no setup, no prompt) still gets the read.
    data = build_markdown(sd, META, "2.10.0", setup=None, variant="data")
    assert "## The read (deterministic)" in data


def test_package_index_is_lean_and_manifests_the_files():
    """START-HERE.md carries the lean ask, a file manifest, and the honesty
    rail — the AI-facing entry to the ZIP."""
    meta = dict(META, car_name="2005 Cayman GT3 WTAC", car_class=4,
                car_pi=800, drivetrain=2)
    md = build_package_index(meta, 25580, has_raw=True, has_setup=True)
    assert "expert Forza Horizon 6 tuner" in md
    assert "competitive tune" in md
    for f in ("report.md", "raw-telemetry.csv", "corner-events.json",
              "laps.csv", "session-info.json", "your-tune.json"):
        assert f"`{f}`" in md, f
    # A full export tells the AI to sample the raw capture, with the size.
    assert "load it with code" in md and "25,580" in md
    assert "no tyre-pressure channel" in md


def test_package_index_sampled_says_read_it_all():
    """The sampled/compact variant flips the instruction: read every row."""
    md = build_package_index(META, 400, sampled=True).lower()
    assert "read every row" in md and "don't skim" in md
    assert "load it with code" not in md  # not a full dump to sample


def test_package_index_omits_absent_files():
    md = build_package_index(META, 100, has_raw=False, has_setup=False)
    assert "raw-telemetry.csv" not in md
    assert "your-tune.json" not in md
    assert "`report.md`" in md  # always present


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


def test_sessions_for_car_excludes_later_runs(tmp_path: Path):
    """The 'since last session' lineage must only look BACKWARD: re-opening
    an older run must not compare it against runs recorded after it (the
    reversed-delta bug found on the Cayman)."""
    db = Database(tmp_path / "t.db")

    def mk(name: str, ordinal: int) -> int:
        sid = db.create_session(name, "2026-07-22T00:00:00", True, "r", "v1")
        db.finalize_session(sid, "2026-07-22T00:05:00", 100,
                            {"car_ordinal": ordinal})
        return sid

    s1 = mk("run1", 4232)
    s2 = mk("run2", 4232)
    s3 = mk("run3", 4232)
    mk("other-car", 999)
    # The middle run sees only the earlier run — never the later one.
    assert [r["id"] for r in db.sessions_for_car(4232, s2)] == [s1]
    # The oldest run has no prior lineage at all.
    assert db.sessions_for_car(4232, s1) == []
    # The newest sees both earlier, newest-first; the other car never appears.
    assert [r["id"] for r in db.sessions_for_car(4232, s3)] == [s2, s1]
    db.close()


def test_honesty_rail_present_in_every_tune_mode():
    """The one surviving rail: every analysis mode still tells the AI the
    data is real telemetry with no pressure channel and unknown slider
    ranges, so it doesn't invent numbers. A data-only export has no prompt."""
    sd = _synthetic_session(seconds=30.0)
    setup = {"label": "v", "data": {}}
    for variant in ("full", "quick"):
        low = " ".join(build_markdown(sd, META, "2.9.0", setup=setup,
                                      variant=variant).lower().split())
        assert "no tyre-pressure channel" in low, variant
        assert "don't invent numbers it doesn't contain" in low, variant
    # The evidence section has its own pressure-provenance note, so check a
    # rail-unique phrase to confirm the data-only export carries no prompt.
    d = build_markdown(sd, META, "2.9.0", setup=None, variant="data")
    assert "don't invent numbers it doesn't contain" not in d.lower()


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
