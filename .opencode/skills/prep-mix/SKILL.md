---
name: prep-mix
description: >
  Prepare a new mix by applying a previously captured session snapshot to the
  current REAPER project. Matches tracks by name (fuzzy matching with user
  confirmation), then applies volume, pan, color, folder structure, FX chains
  with all parameters, per-clip gain, send routing, and master chain. Use this
  skill when the user wants to prep a mix, apply a template, recall settings
  from a previous song, or match a previous mix.
allowed-tools: Bash(python:*), Bash(.venv/bin/python:*), Bash(mkdir:*), Bash(ls:*), Read, Write
---

# Prep Mix

## When to use

The user has a new session in REAPER with raw tracks from an artist, and
wants to apply the settings from a previous snapshot to quickly get to a
starting point that matches their last mix. This skill orchestrates track
matching, user confirmation, and application of the snapshot.

## Prerequisites

- A snapshot JSON file at `snapshots/<artist>/<song>-snapshot.json`
  (created by the snapshot-session skill)
- REAPER is running with the new session loaded
- The distant API is enabled

## Workflow

### Step 1: Identify the snapshot

Ask the user which snapshot to use. List available snapshots:
```bash
ls -la snapshots/*/
```
If the user specifies an artist, look in `snapshots/<artist>/`. Confirm the
exact file path before proceeding.

### Step 2: Get the new session's tracks

Call `reaper_list_tracks` to get the current tracks in the new session.
This returns a list of tracks with index, name, volume, pan, etc.

### Step 3: Run the fuzzy matcher

Run the matching script to propose a track mapping:
```bash
.venv/bin/python scripts/match_tracks.py "<snapshot_path>" '<new_tracks_json>'
```
Where `<new_tracks_json>` is the JSON array of tracks from step 2 (the
`tracks` field from `reaper_list_tracks`), passed as a string argument.

The script outputs:
- `auto_matches`: high-confidence name matches (score >= 0.60)
- `unmatched_snapshot`: snapshot tracks with no auto-match, plus their top
  candidates
- `unmatched_new`: new session tracks not matched to any snapshot track
- `proposed_mapping`: the initial mapping dict for apply_snapshot
- `create_tracks`: snapshot bus/folder tracks that should be created

### Step 4: Present the mapping to the user

Format the matches as a table and present them. Use the question tool to
get confirmation. The table should show:

```
Snapshot Track          -> New Track              Score   Status
─────────────────────────────────────────────────────────────────
[0]  KICK              -> [5]  Kick Drum          0.86    auto ✓
[1]  Sub Kick          -> [6]  Kick Sub           0.82    auto ✓
[5]  SNARE             -> [8]  Snare              0.78    auto ✓
[81] Para 1176         -> (create)                —       bus, no match
[30] EGs               -> (unmatched)             —       needs mapping
[12] Acoustic Gtr      -> [15] Acoustic           0.55    confirm?
```

Ask the user to:
1. **Confirm or reject auto-matches** (especially low-score ones)
2. **Map unmatched snapshot source tracks** to new tracks (or skip them)
3. **Decide on unmatched new tracks** — for each, ask:
   - Skip (leave untouched)
   - Map manually to a snapshot track
   - Apply a default chain (unity gain, no FX)

### Step 5: Build the final mapping

Based on the user's confirmations, build the `track_mapping` dict:
```python
{
    "0": 5,       # snap_index -> new_index
    "1": 6,
    "5": 8,
    "81": "create",  # create as new track (bus/folder)
    "82": "create",
    ...
}
```

Key conventions:
- String keys (snapshot track indices)
- Integer values (new session track indices)
- The string "create" as a value means "create this track in the new session"
- Any snapshot track NOT in the mapping will be automatically created by
  `reaper_apply_snapshot` (this includes trig tracks, copied tracks, buses,
  and folders — they all get created with their snapshot settings)
- Omit a key only if you explicitly want to skip that snapshot track entirely

### Step 6: Apply the snapshot

Call `reaper_apply_snapshot` with:
- `snapshot_path`: the path to the snapshot JSON file
- `track_mapping`: the confirmed mapping dict
- `match_clip_levels`: True (default) — measures each new clip's peak and
  adjusts gain to match the snapshot's peak level, so clips hit the FX chain
  at a similar level even if recorded at different levels
- `reorder_tracks`: True (default) — reorders tracks to match the snapshot's
  track order, which is necessary for folder structure to work correctly
- `skip_fx`: False (unless the user wants to skip FX)
- `skip_sends`: False (unless the user wants to skip sends)

**This is a destructive operation** — the user will be prompted to approve
it via the opencode permission system. State what will happen before they
approve:
- "This will apply settings from the snapshot to N tracks, add M FX plugins,
  set K parameter values, create P sends, and add Q master FX. Tracks not in
  the mapping will be left untouched."

### Step 7: Report results

After applying, print the report:
- Tracks applied / created
- FX added / skipped (already present)
- Parameters set
- Sends created / skipped
- Items adjusted (clip gain)
- Master FX added / params set
- Any failures or warnings

Highlight any failures or warnings that need manual attention.

## Edge cases

- **A plugin in the snapshot isn't installed**: the FX will fail to add.
  Report it in the failures list — the user needs to install the plugin
  or map to a different one manually.
- **Track ordering differs**: folder structure is set via I_FOLDERDEPTH
  values, but if tracks are in a different visual order, folders may not
  display correctly. Warn the user if snapshot and new session track
  orders differ significantly.
- **Snapshot has more tracks than the new session**: unmatched snapshot
  tracks are skipped with a warning. Buses/folders marked "create" will
  be appended at the end of the track list.
- **New session has tracks not in the snapshot**: these are left completely
  untouched (no volume change, no FX, no routing). The user should mix
  them manually.
- **Dry run**: if the user wants to preview without applying, pass
  `dry_run=True` to `reaper_apply_snapshot`. This reports what would
  happen without modifying REAPER.
