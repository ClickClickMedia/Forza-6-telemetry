"""Asyncio UDP receiver for FH6 Data Out packets.

Uses ``loop.create_datagram_endpoint`` with a :class:`asyncio.DatagramProtocol`.
Every datagram is length-validated (exactly 324 bytes) and parsed; malformed
packets are counted and dropped without ever raising out of the protocol, so a
bad packet can never take down the listener.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Tuple

from .packet import FH6_PACKET_SIZE, PacketLengthError, parse
from .telemetry_hub import TelemetryHub

log = logging.getLogger(__name__)


class ForzaUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, hub: TelemetryHub):
        self.hub = hub
        self.transport: Optional[asyncio.transports.DatagramTransport] = None

    def connection_made(self, transport: asyncio.transports.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("sockname")
        log.info("UDP listener bound", extra={"extra": {"sockname": sock}})

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        t_mono = time.monotonic()
        nbytes = len(data)
        if nbytes != FH6_PACKET_SIZE:
            self.hub.ingest_invalid()
            # Log sparingly: only note the first few odd sizes to aid setup.
            if self.hub.stats.total_invalid <= 5:
                log.warning(
                    "dropping non-FH6 packet",
                    extra={"extra": {"bytes": nbytes,
                                     "expected": FH6_PACKET_SIZE,
                                     "from": f"{addr[0]}:{addr[1]}"}},
                )
            return
        try:
            frame = parse(data)
        except (PacketLengthError, Exception):  # noqa: BLE001 - never crash
            self.hub.ingest_invalid()
            log.debug("failed to parse packet", exc_info=True)
            return
        self.hub.ingest_valid(frame, data, t_mono)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        # UDP is connectionless; ICMP errors surface here. Log and continue.
        log.warning("UDP error_received", extra={"extra": {"error": str(exc)}})

    def connection_lost(self, exc: Optional[Exception]) -> None:  # pragma: no cover
        if exc:
            log.warning("UDP connection_lost", extra={"extra": {"error": str(exc)}})


class UDPReceiver:
    """Manages the lifecycle of the datagram endpoint."""

    def __init__(self, hub: TelemetryHub, host: str, port: int):
        self.hub = hub
        self.host = host
        self.port = port
        self._transport: Optional[asyncio.transports.DatagramTransport] = None
        self._protocol: Optional[ForzaUDPProtocol] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: ForzaUDPProtocol(self.hub),
            local_addr=(self.host, self.port),
            allow_broadcast=True,
        )
        log.info(
            "UDP receiver started",
            extra={"extra": {"host": self.host, "port": self.port}},
        )

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            log.info("UDP receiver stopped")
