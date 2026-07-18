"""MCP server: let Claude (Desktop / Code) query your telemetry directly.

A minimal, dependency-free implementation of the Model Context Protocol's
stdio transport (newline-delimited JSON-RPC 2.0, tools capability only).
It proxies the running dashboard's HTTP API, so it works the same whether
the app runs as the Windows exe, Docker, or from source.

Setup (Claude Code)::

    claude mcp add fh6-telemetry -- python -m app.mcp_server

Setup (Claude Desktop) — add to ``claude_desktop_config.json``::

    "fh6-telemetry": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "env": {"FH6_URL": "http://127.0.0.1:8080"}
    }

Then ask Claude things like "list my FH6 sessions", "pull the tuning report
for session 4 and suggest setup changes", or "is the game connected?".

``FH6_URL`` points at the dashboard (default ``http://127.0.0.1:8080``).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

BASE_URL = os.environ.get("FH6_URL", "http://127.0.0.1:8080").rstrip("/")

PROTOCOL_VERSION = "2025-03-26"


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:  # pragma: no cover
        return "0.0.0"

TOOLS = [
    {
        "name": "get_live_status",
        "description": (
            "Current receiver state: whether Forza is streaming packets right "
            "now (pps > 0), packet counters, and whether a session is being "
            "recorded."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_sessions",
        "description": (
            "List recorded telemetry sessions with car metadata (ordinal, "
            "class, PI, drivetrain), best lap, frame counts, and notes."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_session_laps",
        "description": (
            "Per-lap breakdown and tuning aggregates for one session: lap "
            "times, tyre temps (deg C) with front/rear balance, understeer "
            "index, slide times, traction events, suspension travel, "
            "gearing, plus session-level balance/temperature verdicts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "integer",
                                          "description": "Session id from list_sessions"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "get_tuning_report",
        "description": (
            "The full Markdown tuning report for a session — the same "
            "document the dashboard exports for AI analysis. Ideal input "
            "for proposing setup changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "integer"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "get_session_analysis",
        "description": (
            "Whole-session analysis: speed/acceleration extremes, input "
            "usage, gear usage, shift RPM, tyre stats, and detected events "
            "(wheelspin, brake lock, over/understeer candidates...)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "integer"}},
            "required": ["session_id"],
        },
    },
]


def _fetch(path: str) -> str:
    req = urllib.request.Request(BASE_URL + path,
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _call_tool(name: str, args: Dict[str, Any]) -> str:
    if name == "get_live_status":
        return _fetch("/api/status")
    if name == "list_sessions":
        return _fetch("/api/sessions")
    sid = args.get("session_id")
    if not isinstance(sid, int):
        raise ValueError("session_id (integer) is required")
    if name == "get_session_laps":
        return _fetch(f"/api/sessions/{sid}/laps")
    if name == "get_tuning_report":
        return _fetch(f"/api/sessions/{sid}/tuning.md")
    if name == "get_session_analysis":
        return _fetch(f"/api/sessions/{sid}/analysis")
    raise ValueError(f"unknown tool: {name}")


def handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message; return the response (None for notifications)."""
    msg_id = msg.get("id")
    method = msg.get("method")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fh6-telemetry",
                               "version": _version()},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = _call_tool(name, args)
            result = {"content": [{"type": "text", "text": text}]}
        except urllib.error.URLError as exc:
            result = {
                "content": [{"type": "text", "text":
                             f"Could not reach the FH6 dashboard at {BASE_URL} "
                             f"({exc}). Is the telemetry app running?"}],
                "isError": True,
            }
        except Exception as exc:  # noqa: BLE001 - surface tool errors in-band
            result = {"content": [{"type": "text", "text": str(exc)}],
                      "isError": True}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if msg_id is None:
        return None  # notification (e.g. notifications/initialized) — ignore
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> None:
    # stdio transport: one JSON-RPC message per line, UTF-8, no framing.
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        response = handle(msg)
        if response is not None:
            stdout.write(json.dumps(response, separators=(",", ":")).encode("utf-8"))
            stdout.write(b"\n")
            stdout.flush()


if __name__ == "__main__":
    main()
