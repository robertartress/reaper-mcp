# REAPER MCP

This project ships a REAPER MCP server (`reaper-mcp-server`) that exposes REAPER DAW control as tools prefixed `reaper_` (e.g. `reaper_create_track`, `reaper_render_project`). 66 tools cover project, tracks, MIDI, FX, audio, mixing, rendering, mastering, analysis, session snapshotting, and mix prep.

## Snapshot + Prep-Mix Workflow

The `reaper_snapshot_session` tool captures the full mix state (tracks, items, FX + all params + presets, sends, master) as a JSON blob. The `reaper_apply_snapshot` tool replays that state onto a new session. Two opencode skills orchestrate the workflow:

- **snapshot-session** — captures a finished mix to `snapshots/<artist>/<song>-snapshot.json`
- **prep-mix** — loads a snapshot, fuzzy-matches track names against the new session, gets user confirmation on the mapping, then calls `reaper_apply_snapshot` to apply all settings

Use these skills when the user wants to save a mix template or prep a new session from a previous mix. The fuzzy matcher lives in `src/reaper_mcp/track_matcher.py` with a CLI wrapper at `scripts/match_tracks.py`.

## When to use reaper_ tools

- Only use `reaper_*` tools for explicit REAPER / DAW tasks the user asked for.
- Do not call them speculatively or as part of unrelated coding work.
- REAPER must be running with the distant API enabled. If a tool returns a connection error, tell the user to run `scripts/enable_reapy.py` (or `import reapy; reapy.config.enable_dist_api()` in REAPER's Actions > Run ReaScript) and restart REAPER.

## Destructive / expensive operations - confirm first

These tools can overwrite files, delete project data, record audio, or render long audio. opencode is configured to prompt for approval before running them (permission: `"ask"`). Treat that list as authoritative and do not try to bypass it:

- `reaper_create_project`, `reaper_load_project`, `reaper_save_project`
- `reaper_delete_track`, `reaper_remove_fx`, `reaper_remove_send`
- `reaper_start_recording`
- `reaper_render_project`, `reaper_render_stems`, `reaper_render_time_selection`
- `reaper_apply_mastering_chain`, `reaper_apply_limiter`, `reaper_normalize_project`
- `reaper_apply_snapshot` (overwrites volume, pan, FX params, sends, clip gain, master/parent send state, and folder structure on matched tracks; also creates missing tracks and reorders the track list)

For rendering and mastering, state the settings and expected output path before the user approves. For `reaper_apply_snapshot`, state how many tracks will be modified, how many FX will be added, and how many parameters will be set.

## Setup

The server is registered in `opencode.json` under `mcp.reaper` and runs from the local venv at `.venv/bin/reaper-mcp-server`. If the venv is missing, create it:

```bash
python3.12 -m venv .venv && .venv/bin/python -m pip install -e .
```

Run `/reaper` for a status check.
