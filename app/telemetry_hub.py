"""In-memory telemetry hub: statistics, latest frame, and live broadcast.

The hub sits between the UDP receiver and everything else:

    UDP receiver --> hub.ingest(frame) --> Recorder.feed(...)   (every frame)
                                       \-> latest frame + stats
    push loop (18 Hz) --> broadcast latest frame to WebSocket clients

Broadcasting is decoupled from ingestion: the game emits ~60 fps but browsers
are updated at ``push_hz`` by an independent asyncio task, so phones receive a
steady, bandwidth-friendly stream regardless of the incoming frame rate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Set

from .packet import TelemetryFrame

log = logging.getLogger(__name__)


class Stats:
    """Rolling receiver statistics."""

    def __init__(self) -> None:
        self.total_valid = 0
        self.total_invalid = 0
        self.total_bytes = 0
        self.last_packet_mono: Optional[float] = None
        self.last_packet_wall: Optional[float] = None
        # Timestamps (monotonic) of recent valid packets for a 1s rate window.
        self._recent: Deque[float] = deque(maxlen=512)

    def record_valid(self, now_mono: float, now_wall: float, nbytes: int) -> None:
        self.total_valid += 1
        self.total_bytes += nbytes
        self.last_packet_mono = now_mono
        self.last_packet_wall = now_wall
        self._recent.append(now_mono)

    def record_invalid(self) -> None:
        self.total_invalid += 1

    def packets_per_second(self, now_mono: float) -> float:
        # Count packets within the last 1.0 s.
        cutoff = now_mono - 1.0
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        return float(len(self._recent))

    def seconds_since_last(self, now_mono: float) -> Optional[float]:
        if self.last_packet_mono is None:
            return None
        return now_mono - self.last_packet_mono

    def snapshot(self, now_mono: float) -> Dict[str, Any]:
        return {
            "valid": self.total_valid,
            "invalid": self.total_invalid,
            "bytes": self.total_bytes,
            "pps": round(self.packets_per_second(now_mono), 1),
            "seconds_since_last": (
                round(self.seconds_since_last(now_mono), 2)
                if self.last_packet_mono is not None
                else None
            ),
            "last_packet_wall": self.last_packet_wall,
        }


class TelemetryHub:
    def __init__(self, push_hz: float = 18.0):
        self.stats = Stats()
        self.push_hz = max(1.0, push_hz)
        self._latest: Optional[TelemetryFrame] = None
        self._latest_raw_len: int = 0
        self._latest_raw: Optional[bytes] = None
        self._clients: Set["asyncio.Queue[str]"] = set()
        self._push_task: Optional[asyncio.Task] = None
        self._running = False
        # Optional hook set by the app: called with (frame, t_mono) for recording.
        self.on_frame = None  # type: ignore[assignment]

    # -- Ingestion (called from UDP receiver, sync) ------------------------
    def ingest_valid(
        self, frame: TelemetryFrame, raw: bytes, t_mono: float
    ) -> None:
        now_wall = time.time()
        nbytes = len(raw)
        self.stats.record_valid(t_mono, now_wall, nbytes)
        self._latest = frame
        self._latest_raw_len = nbytes
        self._latest_raw = raw
        if self.on_frame is not None:
            try:
                self.on_frame(frame, t_mono)
            except Exception:  # pragma: no cover
                log.exception("on_frame hook failed")

    def ingest_invalid(self) -> None:
        self.stats.record_invalid()

    @property
    def latest(self) -> Optional[TelemetryFrame]:
        return self._latest

    @property
    def latest_raw(self) -> Optional[bytes]:
        return self._latest_raw

    # -- WebSocket client registry -----------------------------------------
    def register(self) -> "asyncio.Queue[str]":
        q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=4)
        self._clients.add(q)
        return q

    def unregister(self, q: "asyncio.Queue[str]") -> None:
        self._clients.discard(q)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # -- Push loop ---------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._push_task = asyncio.create_task(self._push_loop(), name="push-loop")

    async def stop(self) -> None:
        self._running = False
        if self._push_task is not None:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
            self._push_task = None

    async def _push_loop(self) -> None:
        interval = 1.0 / self.push_hz
        while self._running:
            await asyncio.sleep(interval)
            if not self._clients:
                continue
            frame = self._latest
            now_mono = time.monotonic()
            payload = {
                "type": "telemetry",
                "status": self.status_payload(now_mono),
                "data": frame.live_payload() if frame is not None else None,
            }
            msg = json.dumps(payload, separators=(",", ":"))
            for q in list(self._clients):
                # Drop frames for slow clients rather than blocking the loop.
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass

    def status_payload(self, now_mono: Optional[float] = None) -> Dict[str, Any]:
        if now_mono is None:
            now_mono = time.monotonic()
        snap = self.stats.snapshot(now_mono)
        ssl = snap["seconds_since_last"]
        connected = ssl is not None and ssl < 2.0
        snap["connected"] = connected
        snap["clients"] = self.client_count
        return snap
