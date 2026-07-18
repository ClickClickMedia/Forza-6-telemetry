"""Synthetic FH6 telemetry generator.

Simulates a car lapping a simple oval so the whole pipeline (UDP -> parse ->
hub -> WebSocket -> dashboard, plus recording and analysis) can be exercised
without an Xbox. It emits **real 324-byte FH6 packets** to the configured UDP
port, so it tests the actual receive/parse path rather than bypassing it.

Enable at runtime with ``FH6_SYNTHETIC=1`` or run standalone::

    python -m app.synthetic --host 127.0.0.1 --port 9876 --hz 60
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import socket
import time
from typing import Dict

from .packet import pack

log = logging.getLogger(__name__)


class SyntheticDriver:
    """Deterministic-ish physical model of a car on a 1 km oval.

    Not a real vehicle sim - just enough dynamics to make every dashboard and
    analysis field move plausibly: throttle/brake cycles, cornering lateral g,
    gear changes, tyre slip in corners, suspension travel over kerbs, etc.
    """

    TRACK_LENGTH = 1000.0  # metres per lap
    MAX_RPM = 7800.0
    IDLE_RPM = 850.0

    def __init__(self, hz: float = 60.0):
        self.hz = hz
        self.dt = 1.0 / hz
        self.t = 0.0
        self.dist = 0.0
        self.speed = 10.0  # m/s
        self.lap = 0
        self.lap_start_t = 0.0
        self.best_lap = 0.0
        self.last_lap = 0.0
        self.gear = 2
        self.x = 0.0
        self.z = 0.0
        self.heading = 0.0

    def _target_speed(self, s: float) -> float:
        """Target speed as a function of distance along the lap.

        Two straights (fast) and two hairpins (slow), so speed, braking and
        lateral g all cycle within a lap.
        """
        frac = (s % self.TRACK_LENGTH) / self.TRACK_LENGTH
        # Corners near frac 0.25 and 0.75.
        corner = math.cos(frac * 4 * math.pi)  # -1..1, dips at corners
        base = 30.0 + 25.0 * (corner * 0.5 + 0.5)  # 30..55 m/s
        return base

    def _curvature(self, s: float) -> float:
        frac = (s % self.TRACK_LENGTH) / self.TRACK_LENGTH
        # Curvature peaks in the corners.
        return 0.02 * max(0.0, -math.cos(frac * 4 * math.pi))

    def step(self) -> Dict[str, float]:
        dt = self.dt
        target = self._target_speed(self.dist)
        err = target - self.speed

        throttle = 0.0
        brake = 0.0
        if err > 0:
            throttle = min(1.0, err / 10.0)
            accel = throttle * 8.0
        else:
            brake = min(1.0, -err / 8.0)
            accel = -brake * 12.0
        self.speed = max(3.0, self.speed + accel * dt)

        prev_dist = self.dist
        self.dist += self.speed * dt

        # Lap detection.
        if int(self.dist / self.TRACK_LENGTH) > self.lap:
            self.lap = int(self.dist / self.TRACK_LENGTH)
            self.last_lap = self.t - self.lap_start_t
            if self.best_lap == 0.0 or self.last_lap < self.best_lap:
                self.best_lap = self.last_lap
            self.lap_start_t = self.t

        # Position via simple heading integration around curvature.
        kappa = self._curvature(self.dist)
        self.heading += kappa * self.speed * dt
        self.x += math.cos(self.heading) * self.speed * dt
        self.z += math.sin(self.heading) * self.speed * dt

        # Lateral acceleration from curvature (a = v^2 * kappa).
        lat_g = (self.speed ** 2) * kappa / 9.81
        long_g = accel / 9.81

        # Gear / RPM model.
        ratio = [0, 12.0, 8.5, 6.2, 4.8, 3.9, 3.2][min(self.gear, 6)]
        rpm = self.IDLE_RPM + self.speed * ratio * 60.0
        if rpm > self.MAX_RPM * 0.95 and self.gear < 6:
            self.gear += 1
        elif rpm < self.MAX_RPM * 0.35 and self.gear > 1:
            self.gear -= 1
        rpm = min(rpm, self.MAX_RPM)

        # Tyre slip: rises in corners and under hard throttle/brake.
        base_slip = abs(lat_g) * 0.4
        rear_extra = throttle * 0.6
        front_extra = brake * 0.5
        slip_fl = base_slip + front_extra * 0.5
        slip_fr = base_slip + front_extra * 0.5
        slip_rl = base_slip + rear_extra
        slip_rr = base_slip + rear_extra

        # Suspension: normalised travel around 0.5, with kerb bumps.
        bump = 0.15 * math.sin(self.dist * 0.7)
        susp = 0.5 + long_g * 0.1 + bump

        steer = max(-1.0, min(1.0, kappa * 300.0 * (1 if self.heading >= 0 else 1)))

        power = self.speed * 900.0 * (0.4 + throttle * 0.6)  # watts
        torque = power / max(1.0, (rpm * 2 * math.pi / 60.0))
        boost_psi = throttle * 14.0

        self.t += dt

        return {
            "IsRaceOn": 1,
            "TimestampMS": int(self.t * 1000) & 0xFFFFFFFF,
            "EngineMaxRpm": self.MAX_RPM,
            "EngineIdleRpm": self.IDLE_RPM,
            "CurrentEngineRpm": rpm,
            "AccelerationX": lat_g * 9.81,
            "AccelerationY": 0.2,
            "AccelerationZ": long_g * 9.81,
            "VelocityX": self.speed * math.cos(self.heading),
            "VelocityY": 0.0,
            "VelocityZ": self.speed * math.sin(self.heading),
            "AngularVelocityX": 0.0,
            "AngularVelocityY": kappa * self.speed,
            "AngularVelocityZ": 0.0,
            "Yaw": self.heading,
            "Pitch": long_g * 0.05,
            "Roll": lat_g * 0.05,
            "NormalizedSuspensionTravelFrontLeft": max(0.0, min(1.0, susp)),
            "NormalizedSuspensionTravelFrontRight": max(0.0, min(1.0, susp + 0.02)),
            "NormalizedSuspensionTravelRearLeft": max(0.0, min(1.0, susp - 0.02)),
            "NormalizedSuspensionTravelRearRight": max(0.0, min(1.0, susp)),
            "TireSlipRatioFrontLeft": slip_fl,
            "TireSlipRatioFrontRight": slip_fr,
            "TireSlipRatioRearLeft": slip_rl,
            "TireSlipRatioRearRight": slip_rr,
            "WheelRotationSpeedFrontLeft": self.speed * 3.0,
            "WheelRotationSpeedFrontRight": self.speed * 3.0,
            "WheelRotationSpeedRearLeft": self.speed * 3.0 * (1 + throttle * 0.1),
            "WheelRotationSpeedRearRight": self.speed * 3.0 * (1 + throttle * 0.1),
            "TireSlipAngleFrontLeft": steer * 0.3 + base_slip,
            "TireSlipAngleFrontRight": steer * 0.3 + base_slip,
            "TireSlipAngleRearLeft": base_slip * 0.8,
            "TireSlipAngleRearRight": base_slip * 0.8,
            "TireCombinedSlipFrontLeft": slip_fl + abs(steer) * 0.2,
            "TireCombinedSlipFrontRight": slip_fr + abs(steer) * 0.2,
            "TireCombinedSlipRearLeft": slip_rl,
            "TireCombinedSlipRearRight": slip_rr,
            "SuspensionTravelMetersFrontLeft": (susp - 0.5) * 0.1,
            "SuspensionTravelMetersFrontRight": (susp - 0.5) * 0.1,
            "SuspensionTravelMetersRearLeft": (susp - 0.5) * 0.1,
            "SuspensionTravelMetersRearRight": (susp - 0.5) * 0.1,
            "CarOrdinal": 2145,
            "CarClass": 5,
            "CarPerformanceIndex": 798,
            "DrivetrainType": 1,
            "NumCylinders": 8,
            "CarGroup": 3,
            "Unknown1": 0.0,
            "Unknown2": 0.0,
            "PositionX": self.x,
            "PositionY": 0.0,
            "PositionZ": self.z,
            "Speed": self.speed,
            "Power": power,
            "Torque": torque,
            # Wire unit is Fahrenheit (like the real game): ~franchise-typical
            # 175-230 F operating band, rising with slip.
            "TireTempFrontLeft": 176.0 + slip_fl * 54.0,
            "TireTempFrontRight": 176.0 + slip_fr * 54.0,
            "TireTempRearLeft": 185.0 + slip_rl * 54.0,
            "TireTempRearRight": 185.0 + slip_rr * 54.0,
            "Boost": boost_psi,
            "Fuel": 0.8,
            "DistanceTraveled": self.dist,
            "BestLap": self.best_lap,
            "LastLap": self.last_lap,
            "CurrentLap": self.t - self.lap_start_t,
            "CurrentRaceTime": self.t,
            "LapNumber": self.lap,
            "RacePosition": 1,
            "Accel": int(throttle * 255),
            "Brake": int(brake * 255),
            "Clutch": 0,
            "HandBrake": 0,
            "Gear": self.gear,
            "Steer": int(steer * 127),
            "NormalizedDrivingLine": 0,
            "NormalizedAIBrakeDifference": 0,
        }


async def run_synthetic(host: str, port: int, hz: float, stop_event: asyncio.Event | None = None) -> None:
    """Continuously emit synthetic FH6 packets to ``host:port`` over UDP."""
    driver = SyntheticDriver(hz=hz)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / hz
    log.info("synthetic generator started",
             extra={"extra": {"host": host, "port": port, "hz": hz}})
    try:
        next_t = time.monotonic()
        while stop_event is None or not stop_event.is_set():
            values = driver.step()
            sock.sendto(pack(values), (host, port))
            next_t += interval
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_t = time.monotonic()
    except asyncio.CancelledError:
        pass
    finally:
        sock.close()
        log.info("synthetic generator stopped")


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="FH6 synthetic telemetry generator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9876)
    ap.add_argument("--hz", type=float, default=60.0)
    args = ap.parse_args()
    try:
        asyncio.run(run_synthetic(args.host, args.port, args.hz))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _main()
