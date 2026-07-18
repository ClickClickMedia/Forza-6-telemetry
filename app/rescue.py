"""Rescue recordings made with the v1.0.x packet layout.

v1.0.0 shipped with a mis-specified packet layout: a pad byte was inserted at
offset 244 (before ``PositionX``) instead of the real trailing byte at offset
323, so every dash-tail field (position, speed, power, torque, tyre temps,
boost, fuel, distance, lap times, and the input/gear bytes) was decoded one
byte late — producing wildly fluctuating garbage for those columns while the
sled fields (RPM, velocity, slip, suspension) stayed correct.

Because the recorder stored every decoded field at full float precision, the
original wire bytes can be reconstructed exactly (f32 -> Python float -> CSV
text -> f32 round-trips losslessly) and re-decoded under the corrected layout.
The single unrecoverable byte is the one v1.0.x discarded as padding, which
under the correct layout is ``PositionX``'s least-significant mantissa byte —
a sub-millimetre error.

Every rescued file is validated frame-by-frame with the same physics
cross-check used to pin the layout in the first place: the dash ``Speed``
field must equal the sled ``|Velocity|`` magnitude. Files failing validation
are left untouched.

Runs automatically at startup (old files are detected by their CSV header and
backed up alongside as ``*.v1bak``), or manually::

    python -m app.rescue --data-dir ./data
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import packet
from .database import Database

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Frozen copy of the v1.0.x (wrong) layout, used only to rebuild wire bytes.
# --------------------------------------------------------------------------
_V1_TABLE: List[Tuple[str, str]] = (
    packet._FIELD_TABLE[:58]  # sled, unchanged between versions
    + [
        ("CarGroup", "i"),
        ("SmashableVelDiff", "f"),
        ("SmashableMass", "f"),
        ("_pad", "x"),
    ]
    + [
        (name, code)
        for name, code in packet._FIELD_TABLE
        if packet.FIELD_OFFSETS.get(name, -1) >= 244 and name != "Unknown3"
    ]
)
_V1_FORMAT = "<" + "".join(code for _name, code in _V1_TABLE)
_V1_STRUCT = struct.Struct(_V1_FORMAT)
_V1_NAMES = [name for name, code in _V1_TABLE if code != "x"]

assert _V1_STRUCT.size == packet.FH6_PACKET_SIZE, _V1_STRUCT.size

# Speed must match |Velocity| within this tolerance (m/s) for a frame to
# count as validated. CSV rounding is lossless so the practical error is 0;
# the tolerance only absorbs float32 arithmetic noise.
_VALIDATE_TOL_MS = 0.05
# Fraction of moving frames that must pass validation to accept a rescue.
_VALIDATE_MIN_PASS = 0.99


def _coerce(value: str, code: str) -> Any:
    if code == "f":
        return float(value or 0.0)
    return int(float(value or 0))


def is_v1_csv(path: Path) -> bool:
    """True if the CSV header carries the v1.0.x column set."""
    try:
        with open(path, "r", newline="") as fh:
            header = next(csv.reader(fh), None)
    except OSError:
        return False
    return bool(header) and "SmashableVelDiff" in header and "Unknown3" not in header


def rescue_csv(path: Path, backup: bool = True) -> Dict[str, Any]:
    """Re-decode one v1.0.x CSV in place. Returns a stats dict.

    The original file is preserved next to the rescued one as ``*.v1bak``
    (unless ``backup=False``).
    """
    with open(path, "r", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [row for row in reader if row]

    idx = {name: i for i, name in enumerate(header)}
    code_by_name = {name: code for name, code in _V1_TABLE if code != "x"}

    out_rows: List[List[Any]] = []
    checked = moving = passed = 0
    best_lap: Optional[float] = None

    for row in rows:
        values: Dict[str, Any] = {}
        for name in _V1_NAMES:
            i = idx.get(name)
            raw_val = row[i] if i is not None and i < len(row) else "0"
            try:
                values[name] = _coerce(raw_val, code_by_name[name])
            except (TypeError, ValueError):
                values[name] = 0
        wire = _V1_STRUCT.pack(*[values.get(n, 0) for n in _V1_NAMES])
        frame = packet.parse(wire)

        vmag = math.sqrt(
            frame.VelocityX**2 + frame.VelocityY**2 + frame.VelocityZ**2
        )
        checked += 1
        if vmag > 1.0:
            moving += 1
            if abs(frame.Speed - vmag) < _VALIDATE_TOL_MS:
                passed += 1
        best = packet.sane_lap(frame.BestLap)
        if best > 0:
            if best_lap is None or best < best_lap:
                best_lap = float(best)

        d = frame.as_dict()
        t_mono = row[idx["t_mono"]] if "t_mono" in idx else ""
        t_wall = row[idx["t_wall"]] if "t_wall" in idx else ""
        out_rows.append([t_mono, t_wall] + [d[n] for n in packet.FIELD_NAMES])

    pass_rate = (passed / moving) if moving else 1.0
    ok = pass_rate >= _VALIDATE_MIN_PASS
    stats = {
        "path": str(path),
        "frames": checked,
        "moving_frames": moving,
        "speed_check_pass_rate": round(pass_rate, 4),
        "rescued": ok,
        "best_lap": best_lap,
    }
    if not ok:
        log.error("rescue validation FAILED, leaving file untouched",
                  extra={"extra": stats})
        return stats

    if backup:
        bak = path.with_suffix(path.suffix + ".v1bak")
        if not bak.exists():
            path.replace(bak)
        src_note = str(bak)
    else:
        src_note = "(no backup)"

    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_mono", "t_wall"] + packet.FIELD_NAMES)
        writer.writerows(out_rows)

    log.info("rescued v1.0.x recording",
             extra={"extra": {**stats, "backup": src_note}})
    return stats


def rescue_data_dir(sessions_dir: Path, db: Optional[Database] = None) -> List[Dict[str, Any]]:
    """Rescue every v1.0.x CSV found in ``sessions_dir``.

    When ``db`` is given, each rescued session's ``best_lap`` metadata is
    recomputed from the corrected frames (v1.0.x stored garbage or NULL).
    """
    results: List[Dict[str, Any]] = []
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.exists():
        return results

    for path in sorted(sessions_dir.glob("session_*.csv")):
        if not is_v1_csv(path):
            continue
        stats = rescue_csv(path)
        results.append(stats)
        if db is not None and stats["rescued"]:
            try:
                sid = int(path.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            row = db.get_session(sid)
            if row is not None:
                db.finalize_session(
                    sid,
                    row.get("ended_at") or row.get("created_at"),
                    stats["frames"],
                    {
                        "car_ordinal": row.get("car_ordinal"),
                        "car_class": row.get("car_class"),
                        "car_pi": row.get("car_pi"),
                        "drivetrain": row.get("drivetrain"),
                        "cylinders": row.get("cylinders"),
                        "car_group": row.get("car_group"),
                        "best_lap": stats["best_lap"],
                    },
                )

    for path in sorted(sessions_dir.glob("session_*.parquet")):
        log.warning(
            "old-format parquet recording detected; automatic rescue supports "
            "CSV only — re-export or open an issue if you need this file",
            extra={"extra": {"path": str(path)}},
        )
    return results


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Rescue FH6 telemetry recordings made with v1.0.x"
    )
    ap.add_argument("--data-dir", default="data",
                    help="data directory containing sessions/ (default: data)")
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    sessions_dir = data_dir / "sessions"
    db_path = data_dir / "sessions.db"
    db = Database(db_path) if db_path.exists() else None
    results = rescue_data_dir(sessions_dir, db)
    if not results:
        print("No v1.0.x recordings found — nothing to do.")
    for r in results:
        state = "rescued" if r["rescued"] else "FAILED VALIDATION"
        print(f"{r['path']}: {state} "
              f"({r['frames']} frames, speed-check pass rate "
              f"{r['speed_check_pass_rate']*100:.1f}%"
              + (f", best lap {r['best_lap']:.3f}s" if r["best_lap"] else "")
              + ")")
    if db is not None:
        db.close()


if __name__ == "__main__":
    _main()
