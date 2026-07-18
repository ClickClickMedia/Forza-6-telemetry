"""Forza Horizon 6 "Data Out" packet specification and parser.

Reference
---------
Forza's UDP "Data Out" feature streams a fixed-size C struct once per game
frame. The struct is packed (no compiler alignment padding) and every scalar is
**little-endian** on all shipping platforms (Xbox and PC both emit LE regardless
of host endianness, because the title serialises explicitly).

Packet families and their exact sizes:

    * Forza Motorsport 7 "Sled"            : 232 bytes
    * Forza Motorsport 7 "Dash" (V2)       : 311 bytes
    * Forza Motorsport (2023) "Dash"       : 331 bytes  (adds TireWear x4 + TrackOrdinal)
    * Forza Horizon 4 / 5 / 6 "Dash"       : 324 bytes  (this module)

**Forza Horizon 6 uses the exact FH4/FH5 "Horizon" layout** — this was
confirmed empirically against live FH6 captures (2026-07-18) rather than
assumed. The validation method, which anyone can reproduce from the `/debug`
page: the sled's ``VelocityX/Y/Z`` vector magnitude must equal the dash tail's
``Speed`` field. Under this layout they matched to 3 decimal places on every
captured frame (e.g. 37.822 vs 37.82 m/s), tyre temps decoded to the
plausible 180–230 °F hot-lap band, ``DistanceTraveled`` and
``CurrentRaceTime`` advanced monotonically, and the ``Accel`` byte tracked
real throttle application. A one-byte misplacement of any tail field breaks
all of those simultaneously (a little-endian float read one byte off puts a
foreign byte in the exponent), so this cross-check pins the layout hard.

The Horizon layout relative to FM7 "Dash":

    * The packet is exactly **324 bytes**.
    * Horizon titles **insert 12 bytes** between ``NumCylinders`` and
      ``PositionX`` (offsets 232..243). Per the official FH6 Data Out
      documentation these are ``CarGroup`` (stable per-car category int,
      s32), ``SmashableVelDiff`` and ``SmashableMass`` (f32 each — impact
      metadata, observed 0.0 outside collisions). Note: v1.0.x of this
      project had these *names* right but the *geometry* wrong (a 13-byte
      block with a pad byte before ``PositionX``) — the one-byte error that
      corrupted the whole dash tail.
    * The dash tail (``PositionX`` .. ``NormalizedAIBrakeDifference``) is the
      FM7 "Dash" tail, verbatim, at offsets 244..322.
    * One final byte (offset 323) closes the packet; it has always read 0 and
      is exposed as ``Unknown3`` (u8) so community captures can inspect it.
    * There is **no** ``TireWear`` and **no** ``TrackOrdinal`` — those are
      Forza Motorsport (2023) fields, absent from all Horizon packets.

Units on the wire: ``Speed`` m/s · ``Power`` watts · ``Torque`` N·m ·
``TireTemp*`` **degrees Fahrenheit** · ``Boost`` PSI · lap/race times seconds.

Everything below is derived from the struct layout, then validated at import
time by asserting ``struct.calcsize(FH6_FORMAT) == FH6_PACKET_SIZE``. The unit
tests additionally assert the byte offset of every named field so that any
accidental reordering is caught.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, fields as dataclass_fields
from typing import Any, Dict, List, Tuple

#: Exact wire size of an FH6 Data Out packet. Packets of any other length are
#: rejected by the receiver.
FH6_PACKET_SIZE = 324

#: Sanity ceiling for lap-time fields (seconds). In free roam FH6 has been
#: observed emitting garbage/sentinel values in ``BestLap``/``LastLap``
#: (e.g. 388328.3 s ≈ 4.5 days in real captures); anything at or above this
#: bound is treated as "no lap time" by every consumer.
SANE_LAP_MAX_S = 10_800.0


def sane_lap(seconds: float) -> float:
    """Return ``seconds`` if it is a plausible lap time, else 0.0."""
    if seconds and 0.0 < seconds < SANE_LAP_MAX_S:
        return seconds
    return 0.0

# ---------------------------------------------------------------------------
# Field table
# ---------------------------------------------------------------------------
# Each entry is (name, struct_code). ``struct_code`` uses the standard library
# ``struct`` codes:
#   i = int32 (signed)   I = uint32   f = float32
#   H = uint16           B = uint8    b = int8
#   x = one pad/reserved byte (consumes a byte, yields no value)
#
# The order below IS the wire order. Do not reorder without updating the
# offset tests.
_FIELD_TABLE: List[Tuple[str, str]] = [
    # -- Sled (232 bytes) ---------------------------------------------------
    ("IsRaceOn", "i"),
    ("TimestampMS", "I"),
    ("EngineMaxRpm", "f"),
    ("EngineIdleRpm", "f"),
    ("CurrentEngineRpm", "f"),
    ("AccelerationX", "f"),
    ("AccelerationY", "f"),
    ("AccelerationZ", "f"),
    ("VelocityX", "f"),
    ("VelocityY", "f"),
    ("VelocityZ", "f"),
    ("AngularVelocityX", "f"),
    ("AngularVelocityY", "f"),
    ("AngularVelocityZ", "f"),
    ("Yaw", "f"),
    ("Pitch", "f"),
    ("Roll", "f"),
    ("NormalizedSuspensionTravelFrontLeft", "f"),
    ("NormalizedSuspensionTravelFrontRight", "f"),
    ("NormalizedSuspensionTravelRearLeft", "f"),
    ("NormalizedSuspensionTravelRearRight", "f"),
    ("TireSlipRatioFrontLeft", "f"),
    ("TireSlipRatioFrontRight", "f"),
    ("TireSlipRatioRearLeft", "f"),
    ("TireSlipRatioRearRight", "f"),
    ("WheelRotationSpeedFrontLeft", "f"),
    ("WheelRotationSpeedFrontRight", "f"),
    ("WheelRotationSpeedRearLeft", "f"),
    ("WheelRotationSpeedRearRight", "f"),
    ("WheelOnRumbleStripFrontLeft", "i"),
    ("WheelOnRumbleStripFrontRight", "i"),
    ("WheelOnRumbleStripRearLeft", "i"),
    ("WheelOnRumbleStripRearRight", "i"),
    ("WheelInPuddleDepthFrontLeft", "f"),
    ("WheelInPuddleDepthFrontRight", "f"),
    ("WheelInPuddleDepthRearLeft", "f"),
    ("WheelInPuddleDepthRearRight", "f"),
    ("SurfaceRumbleFrontLeft", "f"),
    ("SurfaceRumbleFrontRight", "f"),
    ("SurfaceRumbleRearLeft", "f"),
    ("SurfaceRumbleRearRight", "f"),
    ("TireSlipAngleFrontLeft", "f"),
    ("TireSlipAngleFrontRight", "f"),
    ("TireSlipAngleRearLeft", "f"),
    ("TireSlipAngleRearRight", "f"),
    ("TireCombinedSlipFrontLeft", "f"),
    ("TireCombinedSlipFrontRight", "f"),
    ("TireCombinedSlipRearLeft", "f"),
    ("TireCombinedSlipRearRight", "f"),
    ("SuspensionTravelMetersFrontLeft", "f"),
    ("SuspensionTravelMetersFrontRight", "f"),
    ("SuspensionTravelMetersRearLeft", "f"),
    ("SuspensionTravelMetersRearRight", "f"),
    ("CarOrdinal", "i"),
    ("CarClass", "i"),
    ("CarPerformanceIndex", "i"),
    ("DrivetrainType", "i"),
    ("NumCylinders", "i"),
    # -- Horizon insertion (12 bytes, offsets 232..243) ---------------------
    # Officially documented FH6 fields (absent from Forza Motorsport):
    # CarGroup is a stable per-car category int; the smashable floats carry
    # impact metadata and read 0.0 outside collisions.
    ("CarGroup", "i"),
    ("SmashableVelDiff", "f"),
    ("SmashableMass", "f"),
    # -- Dash tail (79 bytes, offsets 244..322) -----------------------------
    ("PositionX", "f"),
    ("PositionY", "f"),
    ("PositionZ", "f"),
    ("Speed", "f"),           # metres/second
    ("Power", "f"),           # watts
    ("Torque", "f"),          # newton-metres
    ("TireTempFrontLeft", "f"),
    ("TireTempFrontRight", "f"),
    ("TireTempRearLeft", "f"),
    ("TireTempRearRight", "f"),
    ("Boost", "f"),
    ("Fuel", "f"),
    ("DistanceTraveled", "f"),
    ("BestLap", "f"),         # seconds
    ("LastLap", "f"),         # seconds
    ("CurrentLap", "f"),      # seconds
    ("CurrentRaceTime", "f"), # seconds
    ("LapNumber", "H"),
    ("RacePosition", "B"),
    ("Accel", "B"),           # 0..255
    ("Brake", "B"),           # 0..255
    ("Clutch", "B"),          # 0..255
    ("HandBrake", "B"),       # 0..255
    ("Gear", "B"),            # 0 = reverse, 1..10 forward
    ("Steer", "b"),           # -127..127
    ("NormalizedDrivingLine", "b"),
    ("NormalizedAIBrakeDifference", "b"),
    # -- Final byte (offset 323) --------------------------------------------
    # Always observed 0; captured so the full packet round-trips.
    ("Unknown3", "B"),
]

# Build the struct format string (little-endian, packed).
FH6_FORMAT = "<" + "".join(code for _name, code in _FIELD_TABLE)

# Names of fields that actually decode to a value (pad bytes excluded), in order.
FIELD_NAMES: List[str] = [name for name, code in _FIELD_TABLE if code != "x"]

_STRUCT = struct.Struct(FH6_FORMAT)

# Fail fast at import time if the layout ever drifts from the documented size.
assert _STRUCT.size == FH6_PACKET_SIZE, (
    f"FH6 struct layout is {_STRUCT.size} bytes but the spec requires "
    f"{FH6_PACKET_SIZE}. The field table is wrong."
)


def _compute_offsets() -> Dict[str, int]:
    """Return the byte offset of every value-bearing field.

    Computed by walking the field table and summing struct code sizes, so the
    offsets always match ``FH6_FORMAT`` exactly.
    """
    offsets: Dict[str, int] = {}
    cursor = 0
    for name, code in _FIELD_TABLE:
        size = struct.calcsize("<" + code)
        if code != "x":
            offsets[name] = cursor
        cursor += size
    return offsets


#: Byte offset of every named field. Used by tests and the debug page.
FIELD_OFFSETS: Dict[str, int] = _compute_offsets()


# ---------------------------------------------------------------------------
# Parsed representation
# ---------------------------------------------------------------------------
# Drivetrain enum per Forza Data Out.
DRIVETRAIN = {0: "FWD", 1: "RWD", 2: "AWD"}


@dataclass(slots=True)
class TelemetryFrame:
    """A fully parsed FH6 frame plus a handful of convenience conversions.

    The raw wire fields are stored verbatim (SI-ish units as Forza emits them);
    the ``@property`` helpers expose the human-friendly units the dashboard and
    analysis code use, so unit conversions live in exactly one place.
    """

    # Raw wire fields (populated dynamically in ``parse``); declared here for
    # editor/static-analysis friendliness. Defaults keep the dataclass usable
    # even if a field is ever missing.
    IsRaceOn: int = 0
    TimestampMS: int = 0
    EngineMaxRpm: float = 0.0
    EngineIdleRpm: float = 0.0
    CurrentEngineRpm: float = 0.0
    AccelerationX: float = 0.0
    AccelerationY: float = 0.0
    AccelerationZ: float = 0.0
    VelocityX: float = 0.0
    VelocityY: float = 0.0
    VelocityZ: float = 0.0
    AngularVelocityX: float = 0.0
    AngularVelocityY: float = 0.0
    AngularVelocityZ: float = 0.0
    Yaw: float = 0.0
    Pitch: float = 0.0
    Roll: float = 0.0
    NormalizedSuspensionTravelFrontLeft: float = 0.0
    NormalizedSuspensionTravelFrontRight: float = 0.0
    NormalizedSuspensionTravelRearLeft: float = 0.0
    NormalizedSuspensionTravelRearRight: float = 0.0
    TireSlipRatioFrontLeft: float = 0.0
    TireSlipRatioFrontRight: float = 0.0
    TireSlipRatioRearLeft: float = 0.0
    TireSlipRatioRearRight: float = 0.0
    WheelRotationSpeedFrontLeft: float = 0.0
    WheelRotationSpeedFrontRight: float = 0.0
    WheelRotationSpeedRearLeft: float = 0.0
    WheelRotationSpeedRearRight: float = 0.0
    WheelOnRumbleStripFrontLeft: int = 0
    WheelOnRumbleStripFrontRight: int = 0
    WheelOnRumbleStripRearLeft: int = 0
    WheelOnRumbleStripRearRight: int = 0
    WheelInPuddleDepthFrontLeft: float = 0.0
    WheelInPuddleDepthFrontRight: float = 0.0
    WheelInPuddleDepthRearLeft: float = 0.0
    WheelInPuddleDepthRearRight: float = 0.0
    SurfaceRumbleFrontLeft: float = 0.0
    SurfaceRumbleFrontRight: float = 0.0
    SurfaceRumbleRearLeft: float = 0.0
    SurfaceRumbleRearRight: float = 0.0
    TireSlipAngleFrontLeft: float = 0.0
    TireSlipAngleFrontRight: float = 0.0
    TireSlipAngleRearLeft: float = 0.0
    TireSlipAngleRearRight: float = 0.0
    TireCombinedSlipFrontLeft: float = 0.0
    TireCombinedSlipFrontRight: float = 0.0
    TireCombinedSlipRearLeft: float = 0.0
    TireCombinedSlipRearRight: float = 0.0
    SuspensionTravelMetersFrontLeft: float = 0.0
    SuspensionTravelMetersFrontRight: float = 0.0
    SuspensionTravelMetersRearLeft: float = 0.0
    SuspensionTravelMetersRearRight: float = 0.0
    CarOrdinal: int = 0
    CarClass: int = 0
    CarPerformanceIndex: int = 0
    DrivetrainType: int = 0
    NumCylinders: int = 0
    CarGroup: int = 0
    SmashableVelDiff: float = 0.0
    SmashableMass: float = 0.0
    PositionX: float = 0.0
    PositionY: float = 0.0
    PositionZ: float = 0.0
    Speed: float = 0.0
    Power: float = 0.0
    Torque: float = 0.0
    TireTempFrontLeft: float = 0.0
    TireTempFrontRight: float = 0.0
    TireTempRearLeft: float = 0.0
    TireTempRearRight: float = 0.0
    Boost: float = 0.0
    Fuel: float = 0.0
    DistanceTraveled: float = 0.0
    BestLap: float = 0.0
    LastLap: float = 0.0
    CurrentLap: float = 0.0
    CurrentRaceTime: float = 0.0
    LapNumber: int = 0
    RacePosition: int = 0
    Accel: int = 0
    Brake: int = 0
    Clutch: int = 0
    HandBrake: int = 0
    Gear: int = 0
    Steer: int = 0
    NormalizedDrivingLine: int = 0
    NormalizedAIBrakeDifference: int = 0
    Unknown3: int = 0

    # -- Convenience conversions (not on the wire) --------------------------
    @property
    def speed_kmh(self) -> float:
        return self.Speed * 3.6

    @property
    def speed_mph(self) -> float:
        return self.Speed * 2.2369362920544

    @property
    def power_kw(self) -> float:
        return self.Power / 1000.0

    @property
    def torque_nm(self) -> float:
        return self.Torque

    @property
    def boost_bar(self) -> float:
        # Forza reports boost in PSI; convert to bar for the metric readout.
        return self.Boost * 0.0689475729

    @property
    def boost_psi(self) -> float:
        return self.Boost

    @property
    def rpm(self) -> float:
        return self.CurrentEngineRpm

    @property
    def rpm_pct(self) -> float:
        if self.EngineMaxRpm <= 0:
            return 0.0
        return max(0.0, min(1.0, self.CurrentEngineRpm / self.EngineMaxRpm)) * 100.0

    @property
    def throttle_pct(self) -> float:
        return self.Accel / 255.0 * 100.0

    @property
    def brake_pct(self) -> float:
        return self.Brake / 255.0 * 100.0

    @property
    def clutch_pct(self) -> float:
        return self.Clutch / 255.0 * 100.0

    @property
    def handbrake_pct(self) -> float:
        return self.HandBrake / 255.0 * 100.0

    @property
    def steer_norm(self) -> float:
        """Steering in [-1, 1]."""
        return max(-1.0, min(1.0, self.Steer / 127.0))

    @property
    def gear_label(self) -> str:
        if self.Gear == 0:
            return "R"
        if self.Gear >= 11:  # neutral is sometimes emitted as an out-of-range gear
            return "N"
        return str(self.Gear)

    @property
    def tire_temps_f(self) -> "List[float]":
        """Tyre temps [FL, FR, RL, RR] in Fahrenheit, exactly as on the wire."""
        return [
            self.TireTempFrontLeft,
            self.TireTempFrontRight,
            self.TireTempRearLeft,
            self.TireTempRearRight,
        ]

    @property
    def tire_temps_c(self) -> "List[float]":
        """Tyre temps [FL, FR, RL, RR] converted to Celsius.

        Forza emits Fahrenheit; the dashboard's default display is Celsius, so
        the conversion lives here in the one place units are defined.
        """
        return [(t - 32.0) * 5.0 / 9.0 for t in self.tire_temps_f]

    @property
    def drivetrain(self) -> str:
        return DRIVETRAIN.get(self.DrivetrainType, "?")

    def as_dict(self) -> Dict[str, Any]:
        """Return every raw wire field as a plain dict (no conversions)."""
        return {name: getattr(self, name) for name in FIELD_NAMES}

    def live_payload(self) -> Dict[str, Any]:
        """Compact JSON payload pushed to the live dashboard.

        Uses converted/human units and rounds aggressively to keep the socket
        frames small. Four-wheel arrays are ordered [FL, FR, RL, RR].
        """
        return {
            "race_on": int(self.IsRaceOn),
            "timestamp_ms": int(self.TimestampMS),
            "speed_kmh": round(self.speed_kmh, 1),
            "gear": self.gear_label,
            "gear_num": int(self.Gear),
            "rpm": round(self.rpm, 0),
            "rpm_max": round(self.EngineMaxRpm, 0),
            "rpm_pct": round(self.rpm_pct, 1),
            "throttle": round(self.throttle_pct, 1),
            "brake": round(self.brake_pct, 1),
            "clutch": round(self.clutch_pct, 1),
            "handbrake": round(self.handbrake_pct, 1),
            "steer": round(self.steer_norm, 3),
            "power_kw": round(self.power_kw, 1),
            "torque_nm": round(self.torque_nm, 1),
            "boost_bar": round(self.boost_bar, 2),
            "boost_psi": round(self.boost_psi, 2),
            "cur_lap": round(sane_lap(self.CurrentLap), 3),
            "last_lap": round(sane_lap(self.LastLap), 3),
            "best_lap": round(sane_lap(self.BestLap), 3),
            "lap_number": int(self.LapNumber),
            "race_position": int(self.RacePosition),
            "race_time": round(self.CurrentRaceTime, 3),
            "dist_m": round(self.DistanceTraveled, 1),
            "fuel": round(self.Fuel, 4),
            "pos": [round(self.PositionX, 1), round(self.PositionZ, 1)],
            "accel_long": round(self.AccelerationZ, 3),
            "accel_lat": round(self.AccelerationX, 3),
            "yaw_rate": round(self.AngularVelocityY, 4),
            "tire_temp_f": [round(t, 1) for t in self.tire_temps_f],
            "tire_temp_c": [round(t, 1) for t in self.tire_temps_c],
            "slip_ratio": [
                round(self.TireSlipRatioFrontLeft, 3),
                round(self.TireSlipRatioFrontRight, 3),
                round(self.TireSlipRatioRearLeft, 3),
                round(self.TireSlipRatioRearRight, 3),
            ],
            "slip_angle": [
                round(self.TireSlipAngleFrontLeft, 3),
                round(self.TireSlipAngleFrontRight, 3),
                round(self.TireSlipAngleRearLeft, 3),
                round(self.TireSlipAngleRearRight, 3),
            ],
            "combined_slip": [
                round(self.TireCombinedSlipFrontLeft, 3),
                round(self.TireCombinedSlipFrontRight, 3),
                round(self.TireCombinedSlipRearLeft, 3),
                round(self.TireCombinedSlipRearRight, 3),
            ],
            "susp_norm": [
                round(self.NormalizedSuspensionTravelFrontLeft, 3),
                round(self.NormalizedSuspensionTravelFrontRight, 3),
                round(self.NormalizedSuspensionTravelRearLeft, 3),
                round(self.NormalizedSuspensionTravelRearRight, 3),
            ],
            "car_ordinal": int(self.CarOrdinal),
            "car_class": int(self.CarClass),
            "pi": int(self.CarPerformanceIndex),
            "drivetrain": self.drivetrain,
            "cylinders": int(self.NumCylinders),
            "car_group": int(self.CarGroup),
        }


class PacketLengthError(ValueError):
    """Raised when a datagram is not exactly ``FH6_PACKET_SIZE`` bytes."""


def parse(data: bytes) -> TelemetryFrame:
    """Parse a raw FH6 datagram into a :class:`TelemetryFrame`.

    Raises
    ------
    PacketLengthError
        If ``data`` is not exactly 324 bytes.
    struct.error
        If the bytes cannot be unpacked (should not happen once the length
        check passes, but is surfaced rather than swallowed).
    """
    if len(data) != FH6_PACKET_SIZE:
        raise PacketLengthError(
            f"expected {FH6_PACKET_SIZE} bytes, got {len(data)}"
        )
    values = _STRUCT.unpack(data)
    return TelemetryFrame(**dict(zip(FIELD_NAMES, values)))


def pack(values: Dict[str, Any]) -> bytes:
    """Build a 324-byte FH6 datagram from a mapping of field name -> value.

    Missing fields default to 0. Used by the synthetic generator and by
    round-trip unit tests. Reserved/pad bytes are emitted automatically.
    """
    ordered = [values.get(name, 0) for name in FIELD_NAMES]
    data = _STRUCT.pack(*ordered)
    assert len(data) == FH6_PACKET_SIZE
    return data


def field_debug(data: bytes) -> List[Dict[str, Any]]:
    """Return a per-field breakdown (name, offset, raw code, value).

    Used by the packet-debug page. Tolerant: if the packet is the wrong length
    it still reports what it can so the operator can see *why* it was rejected.
    """
    rows: List[Dict[str, Any]] = []
    ok = len(data) == FH6_PACKET_SIZE
    values: Dict[str, Any] = {}
    if ok:
        values = dict(zip(FIELD_NAMES, _STRUCT.unpack(data)))
    for name, code in _FIELD_TABLE:
        if code == "x":
            continue
        rows.append(
            {
                "name": name,
                "offset": FIELD_OFFSETS[name],
                "type": code,
                "value": values.get(name) if ok else None,
            }
        )
    return rows


# Sanity: the declared dataclass must expose every value-bearing wire field.
_declared = {f.name for f in dataclass_fields(TelemetryFrame)}
_missing = [n for n in FIELD_NAMES if n not in _declared]
assert not _missing, f"TelemetryFrame is missing wire fields: {_missing}"
