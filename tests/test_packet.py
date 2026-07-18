"""Tests for the FH6 packet specification and parser.

These are the safety net for the single most error-prone part of the project:
the byte-for-byte packet layout. They assert the total size, the exact offset
of every field, the FH6-specific field insertions/omissions, little-endian
decoding, round-trip stability, and robustness against malformed input.
"""

from __future__ import annotations

import struct

import pytest

from app import packet
from app.packet import (
    FH6_FORMAT,
    FH6_PACKET_SIZE,
    FIELD_NAMES,
    FIELD_OFFSETS,
    PacketLengthError,
    parse,
    pack,
)


def test_packet_size_is_324():
    assert FH6_PACKET_SIZE == 324
    assert struct.calcsize(FH6_FORMAT) == 324


def test_format_is_little_endian():
    assert FH6_FORMAT.startswith("<")


def test_all_fields_have_offsets_and_are_ordered():
    # Offsets must be strictly increasing in field order.
    prev = -1
    for name in FIELD_NAMES:
        off = FIELD_OFFSETS[name]
        assert off > prev, f"{name} offset {off} not increasing"
        prev = off


def test_known_sled_offsets():
    # Anchor offsets from the standard Forza "sled" layout (shared with FH6).
    expected = {
        "IsRaceOn": 0,
        "TimestampMS": 4,
        "EngineMaxRpm": 8,
        "EngineIdleRpm": 12,
        "CurrentEngineRpm": 16,
        "AccelerationX": 20,
        "Yaw": 56,
        "NormalizedSuspensionTravelFrontLeft": 68,
        "TireSlipRatioFrontLeft": 84,
        "WheelOnRumbleStripFrontLeft": 116,
        "TireCombinedSlipFrontLeft": 180,
        "SuspensionTravelMetersFrontLeft": 196,
        "CarOrdinal": 212,
        "CarClass": 216,
        "CarPerformanceIndex": 220,
        "DrivetrainType": 224,
        "NumCylinders": 228,
    }
    for name, off in expected.items():
        assert FIELD_OFFSETS[name] == off, f"{name} should be at {off}"


def test_fh6_inserts_cargroup_smashables_after_numcylinders():
    # FH6-specific: CarGroup, SmashableVelDiff, SmashableMass are inserted
    # immediately after NumCylinders (offset 228, size 4 -> ends at 232).
    assert FIELD_OFFSETS["NumCylinders"] == 228
    assert FIELD_OFFSETS["CarGroup"] == 232
    assert FIELD_OFFSETS["SmashableVelDiff"] == 236
    assert FIELD_OFFSETS["SmashableMass"] == 240
    # ...and PositionX comes *after* the 13-byte FH6 block (incl. reserved byte).
    assert FIELD_OFFSETS["PositionX"] == 245


def test_fh6_omits_tirewear_and_trackordinal():
    # FH6 must NOT contain these FH4/FH5 fields.
    assert "TireWearFrontLeft" not in FIELD_NAMES
    assert "TireWearFrontRight" not in FIELD_NAMES
    assert "TireWearRearLeft" not in FIELD_NAMES
    assert "TireWearRearRight" not in FIELD_NAMES
    assert "TrackOrdinal" not in FIELD_NAMES


def test_dash_tail_offsets():
    expected = {
        "PositionX": 245,
        "Speed": 257,
        "Power": 261,
        "Torque": 265,
        "Boost": 285,
        "BestLap": 297,
        "LapNumber": 313,
        "Accel": 316,
        "Brake": 317,
        "Gear": 320,
        "Steer": 321,
        "NormalizedDrivingLine": 322,
        "NormalizedAIBrakeDifference": 323,
    }
    for name, off in expected.items():
        assert FIELD_OFFSETS[name] == off, f"{name} should be at {off}"
    # Last byte must be within the packet.
    assert FIELD_OFFSETS["NormalizedAIBrakeDifference"] == FH6_PACKET_SIZE - 1


def test_parse_rejects_wrong_length():
    with pytest.raises(PacketLengthError):
        parse(b"\x00" * 323)
    with pytest.raises(PacketLengthError):
        parse(b"\x00" * 325)
    with pytest.raises(PacketLengthError):
        parse(b"")


def test_parse_zeroed_packet():
    frame = parse(b"\x00" * FH6_PACKET_SIZE)
    assert frame.IsRaceOn == 0
    assert frame.Speed == 0.0
    assert frame.Gear == 0


def test_roundtrip_known_values():
    values = {name: 0 for name in FIELD_NAMES}
    values.update({
        "IsRaceOn": 1,
        "TimestampMS": 123456,
        "EngineMaxRpm": 7800.0,
        "CurrentEngineRpm": 6543.0,
        "Speed": 55.5,          # m/s
        "Power": 250000.0,      # watts
        "Boost": 14.5,          # psi
        "CarOrdinal": 2145,
        "CarClass": 5,
        "CarPerformanceIndex": 798,
        "DrivetrainType": 1,
        "NumCylinders": 8,
        "CarGroup": 3,
        "SmashableVelDiff": 1.25,
        "SmashableMass": 900.0,
        "Gear": 4,
        "Accel": 255,
        "Brake": 0,
        "Steer": -50,
        "BestLap": 92.345,
        "LapNumber": 3,
    })
    data = pack(values)
    assert len(data) == FH6_PACKET_SIZE
    frame = parse(data)
    assert frame.IsRaceOn == 1
    assert frame.TimestampMS == 123456
    assert abs(frame.EngineMaxRpm - 7800.0) < 1e-3
    assert abs(frame.Speed - 55.5) < 1e-3
    assert frame.CarOrdinal == 2145
    assert frame.NumCylinders == 8
    assert frame.CarGroup == 3
    assert abs(frame.SmashableVelDiff - 1.25) < 1e-4
    assert abs(frame.SmashableMass - 900.0) < 1e-2
    assert frame.Gear == 4
    assert frame.Steer == -50
    assert frame.LapNumber == 3


def test_little_endian_decoding_explicit():
    # IsRaceOn is the first int32; 0x01000000 little-endian bytes -> value 1.
    data = bytearray(FH6_PACKET_SIZE)
    data[0:4] = (1).to_bytes(4, "little")
    frame = parse(bytes(data))
    assert frame.IsRaceOn == 1
    # And confirm big-endian would have been wrong (sanity of the LE choice).
    assert int.from_bytes(data[0:4], "big") != 1


def test_convenience_conversions():
    values = {name: 0 for name in FIELD_NAMES}
    values.update({
        "Speed": 50.0,          # m/s -> 180 km/h
        "EngineMaxRpm": 8000.0,
        "CurrentEngineRpm": 4000.0,
        "Accel": 255,
        "Brake": 0,
        "Boost": 10.0,          # psi
        "Power": 100000.0,      # 100 kW
        "Gear": 0,
        "DrivetrainType": 2,
    })
    frame = parse(pack(values))
    assert abs(frame.speed_kmh - 180.0) < 1e-3
    assert abs(frame.rpm_pct - 50.0) < 1e-3
    assert abs(frame.throttle_pct - 100.0) < 1e-3
    assert abs(frame.power_kw - 100.0) < 1e-3
    assert abs(frame.boost_psi - 10.0) < 1e-3
    assert abs(frame.boost_bar - 0.689475729) < 1e-4
    assert frame.gear_label == "R"
    assert frame.drivetrain == "AWD"


def test_field_debug_reports_offsets_for_bad_length():
    rows = packet.field_debug(b"\x00" * 100)
    assert rows  # still reports the field table
    assert rows[0]["name"] == "IsRaceOn"
    assert rows[0]["offset"] == 0
    assert rows[0]["value"] is None  # no value decoded for wrong length


def test_live_payload_shape():
    frame = parse(pack({name: 0 for name in FIELD_NAMES}))
    payload = frame.live_payload()
    for key in ("speed_kmh", "gear", "rpm", "throttle", "brake",
                "tire_temp", "slip_ratio", "combined_slip", "susp_norm"):
        assert key in payload
    assert len(payload["tire_temp"]) == 4
    assert len(payload["susp_norm"]) == 4
