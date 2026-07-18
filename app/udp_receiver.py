"""Asyncio UDP receiver for FH6 Data Out packets.

Uses ``loop.create_datagram_endpoint`` with a :class:`asyncio.DatagramProtocol`.
Every datagram is length-validated (exactly 324 bytes) and parsed; malformed
packets are counted and dropped without ever raising out of the protocol, so a
bad packet can never take down the listener.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Optional, Tuple

from .packet import FH6_PACKET_SIZE, PacketLengthError, parse
from .telemetry_hub import TelemetryHub

log = logging.getLogger(__name__)


def parse_forward(value: str) -> Optional[Tuple[str, int]]:
    """Parse an 'ip:port' forward target; None if empty/invalid."""
    value = (value or "").strip()
    if not value:
        return None
    host, sep, port_s = value.rpartition(":")
    if not sep or not host:
        return None
    try:
        port = int(port_s)
    except ValueError:
        return None
    if not (1 <= port <= 65535):
        return None
    return host, port


class ForzaUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, hub: TelemetryHub):
        self.hub = hub
        self.transport: Optional[asyncio.transports.DatagramTransport] = None
        # Optional mirror target: every raw datagram (valid or not) is
        # re-sent verbatim so a second tool (SimHub etc.) can coexist with
        # Forza's single Data Out target. Set via UDPReceiver.set_forward.
        self.forward_addr: Optional[Tuple[str, int]] = None
        self._fwd_sock: Optional[socket.socket] = None
        self.forwarded = 0
        self.forward_errors = 0

    def connection_made(self, transport: asyncio.transports.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("sockname")
        log.info("UDP listener bound", extra={"extra": {"sockname": sock}})

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        t_mono = time.monotonic()
        if self.forward_addr is not None:
            try:
                if self._fwd_sock is None:
                    self._fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self._fwd_sock.setblocking(False)
                self._fwd_sock.sendto(data, self.forward_addr)
                self.forwarded += 1
            except OSError:
                self.forward_errors += 1
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
        pending = getattr(self, "_forward_pending", None)
        if pending is not None:
            self._protocol.forward_addr = pending
        log.info(
            "UDP receiver started",
            extra={"extra": {"host": self.host, "port": self.port}},
        )

    def set_forward(self, value: str) -> Optional[str]:
        """Set/clear the mirror target. Returns the applied 'ip:port' or None.

        Refuses to forward to our own listen port on localhost (loop).
        """
        addr = parse_forward(value)
        if addr and addr[1] == self.port and addr[0] in (
            "127.0.0.1", "localhost", "0.0.0.0",
        ):
            raise ValueError("forward target would loop back to this receiver")
        if self._protocol is not None:
            self._protocol.forward_addr = addr
        self._forward_pending = addr
        log.info("packet forwarding %s",
                 f"-> {addr[0]}:{addr[1]}" if addr else "disabled")
        return f"{addr[0]}:{addr[1]}" if addr else None

    def forward_status(self) -> dict:
        p = self._protocol
        addr = p.forward_addr if p else getattr(self, "_forward_pending", None)
        return {
            "target": f"{addr[0]}:{addr[1]}" if addr else None,
            "forwarded": p.forwarded if p else 0,
            "errors": p.forward_errors if p else 0,
        }

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            log.info("UDP receiver stopped")
