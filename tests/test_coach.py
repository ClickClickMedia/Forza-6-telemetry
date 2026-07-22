"""Tests for the local, deterministic driving coach."""

from __future__ import annotations

from app.coach import (coach_report, _fmt, _car_flags, _driver_flags)
from app.laps import lap_report
from app.sections import detect_sections
from tests.test_laps import _synthetic_session


def _laps(times):
    return [{"time_s": t, "complete": True} for t in times]


def _sec(reapply=None, min_kmh=None, exit_kmh=None, braking_s=None):
    mm = {}
    if reapply is not None:
        mm["throttle_reapply_s"] = reapply
    if min_kmh is not None:
        mm["min_kmh"] = min_kmh
    if exit_kmh is not None:
        mm["exit_kmh"] = exit_kmh
    if braking_s is not None:
        mm["braking_s"] = braking_s
    return {"hairpin": {"count": 5, "median_metrics": mm}}


# --- race summary ----------------------------------------------------------

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
    assert s["consistency"] in ("tight", "workable")
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


# --- car flags -------------------------------------------------------------

def test_both_axles_saturated_is_a_build_ceiling_car_flag():
    flags = _car_flags({"balance": {"both_axles_saturated": True,
                                    "understeer_index": 0.6, "phases": {}},
                        "traction": {}})
    assert "car" in {f["tag"] for f in flags}
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


# --- driver flags + triage -------------------------------------------------

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


def test_power_down_is_a_standalone_car_flag_without_late_throttle():
    flags, pl = _driver_flags(
        {"balance": {}, "traction": {"slide_power_on_s": 40.0,
                                     "slide_off_throttle_s": 5.0},
         "full_lock_pct_of_cornering": 0.0}, None)
    assert pl is True
    f = next(f for f in flags if f["metric"] == "slide_power_on_s")
    assert f["tag"] == "car"


# --- assembly --------------------------------------------------------------

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
    out = coach_report(report, _sec(reapply=1.7))
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
    assert ("nothing" in out["headline"].lower()
            or "tidy" in out["headline"].lower())


def test_insufficient_data_gates_the_read():
    report = {"has_laps": False, "best_lap_s": None, "laps": [],
              "session": {"balance": {"phases": {}}, "traction": {},
                          "full_lock_pct_of_cornering": None}}
    out = coach_report(report, None)
    assert out["data_sufficient"] is False
    assert out["flags"] == []
    assert ("few more laps" in out["headline"].lower()
            or "not enough" in out["headline"].lower())


# --- integration on real synthetic analysis (guards field names) ----------

def test_coach_report_runs_on_real_synthetic_analysis():
    sd = _synthetic_session(seconds=90.0)
    out = coach_report(lap_report(sd), detect_sections(sd))
    assert set(out) == {"data_sufficient", "headline", "summary", "flags"}
    assert isinstance(out["headline"], str) and out["headline"]
    for f in out["flags"]:
        assert set(f) >= {"tag", "severity", "title", "detail", "metric",
                          "value"}
        assert f["tag"] in ("you", "car", "both")
