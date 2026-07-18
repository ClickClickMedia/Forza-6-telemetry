"""Tests for the minimal MCP stdio server (protocol handling, not transport)."""

from __future__ import annotations

import json

from app import mcp_server


def test_initialize_shape():
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26"}})
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"]
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "fh6-telemetry"


def test_notifications_are_ignored():
    assert mcp_server.handle({"jsonrpc": "2.0",
                              "method": "notifications/initialized"}) is None


def test_tools_list():
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"get_live_status", "list_sessions", "get_session_laps",
                     "get_tuning_report", "get_session_analysis"}
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_tools_call_proxies_http(monkeypatch):
    calls = []

    def fake_fetch(path):
        calls.append(path)
        return json.dumps({"ok": True, "path": path})

    monkeypatch.setattr(mcp_server, "_fetch", fake_fetch)

    resp = mcp_server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_session_laps", "arguments": {"session_id": 7}},
    })
    content = resp["result"]["content"][0]
    assert content["type"] == "text"
    assert json.loads(content["text"])["path"] == "/api/sessions/7/laps"
    assert calls == ["/api/sessions/7/laps"]


def test_tools_call_missing_arg_is_tool_error(monkeypatch):
    monkeypatch.setattr(mcp_server, "_fetch", lambda p: "{}")
    resp = mcp_server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "get_tuning_report", "arguments": {}},
    })
    assert resp["result"]["isError"] is True


def test_unknown_method_errors():
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})
    assert resp["error"]["code"] == -32601
