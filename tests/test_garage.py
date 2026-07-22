"""Tests for the garage aggregation (one entry per car, from sessions)."""

from __future__ import annotations

from app.main import _garage_aggregate


def _s(ordinal, created, best=None, cls=4, pi=800, dt=2):
    return {"car_ordinal": ordinal, "created_at": created, "best_lap": best,
            "car_class": cls, "car_pi": pi, "drivetrain": dt, "cylinders": 6}


def test_garage_groups_and_aggregates():
    # Newest-first, as list_sessions returns.
    sessions = [
        _s(3037, "2026-07-22T04:00", best=58.842),   # BMW, newest
        _s(3037, "2026-07-22T03:00", best=61.0),
        _s(3037, "2026-07-21T10:00", best=59.5),      # oldest BMW
        _s(348, "2026-07-20T12:00", best=207.4),      # Ford GT
        _s(0, "2026-07-22T05:00"),                    # menu frame — skipped
        _s(None, "2026-07-22T05:00"),                 # no ordinal — skipped
    ]
    names = {3037: "BMW 325i", 348: "Ford GT"}
    tunes = {3037: 3, 348: 1}
    cars = _garage_aggregate(sessions, names.get, tunes.get)

    assert [c["ordinal"] for c in cars] == [3037, 348]  # by last_driven desc
    bmw = cars[0]
    assert bmw["car_name"] == "BMW 325i"
    assert bmw["session_count"] == 3
    assert bmw["best_lap"] == 58.842            # min across sessions
    assert bmw["last_driven"] == "2026-07-22T04:00"
    assert bmw["first_driven"] == "2026-07-21T10:00"
    assert bmw["tune_versions"] == 3
    # Identity fields come from the newest session.
    assert bmw["car_class"] == 4 and bmw["drivetrain"] == 2


def test_garage_includes_named_car_with_no_sessions():
    cars = _garage_aggregate([], {9999: "Fresh Build"}.get, lambda o: 0,
                             extra_named={9999: "Fresh Build"})
    assert len(cars) == 1
    c = cars[0]
    assert c["ordinal"] == 9999 and c["car_name"] == "Fresh Build"
    assert c["session_count"] == 0 and c["best_lap"] is None


def test_garage_empty():
    assert _garage_aggregate([], lambda o: None, lambda o: 0) == []
