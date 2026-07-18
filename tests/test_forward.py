"""Tests for the raw-packet forwarding helpers."""

from __future__ import annotations

import pytest

from app.telemetry_hub import TelemetryHub
from app.udp_receiver import UDPReceiver, parse_forward


def test_parse_forward_accepts_ip_port():
    assert parse_forward("192.168.1.50:8888") == ("192.168.1.50", 8888)
    assert parse_forward("  127.0.0.1:5300 ") == ("127.0.0.1", 5300)


def test_parse_forward_rejects_garbage():
    assert parse_forward("") is None
    assert parse_forward("not-an-address") is None
    assert parse_forward("host:") is None
    assert parse_forward(":9876") is None
    assert parse_forward("1.2.3.4:0") is None
    assert parse_forward("1.2.3.4:99999") is None


def test_set_forward_refuses_loopback_to_self():
    rx = UDPReceiver(TelemetryHub(), "0.0.0.0", 9876)
    with pytest.raises(ValueError):
        rx.set_forward("127.0.0.1:9876")
    # A different port on localhost is fine.
    assert rx.set_forward("127.0.0.1:8888") == "127.0.0.1:8888"
    assert rx.forward_status()["target"] == "127.0.0.1:8888"
    # Clearing works.
    assert rx.set_forward("") is None
    assert rx.forward_status()["target"] is None
