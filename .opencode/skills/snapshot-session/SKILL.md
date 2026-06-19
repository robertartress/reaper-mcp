---
name: snapshot-session
description: >
  Capture the full mix state of the current REAPER project as a JSON snapshot
  file. Use this skill when the user wants to save a mix template, snapshot
  a session, or create a baseline for future mix prep. This skill calls
  reaper_snapshot_session and writes the result to
  snapshots/<artist>/<song>-snapshot.json.
allowed-tools: Bash(python:*), Bash(mkdir:*), Bash(ls:*), Read, Write
---

# Snapshot Session

## When to use

The user has a finished (or near-finished) mix in REAPER and wants to capture
its entire state — every track's volume, pan, color, folder structure, FX
chains with all parameter values + full FX chain chunks, per-clip gain, send
routing, and master chain — so it can be recalled on a future session for the
same artist.

## Workflow

1. **Confirm the artist and song name with the user.** If they didn't
   specify, ask. These determine the storage path:
   `snapshots/<artist>/<song>-snapshot.json`

2. **Call `reaper_snapshot_session`** with the artist and song name.
   This captures the full session state as a JSON blob, including base64-
   encoded FX chain chunks for each track. It is non-destructive (read-only)
   and takes ~30 seconds for a 100+ track session.

3. **Create the storage directory** if it doesn't exist:
   ```bash
   mkdir -p "snapshots/<artist>"
   ```

4. **Write the snapshot to disk.** The tool returns the snapshot under the
   `snapshot` key. Use the Write tool to save it as JSON:
   `snapshots/<artist>/<song>-snapshot.json`

5. **Export sidecar .rfxchain files** by calling `reaper_export_fxchains`
   with the snapshot path. This writes one `.rfxchain` file per track to
   `snapshots/<artist>/<song>-fxchains/`. These files can be manually
   loaded in REAPER (right-click FX chain > Load FX chain) and provide
   a human-inspectable backup of each track's full FX state.

6. **Print a summary** for the user:
   - Track count, FX count, parameter count, send count
   - Role breakdown (folders, sources, buses, empty)
   - Master FX count
   - File path and size
   - Sidecar .rfxchain count and directory
   - The captured_at timestamp

## Important notes

- The snapshot file can be large (40+ MB for a complex session). This is
  expected — it contains every FX parameter value plus base64-encoded FX
  chain chunks. JSON files are kept in git; binary exports (.rfxchain,
  .rTrackTemplate) are gitignored.
- The snapshot captures FX chains two ways: (1) per-parameter values for
  metadata and fallback, (2) full state-chunk blocks for reliable recall.
  On apply, chunks are preferred (they capture internal plugin state like
  Ozone modules that parameters miss); parameter replay is the fallback.
- The snapshot is schema-versioned (currently v1.1). Future versions will
  be backward-compatible.
- If `reaper_snapshot_session` returns an error about connecting to REAPER,
  tell the user to run `scripts/enable_reapy.py` and restart REAPER.
- Always verify the summary looks reasonable before reporting success
  (e.g. if param_count is 0, something went wrong).
