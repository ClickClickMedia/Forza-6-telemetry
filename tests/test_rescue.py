"""Tests for the v1.0.x recording rescue.

Strategy: build known-good wire packets, mis-decode them exactly the way
v1.0.x did (pad byte before PositionX instead of a trailing byte), write that
mis-decoded data as a v1-format CSV, run the rescue, and assert the corrected
CSV reproduces the original values.
"""

from __future__ import annotations

import csv
import math
import struct
from pathlib import Path

from app import packet
from app.rescue import _V1_NAMES, _V1_STRUCT, is_v1_csv, rescue_csv


def _make_wire_frame(i: int) -> bytes:
    """A realistic 324-byte wire packet with self-consistent Speed/Velocity."""
    vx, vy, vz = 3.0 * i, 4.0 * i, 12.0 * i
    speed = math.sqrt(vx * vx + vy * vy + vz * vz)  # = 13 * i
    values = {name: 0 for name in packet.FIELD_NAMES}
    values.update({
        "IsRaceOn": 1,
        "TimestampMS": 1000 + i,
        "EngineMaxRpm": 8000.0,
        "CurrentEngineRpm": 3000.0 + 10.0 * i,
        "VelocityX": vx, "VelocityY": vy, "VelocityZ": vz,
        "CarOrdinal": 3198, "CarClass": 2, "CarPerformanceIndex": 600,
        "DrivetrainType": 1, "NumCylinders": 8, "CarGroup": 12,
        "PositionX": 2700.0 + i, "PositionY": 445.0, "PositionZ": 4900.0 - i,
        "Speed": speed,
        "Power": 250000.0, "Torque": 400.0 + i,
        "TireTempFrontLeft": 190.0, "TireTempFrontRight": 191.0,
        "TireTempRearLeft": 200.0, "TireTempRearRight": 201.0,
        "Boost": 9.5, "Fuel": 1.0,
        "DistanceTraveled": 100.0 * i,
        "BestLap": 83.123, "LastLap": 84.5, "CurrentLap": 12.0 + i,
        "CurrentRaceTime": 500.0 + i,
        "LapNumber": 2, "RacePosition": 1,
        "Accel": min(255, 40 * i), "Brake": 0, "Gear": 3, "Steer": -10,
    })
    return packet.pack(values)


def _write_v1_csv(path: Path, wires: list) -> None:
    """Mis-decode wire packets exactly as v1.0.x did and write its CSV."""
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_mono", "t_wall"] + _V1_NAMES)
        for i, wire in enumerate(wires):
            v1_values = dict(zip(_V1_NAMES, _V1_STRUCT.unpack(wire)))
            writer.writerow(
                [round(i / 60.0, 6), "2026-07-18T03:00:00+00:00"]
                + [v1_values[n] for n in _V1_NAMES]
            )


def test_v1_csv_detection(tmp_path: Path):
    p = tmp_path / "session_000001.csv"
    _write_v1_csv(p, [_make_wire_frame(i) for i in range(1, 4)])
    assert is_v1_csv(p)

    # A current-format file must NOT be detected as v1.
    p2 = tmp_path / "session_000002.csv"
    with open(p2, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["t_mono", "t_wall"] + packet.FIELD_NAMES)
    assert not is_v1_csv(p2)


def test_rescue_recovers_true_values(tmp_path: Path):
    wires = [_make_wire_frame(i) for i in range(1, 6)]
    p = tmp_path / "session_000001.csv"
    _write_v1_csv(p, wires)

    stats = rescue_csv(p)
    assert stats["rescued"], stats
    assert stats["frames"] == 5
    assert stats["speed_check_pass_rate"] == 1.0
    assert abs(stats["best_lap"] - 83.123) < 1e-3

    # Backup of the original mis-decoded file exists.
    assert (tmp_path / "session_000001.csv.v1bak").exists()

    # Rescued rows carry the true values (modulo the one discarded pad byte,
    # which is PositionX's least-significant mantissa byte).
    with open(p, "r", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    assert header == ["t_mono", "t_wall"] + packet.FIELD_NAMES
    idx = {n: i for i, n in enumerate(header)}
    for i, row in enumerate(rows, start=1):
        true = packet.parse(wires[i - 1])
        assert abs(float(row[idx["Speed"]]) - true.Speed) < 1e-4
        assert abs(float(row[idx["Torque"]]) - true.Torque) < 1e-3
        assert abs(float(row[idx["TireTempRearLeft"]]) - 200.0) < 1e-3
        assert abs(float(row[idx["BestLap"]]) - 83.123) < 1e-3
        assert int(row[idx["Gear"]]) == 3
        assert int(row[idx["Accel"]]) == min(255, 40 * i)
        assert abs(float(row[idx["PositionX"]]) - true.PositionX) < 0.001

    # Running the rescue again must be a no-op (file is now current format).
    assert not is_v1_csv(p)


def test_rescue_rejects_corrupt_data(tmp_path: Path):
    """If repacked frames fail the physics cross-check, nothing is rewritten."""
    wires = [bytearray(_make_wire_frame(i)) for i in range(1, 6)]
    # Corrupt the Speed field on every wire frame so Speed != |Velocity|.
    for w in wires:
        struct.pack_into("<f", w, 256, 999.0)
    p = tmp_path / "session_000001.csv"
    _write_v1_csv(p, [bytes(w) for w in wires])
    before = p.read_text()

    stats = rescue_csv(p)
    assert not stats["rescued"]
    assert p.read_text() == before  # untouched
    assert not (tmp_path / "session_000001.csv.v1bak").exists()
