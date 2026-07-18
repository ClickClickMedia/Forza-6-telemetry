# Advanced: let Claude query your telemetry directly (MCP)

> **You don't need any of this to tune with AI.** The zero-setup path is the
> **"Copy tuning report"** button on the Sessions or Analysis page — it
> copies a complete Markdown report you paste into Claude or ChatGPT (free
> tiers included). This page is for people who already use Claude Desktop or
> Claude Code and want Claude to *pull* the data itself.

The app ships a small [MCP](https://modelcontextprotocol.io) server that
exposes your local telemetry to Claude as tools. It is read-only, talks only
to your local dashboard, and adds no dependencies.

## What Claude can do once connected

Ask things like:

- *"List my FH6 sessions."*
- *"Pull the tuning report for my latest session and suggest setup changes."*
- *"Compare the understeer index across my last three sessions."*
- *"Is the game connected right now?"*

Tools exposed: `get_live_status`, `list_sessions`, `get_session_laps`,
`get_tuning_report`, `get_session_analysis`.

## Setup — Claude Desktop (Windows exe, no Python needed)

1. Make sure the telemetry app is running (the normal double-click).
2. In Claude Desktop: **Settings → Developer → Edit Config**. This opens
   `claude_desktop_config.json`.
3. Add this entry inside `"mcpServers"` (create the block if it doesn't
   exist), with the real path to your exe:

   ```json
   {
     "mcpServers": {
       "fh6-telemetry": {
         "command": "C:\\Users\\you\\Documents\\FH6\\fh6-telemetry.exe",
         "args": ["--mcp"],
         "env": { "FH6_URL": "http://127.0.0.1:8080" }
       }
     }
   }
   ```

4. Restart Claude Desktop. A hammer/tools icon appears in the chat input —
   ask it to list your sessions.

`FH6_URL` points at your dashboard; change it if you run on a different
machine or port (e.g. the collector runs on your gaming PC and Claude on a
laptop: `http://192.168.1.50:8080`).

## Setup — Claude Code (running from source)

```bash
claude mcp add fh6-telemetry -- python -m app.mcp_server
```

(Optionally `--env FH6_URL=http://<host>:8080`.)

## What about ChatGPT?

ChatGPT has no equivalent way to reach a program on your own computer, so
there's no direct connection — use the **Copy tuning report** button and
paste. The report is written to work well in both Claude and ChatGPT.

## Notes

- The MCP process is a thin proxy over the dashboard's HTTP API
  (`/api/...`). It never modifies or uploads your data.
- If Claude reports it can't reach the dashboard, the telemetry app isn't
  running or `FH6_URL` is wrong.
