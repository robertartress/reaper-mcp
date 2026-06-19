import logging
import datetime
import json
import os
import base64

import reapy
from reapy import reascript_api as RPR

from reaper_mcp.connection import get_project
from reaper_mcp.mixing_tools import _db_to_linear, _linear_to_db

logger = logging.getLogger("reaper_mcp.snapshot_tools")

SNAPSHOT_VERSION = "1.1"

_AUTOMODE_NAMES = {
    -1: "default",
    0: "trim/off",
    1: "read",
    2: "touch",
    3: "write",
    4: "latch",
    5: "latch preview",
}

_SENDMODE_NAMES = {
    0: "post-fader",
    1: "pre-fx",
    2: "post-fx (deprecated)",
    3: "post-fx",
}

_SENDMODE_RAW = {
    "post-fader": 0,
    "pre-fx": 1,
    "post-fx (deprecated)": 2,
    "post-fx": 3,
}

_AUTOMODE_RAW = {
    "default": -1,
    "trim/off": 0,
    "read": 1,
    "touch": 2,
    "write": 3,
    "latch": 4,
    "latch preview": 5,
}


def _decode_input(recinput: int) -> dict:
    """Decode I_RECINPUT into a human-readable source description."""
    if recinput >= 4096:
        midi_chan = recinput - 4096
        if midi_chan == 0:
            return {"source": "midi", "channels": "all", "raw": recinput}
        return {"source": "midi", "channels": midi_chan, "raw": recinput}
    if recinput < 0:
        return {"source": "none", "raw": recinput}
    mono = bool(recinput & 1024)
    chan = recinput & 0x3FF
    return {"source": "audio", "channel": chan, "mono": mono, "raw": recinput}


def _decode_color(color_val: float) -> list:
    """Decode I_CUSTOMCOLOR (int as float) into [r, g, b]; [0,0,0] if default."""
    c = int(color_val)
    if c & 0x1000000:
        _, r, g, b = RPR.ColorFromNative(c & 0xFFFFFF, 0, 0, 0)
        return [int(r), int(g), int(b)]
    return [0, 0, 0]


def _read_fx_chain(track_id) -> list:
    """Read the full FX chain of a track (or master) via raw TrackFX_* calls."""
    n_fx = RPR.TrackFX_GetCount(track_id)
    fx_list = []
    for fx in range(n_fx):
        name = RPR.TrackFX_GetFXName(track_id, fx, "", 2048)[3]
        preset = RPR.TrackFX_GetPreset(track_id, fx, "", 2048)[3]
        enabled = bool(RPR.TrackFX_GetEnabled(track_id, fx))
        n_params = RPR.TrackFX_GetNumParams(track_id, fx)
        params = []
        for p in range(n_params):
            pname = RPR.TrackFX_GetParamName(track_id, fx, p, "", 2048)[4]
            norm = RPR.TrackFX_GetParamNormalized(track_id, fx, p)
            raw, pmin, pmax = RPR.TrackFX_GetParam(track_id, fx, p, 0, 0)[3:6]
            params.append({
                "index": p,
                "name": pname,
                "normalized": float(norm),
                "raw": float(raw),
                "min": float(pmin),
                "max": float(pmax),
            })
        fx_list.append({
            "index": fx,
            "name": name,
            "enabled": enabled,
            "preset": preset,
            "n_params": n_params,
            "params": params,
        })
    return fx_list


def _extract_fxchain_block(track_id):
    """Extract the <FXCHAIN> block from a track's state chunk.

    Returns the raw text of the FXCHAIN block (from '<FXCHAIN' to its
    matching '>'), or None if the track has no FX chain block.

    Uses depth tracking: lines starting with '<' + alpha open a block,
    lines that are exactly '>' close a block.
    """
    res = RPR.GetTrackStateChunk(track_id, "", 16 * 1024 * 1024, False)
    chunk = res[2]
    lines = chunk.split("\n")

    start_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("<FXCHAIN"):
            start_idx = i
            break
    if start_idx is None:
        return None

    depth = 0
    block_lines = []
    for i in range(start_idx, len(lines)):
        stripped = lines[i].strip()
        if stripped and stripped[0] == "<" and len(stripped) > 1 and stripped[1].isalpha():
            depth += 1
        elif stripped == ">":
            depth -= 1
        block_lines.append(lines[i])
        if depth == 0:
            break
    return "\n".join(block_lines) if block_lines else None


def _replace_fxchain_block(track_id, fxchain_text):
    """Replace the <FXCHAIN> block in a track's state chunk.

    Removes the existing FXCHAIN block (if any) and inserts the new one
    in its place. Returns True on success.
    """
    res = RPR.GetTrackStateChunk(track_id, "", 16 * 1024 * 1024, False)
    chunk = res[2]
    lines = chunk.split("\n")

    # Find existing FXCHAIN block boundaries.
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            if stripped.startswith("<FXCHAIN"):
                start_idx = i
                depth = 0
        else:
            if stripped and stripped[0] == "<" and len(stripped) > 1 and stripped[1].isalpha():
                depth += 1
            elif stripped == ">":
                depth -= 1
            if depth == 0:
                end_idx = i
                break

    new_lines = fxchain_text.split("\n")

    if start_idx is not None and end_idx is not None:
        # Replace existing block.
        rebuilt = lines[:start_idx] + new_lines + lines[end_idx + 1:]
    elif start_idx is not None:
        # FXCHAIN exists but no closing '>' found — replace to end.
        rebuilt = lines[:start_idx] + new_lines
    else:
        # No FXCHAIN block — insert before the track's closing '>'.
        # Find the last '>' that closes the TRACK block.
        track_depth = 0
        insert_at = len(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and stripped[0] == "<" and len(stripped) > 1 and stripped[1].isalpha():
                track_depth += 1
            elif stripped == ">":
                track_depth -= 1
                if track_depth == 0:
                    insert_at = i
                    break
        rebuilt = lines[:insert_at] + new_lines + lines[insert_at:]

    new_chunk = "\n".join(rebuilt)
    RPR.SetTrackStateChunk(track_id, new_chunk, False)
    return True


def _encode_fxchain_chunk(fxchain_text):
    """Base64-encode an FXCHAIN text block for JSON storage."""
    if fxchain_text is None:
        return None
    return base64.b64encode(fxchain_text.encode("utf-8")).decode("ascii")


def _decode_fxchain_chunk(encoded):
    """Decode a base64-encoded FXCHAIN block back to text."""
    if encoded is None:
        return None
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")


def _read_sends(track_id) -> list:
    """Read all track-to-track sends (category 0) from a track."""
    n = RPR.GetTrackNumSends(track_id, 0)
    sends = []
    for i in range(n):
        vol = RPR.GetTrackSendInfo_Value(track_id, 0, i, "D_VOL")
        pan = RPR.GetTrackSendInfo_Value(track_id, 0, i, "D_PAN")
        muted = bool(RPR.GetTrackSendInfo_Value(track_id, 0, i, "B_MUTE"))
        mode = int(RPR.GetTrackSendInfo_Value(track_id, 0, i, "I_SENDMODE"))
        phase = bool(RPR.GetTrackSendInfo_Value(track_id, 0, i, "B_PHASE"))
        src_chan = int(RPR.GetTrackSendInfo_Value(track_id, 0, i, "I_SRCCHAN"))
        dst_chan = int(RPR.GetTrackSendInfo_Value(track_id, 0, i, "I_DSTCHAN"))
        # Resolve destination track index via the P_DESTTRACK pointer.
        dest_index = None
        dest_name = ""
        ptr = RPR.GetTrackSendInfo_Value(track_id, 0, i, "P_DESTTRACK")
        try:
            dest_track = reapy.Track(reapy.Track._get_id_from_pointer(ptr))
            di = int(RPR.GetMediaTrackInfo_Value(dest_track.id, "IP_TRACKNUMBER")) - 1
            if di >= 0:
                dest_index = di
                _, _, dest_name, _ = RPR.GetTrackName(dest_track.id, "", 2048)
        except Exception:
            pass
        sends.append({
            "send_index": i,
            "dest_index": dest_index,
            "dest_name": dest_name,
            "volume_db": _linear_to_db(vol),
            "pan": float(pan),
            "type": _SENDMODE_NAMES.get(mode, str(mode)),
            "send_mode_raw": mode,
            "muted": muted,
            "phase": phase,
            "src_channel": src_chan,
            "dst_channel": dst_chan,
        })
    return sends


def _read_items(track_id) -> list:
    """Read all media items on a track, including per-take gain/pitch/rate and peak level."""
    n_items = RPR.CountTrackMediaItems(track_id)
    items = []
    for it in range(n_items):
        item_id = RPR.GetTrackMediaItem(track_id, it)
        position = RPR.GetMediaItemInfo_Value(item_id, "D_POSITION")
        length = RPR.GetMediaItemInfo_Value(item_id, "D_LENGTH")
        gain = RPR.GetMediaItemInfo_Value(item_id, "D_VOL")
        fade_in = RPR.GetMediaItemInfo_Value(item_id, "D_FADEINLEN")
        fade_out = RPR.GetMediaItemInfo_Value(item_id, "D_FADEOUTLEN")
        item = {
            "index": it,
            "position": float(position),
            "length": float(length),
            "gain_db": _linear_to_db(gain),
            "fade_in": float(fade_in),
            "fade_out": float(fade_out),
        }
        # Per-take properties from the active take.
        take_id = RPR.GetActiveTake(item_id)
        if take_id:
            pitch = RPR.GetMediaItemTakeInfo_Value(take_id, "D_PITCH")
            rate = RPR.GetMediaItemTakeInfo_Value(take_id, "D_PLAYRATE")
            offset = RPR.GetMediaItemTakeInfo_Value(take_id, "D_STARTOFFS")
            is_midi = bool(RPR.TakeIsMIDI(take_id))
            item["pitch"] = float(pitch)
            item["playback_rate"] = float(rate)
            item["take_offset"] = float(offset)
            item["is_midi"] = is_midi
        # Peak level (dBFS) after gain — used for loudness-matched clip gain on apply.
        # NF_GetMediaItemMaxPeak returns the peak including the item's current gain.
        try:
            peak_dbfs = RPR.NF_GetMediaItemMaxPeak(item_id)
            item["peak_dbfs"] = float(peak_dbfs)
        except Exception:
            item["peak_dbfs"] = None
        items.append(item)
    return items


def _read_track(track, index: int) -> dict:
    """Read a single track into a snapshot dict via raw RPR calls."""
    track_id = track.id
    name = RPR.GetSetMediaTrackInfo_String(track_id, "P_NAME", "", False)[3]
    vol = RPR.GetMediaTrackInfo_Value(track_id, "D_VOL")
    pan = RPR.GetMediaTrackInfo_Value(track_id, "D_PAN")
    muted = bool(RPR.GetMediaTrackInfo_Value(track_id, "B_MUTE"))
    solo = bool(RPR.GetMediaTrackInfo_Value(track_id, "B_SOLO"))
    phase = bool(RPR.GetMediaTrackInfo_Value(track_id, "B_PHASE"))
    folder_depth = int(RPR.GetMediaTrackInfo_Value(track_id, "I_FOLDERDEPTH"))
    color_val = RPR.GetMediaTrackInfo_Value(track_id, "I_CUSTOMCOLOR")
    automode = int(RPR.GetMediaTrackInfo_Value(track_id, "I_AUTOMODE"))
    recinput = int(RPR.GetMediaTrackInfo_Value(track_id, "I_RECINPUT"))
    recmon = int(RPR.GetMediaTrackInfo_Value(track_id, "I_RECMON"))
    main_send = bool(RPR.GetMediaTrackInfo_Value(track_id, "B_MAINSEND"))

    # Parent track (folder) resolution.
    parent_index = None
    parent_id = RPR.GetParentTrack(track_id)
    try:
        if parent_id:
            pidx = int(RPR.GetMediaTrackInfo_Value(parent_id, "IP_TRACKNUMBER")) - 1
            if pidx >= 0:
                parent_index = pidx
    except Exception:
        pass

    n_items = RPR.CountTrackMediaItems(track_id)
    n_sends = RPR.GetTrackNumSends(track_id, 0)
    n_receives = RPR.GetTrackNumSends(track_id, -1)

    # Derive role: folder > source > bus > empty.
    if folder_depth == 1:
        role = "folder"
    elif n_items > 0:
        role = "source"
    elif n_receives > 0:
        role = "bus"
    else:
        role = "empty"

    return {
        "index": index,
        "name": name,
        "role": role,
        "volume_db": _linear_to_db(vol),
        "pan": float(pan),
        "color": _decode_color(color_val),
        "muted": muted,
        "soloed": solo,
        "phase": phase,
        "folder_depth": folder_depth,
        "parent_index": parent_index,
        "automation_mode": _AUTOMODE_NAMES.get(automode, str(automode)),
        "automation_mode_raw": automode,
        "input": _decode_input(recinput),
        "input_monitoring": recmon,
        "main_send_enabled": main_send,
        "items": _read_items(track_id),
        "fx": _read_fx_chain(track_id),
        "fxchain_chunk": _encode_fxchain_chunk(_extract_fxchain_block(track_id)),
        "sends": _read_sends(track_id),
        "n_items": int(n_items),
        "n_sends": int(n_sends),
        "n_receives": int(n_receives),
    }


def _read_master(project) -> dict:
    """Read the master track (volume, pan, FX chain)."""
    master = project.master_track
    mid = master.id
    vol = RPR.GetMediaTrackInfo_Value(mid, "D_VOL")
    pan = RPR.GetMediaTrackInfo_Value(mid, "D_PAN")
    return {
        "volume_db": _linear_to_db(vol),
        "pan": float(pan),
        "fx": _read_fx_chain(mid),
        "fxchain_chunk": _encode_fxchain_chunk(_extract_fxchain_block(mid)),
    }


# ---------------------------------------------------------------------------
# Apply helpers (write side)
# ---------------------------------------------------------------------------

def _strip_fx_prefix(name: str) -> str:
    """Strip the 'VST3: ', 'VST: ', 'AU: ', etc. prefix from an FX name."""
    if ": " in name:
        return name.split(": ", 1)[1]
    return name


def _find_fx_by_name(track_id, target_name: str):
    """Return the index of an FX with matching full name, or None."""
    n = RPR.TrackFX_GetCount(track_id)
    for i in range(n):
        name = RPR.TrackFX_GetFXName(track_id, i, "", 2048)[3]
        if name == target_name:
            return i
    return None


def _find_send_to_dest(track_id, dest_track_id):
    """Return the index of the first send from track_id to dest_track_id, or None."""
    n = RPR.GetTrackNumSends(track_id, 0)
    for i in range(n):
        ptr = RPR.GetTrackSendInfo_Value(track_id, 0, i, "P_DESTTRACK")
        try:
            existing_dest = reapy.Track(reapy.Track._get_id_from_pointer(ptr))
            if existing_dest.id == dest_track_id:
                return i
        except Exception:
            pass
    return None


def _reorder_tracks(project, desired_track_ids):
    """Reorder tracks so that track IDs appear in the order of desired_track_ids.

    Uses InsertTrackAtIndex + MoveMediaItemToTrack + TrackFX_CopyToTrack + DeleteTrack
    since Main_OnCommand move actions don't work through the distant API.

    Tracks not in desired_track_ids are left at the end in their current relative order.
    """
    # Build a set of track IDs that need to be in specific positions.
    desired_set = set()
    for tid in desired_track_ids:
        if tid is not None:
            desired_set.add(tid)

    # For each desired position, ensure the right track is there.
    # We process from top to bottom. When a track is out of position, we:
    # 1. Insert a new track at the target position
    # 2. Move all items from the source track to the new track
    # 3. Copy FX chain from source to new
    # 4. Copy track name
    # 5. Delete the old (now empty) source track
    # 6. Update the desired_track_ids list to point to the new track

    for target_pos in range(len(desired_track_ids)):
        desired_id = desired_track_ids[target_pos]
        if desired_id is None:
            continue

        # Find where this track currently is.
        current_pos = None
        for i in range(project.n_tracks):
            if project.tracks[i].id == desired_id:
                current_pos = i
                break

        if current_pos is None:
            # Track was already moved/deleted — skip.
            continue

        if current_pos == target_pos:
            continue

        # Insert a new track at the target position.
        # wantNewTrack=True is critical: with False, REAPER recycles
        # previously-deleted tracks from its internal pool, which can
        # still carry stale media items, FX chains, and settings from
        # their previous life. This causes clips to be doubled up on
        # the destination track (the "buried clip" bug).
        RPR.InsertTrackAtIndex(target_pos, True)
        new_track = project.tracks[target_pos]

        # Get the source track (its position may have shifted by +1 due to insertion).
        source_track = project.tracks[current_pos + 1]

        # Copy name.
        source_name = RPR.GetSetMediaTrackInfo_String(source_track.id, "P_NAME", "", False)[3]
        RPR.GetSetMediaTrackInfo_String(new_track.id, "P_NAME", source_name, True)

        # Move all items from source to new track.
        n_items = RPR.CountTrackMediaItems(source_track.id)
        for _ in range(n_items):
            item_id = RPR.GetTrackMediaItem(source_track.id, 0)
            RPR.MoveMediaItemToTrack(item_id, new_track.id)

        # Copy FX chain.
        n_fx = RPR.TrackFX_GetCount(source_track.id)
        for fx_idx in range(n_fx):
            RPR.TrackFX_CopyToTrack(source_track.id, fx_idx, new_track.id, fx_idx, False)

        # Copy key track settings (volume, pan, color, mute, solo, phase, main send,
        # folder depth, automation, input). These will also be set by apply_snapshot
        # later, but we copy them now so the track is functional before the apply step.
        for field in ["D_VOL", "D_PAN", "I_CUSTOMCOLOR", "B_MUTE", "B_SOLO",
                       "B_PHASE", "B_MAINSEND", "I_FOLDERDEPTH", "I_AUTOMODE",
                       "I_RECINPUT", "I_RECMON"]:
            val = RPR.GetMediaTrackInfo_Value(source_track.id, field)
            RPR.SetMediaTrackInfo_Value(new_track.id, field, val)

        # Copy sends from source to new track.
        n_sends = RPR.GetTrackNumSends(source_track.id, 0)
        for s_idx in range(n_sends):
            ptr = RPR.GetTrackSendInfo_Value(source_track.id, 0, s_idx, "P_DESTTRACK")
            try:
                dest_track = reapy.Track(reapy.Track._get_id_from_pointer(ptr))
                new_send = RPR.CreateTrackSend(new_track.id, dest_track.id)
                for sfield in ["D_VOL", "D_PAN", "I_SENDMODE", "B_MUTE", "B_PHASE"]:
                    sval = RPR.GetTrackSendInfo_Value(source_track.id, 0, s_idx, sfield)
                    RPR.SetTrackSendInfo_Value(new_track.id, 0, new_send, sfield, sval)
            except Exception:
                pass

        # Delete the old source track.
        RPR.DeleteTrack(source_track.id)

        # Update desired_track_ids to point to the new track's ID.
        new_id = new_track.id
        for j in range(len(desired_track_ids)):
            if desired_track_ids[j] == desired_id:
                desired_track_ids[j] = new_id


def _apply_sends(track_id, track_snap, mapping, report, dry_run=False,
                 folder_parent_indices=None, project=None):
    """Create sends from a track to mapped destination tracks.

    folder_parent_indices: set of snapshot track indices that are folder parents.
    Sends to a track's own folder parent are skipped (folder structure handles that
    routing automatically).
    """
    if folder_parent_indices is None:
        folder_parent_indices = set()

    for send_snap in track_snap["sends"]:
        dest_snap_index = send_snap["dest_index"]
        if dest_snap_index is None:
            report["warnings"].append(
                f"Track '{track_snap['name']}': send {send_snap['send_index']} "
                f"has no resolved destination — skipped"
            )
            continue

        # Skip sends to this track's folder parent — the folder structure
        # handles parent routing automatically in REAPER.
        if dest_snap_index == track_snap.get("parent_index"):
            report["sends_skipped"] += 1
            continue

        dest_new_index = mapping.get(dest_snap_index)
        if dest_new_index is None or dest_new_index == "create":
            report["warnings"].append(
                f"Track '{track_snap['name']}': send to '{send_snap['dest_name']}' "
                f"(snapshot track {dest_snap_index}) is unmapped — skipped"
            )
            continue

        # Destination track is mapped — create the send.
        if dry_run:
            report["sends_created"] += 1
            continue

        if project is None:
            report["warnings"].append(
                f"Track '{track_snap['name']}': send to '{send_snap['dest_name']}' "
                f"— no project context, skipped"
            )
            continue

        try:
            dest_track = project.tracks[int(dest_new_index)]
        except Exception:
            report["warnings"].append(
                f"Track '{track_snap['name']}': send to '{send_snap['dest_name']}' "
                f"(new track {dest_new_index}) could not be resolved — skipped"
            )
            continue

        # Skip if a send to this destination already exists (idempotency).
        existing = False
        n_existing = RPR.GetTrackNumSends(track_id, 0)
        for si in range(n_existing):
            ptr = RPR.GetTrackSendInfo_Value(track_id, 0, si, "P_DESTTRACK")
            try:
                ex_dest = reapy.Track(reapy.Track._get_id_from_pointer(ptr))
                if ex_dest.id == dest_track.id:
                    existing = True
                    break
            except Exception:
                pass
        if existing:
            report["sends_skipped"] += 1
            continue

        new_send = RPR.CreateTrackSend(track_id, dest_track.id)
        if new_send < 0:
            report["failed"].append({
                "item": f"Send '{track_snap['name']}' -> '{send_snap['dest_name']}'",
                "error": "CreateTrackSend returned -1",
            })
            continue

        # Replay send parameters.
        RPR.SetTrackSendInfo_Value(track_id, 0, new_send, "D_VOL",
                                   _db_to_linear(send_snap["volume_db"]))
        RPR.SetTrackSendInfo_Value(track_id, 0, new_send, "D_PAN",
                                   float(send_snap["pan"]))
        RPR.SetTrackSendInfo_Value(track_id, 0, new_send, "B_MUTE",
                                   1 if send_snap["muted"] else 0)
        RPR.SetTrackSendInfo_Value(track_id, 0, new_send, "B_PHASE",
                                   1 if send_snap["phase"] else 0)
        mode = send_snap.get("send_mode_raw")
        if mode is None:
            mode = _SENDMODE_RAW.get(send_snap.get("type", "post-fader"), 0)
        RPR.SetTrackSendInfo_Value(track_id, 0, new_send, "I_SENDMODE", int(mode))
        # Restore source/dest channel routing (I_SRCCHAN is signed: <0 = stereo).
        if "src_channel" in send_snap:
            RPR.SetTrackSendInfo_Value(track_id, 0, new_send, "I_SRCCHAN",
                                       int(send_snap["src_channel"]))
        if "dst_channel" in send_snap:
            RPR.SetTrackSendInfo_Value(track_id, 0, new_send, "I_DSTCHAN",
                                       int(send_snap["dst_channel"]))
        report["sends_created"] += 1


def _apply_fx_chain_from_chunk(track_id, fxchain_text, report, dry_run=False):
    """Apply an entire FX chain from a state-chunk block.

    This replaces the track's entire FX chain with the captured chunk,
    which includes all plugins and their full binary state (presets,
    internal data, routing, etc.). This is the most reliable method for
    plugins like Ozone that store state not exposed via parameters.

    Returns True if applied, False if skipped or failed.
    """
    if not fxchain_text:
        return False
    if dry_run:
        report["fx_added"] += fxchain_text.count("<VST") + fxchain_text.count("<AU")
        report["fx_skipped"] += 0
        report["params_set"] += 0
        return True
    try:
        _replace_fxchain_block(track_id, fxchain_text)
        # Count FX and params for reporting (chunk applies them all at once).
        report["fx_added"] += fxchain_text.count("<VST") + fxchain_text.count("<AU")
        report["params_set"] += 0  # chunk state is opaque; params are embedded
        return True
    except Exception as e:
        report["failed"].append({
            "item": "FX chain chunk replacement",
            "error": str(e),
        })
        return False


def _apply_fx_chain(track_id, snapshot_fx_list, report, dry_run=False,
                    fxchain_chunk=None):
    """Apply an FX chain to a track.

    If fxchain_chunk (base64-encoded state-chunk FXCHAIN block) is provided,
    uses chunk-based replacement (most reliable — captures full plugin state
    including internal data not exposed as parameters). Falls back to
    parameter-by-parameter replay otherwise.
    """
    if fxchain_chunk:
        fxchain_text = _decode_fxchain_chunk(fxchain_chunk)
        if fxchain_text:
            if _apply_fx_chain_from_chunk(track_id, fxchain_text, report, dry_run):
                return

    # Fallback: parameter-by-parameter replay.
    for fx_snap in snapshot_fx_list:
        fx_name = fx_snap["name"]
        fx_index = _find_fx_by_name(track_id, fx_name)
        if fx_index is not None:
            report["fx_skipped"] += 1
        else:
            if dry_run:
                report["fx_added"] += 1
                continue
            add_name = _strip_fx_prefix(fx_name)
            fx_index = RPR.TrackFX_AddByName(track_id, add_name, False, 1)
            if fx_index < 0:
                report["failed"].append({
                    "item": f"FX '{fx_name}'",
                    "error": "Plugin not found in REAPER",
                })
                continue
            report["fx_added"] += 1

        # Set preset if one was captured and is non-empty.
        if fx_snap.get("preset") and not dry_run:
            try:
                RPR.TrackFX_SetPreset(track_id, fx_index, fx_snap["preset"])
            except Exception:
                pass

        # Set enabled/bypass state.
        if not dry_run:
            RPR.TrackFX_SetEnabled(track_id, fx_index, fx_snap["enabled"])

        # Replay every parameter.
        for p in fx_snap["params"]:
            if dry_run:
                report["params_set"] += 1
                continue
            RPR.TrackFX_SetParamNormalized(
                track_id, fx_index, p["index"], p["normalized"]
            )
            report["params_set"] += 1


def _apply_track_settings(track_id, track_snap, report, skip_items=False,
                          dry_run=False, match_clip_levels=False):
    """Apply volume, pan, color, mute, solo, phase, folder, automation, input, main send."""
    if dry_run:
        return

    # Volume and pan.
    RPR.SetMediaTrackInfo_Value(track_id, "D_VOL", _db_to_linear(track_snap["volume_db"]))
    RPR.SetMediaTrackInfo_Value(track_id, "D_PAN", track_snap["pan"])

    # Color.
    r, g, b = track_snap["color"]
    if r or g or b:
        color = RPR.ColorToNative(r, g, b) | 0x1000000
        RPR.SetMediaTrackInfo_Value(track_id, "I_CUSTOMCOLOR", color)

    # Mute, solo, phase.
    RPR.SetMediaTrackInfo_Value(track_id, "B_MUTE", 1 if track_snap["muted"] else 0)
    RPR.SetMediaTrackInfo_Value(track_id, "B_SOLO", 1 if track_snap["soloed"] else 0)
    RPR.SetMediaTrackInfo_Value(track_id, "B_PHASE", 1 if track_snap["phase"] else 0)

    # Folder depth — set after track reordering so hierarchy is correct.
    RPR.SetMediaTrackInfo_Value(track_id, "I_FOLDERDEPTH", track_snap["folder_depth"])

    # Main/parent send enable/disable.
    main_send = track_snap.get("main_send_enabled", True)
    RPR.SetMediaTrackInfo_Value(track_id, "B_MAINSEND", 1 if main_send else 0)

    # Automation mode.
    automode = track_snap.get("automation_mode_raw", 1)
    RPR.SetMediaTrackInfo_Value(track_id, "I_AUTOMODE", automode)

    # Input source.
    raw_input = track_snap["input"].get("raw", 0)
    RPR.SetMediaTrackInfo_Value(track_id, "I_RECINPUT", raw_input)
    RPR.SetMediaTrackInfo_Value(track_id, "I_RECMON", track_snap.get("input_monitoring", 0))

    # Per-item gain: loudness-matched or blind copy.
    if not skip_items and track_snap["items"]:
        n_items = RPR.CountTrackMediaItems(track_id)
        for item_snap in track_snap["items"]:
            if item_snap["index"] < n_items:
                item_id = RPR.GetTrackMediaItem(track_id, item_snap["index"])

                if match_clip_levels and item_snap.get("peak_dbfs") is not None:
                    # Measure the new clip's current peak and adjust gain so it
                    # matches the snapshot's peak level (hitting FX chain similarly).
                    try:
                        new_peak = RPR.NF_GetMediaItemMaxPeak(item_id)
                        snap_peak = item_snap["peak_dbfs"]
                        if new_peak > -150.0 and snap_peak > -150.0:
                            gain_db = snap_peak - new_peak
                        else:
                            gain_db = item_snap["gain_db"]
                    except Exception:
                        gain_db = item_snap["gain_db"]
                else:
                    gain_db = item_snap["gain_db"]

                RPR.SetMediaItemInfo_Value(item_id, "D_VOL", _db_to_linear(gain_db))
                if item_snap.get("fade_in", 0) > 0:
                    RPR.SetMediaItemInfo_Value(item_id, "D_FADEINLEN", item_snap["fade_in"])
                if item_snap.get("fade_out", 0) > 0:
                    RPR.SetMediaItemInfo_Value(item_id, "D_FADEOUTLEN", item_snap["fade_out"])
                report["items_adjusted"] += 1


def register_tools(mcp):

    @mcp.tool()
    def snapshot_session(artist: str = "", song: str = "") -> dict:
        """
        Capture the full state of the current REAPER project as a JSON-serializable
        snapshot: every track's volume/pan/color/folder/input/automation, every item's
        gain/fades/pitch/rate, every FX with all parameter values + preset name, every
        send with destination/volume/pan/type/phase, and the master FX chain + volume.

        Non-destructive: only reads. Returns the snapshot under the "snapshot" key plus
        a summary. Persist it to snapshots/<artist>/<song>-snapshot.json via the
        snapshot-session skill.

        Args:
            artist: Artist name (used for storage path / identification).
            song: Song name (used for storage path / identification).
        """
        try:
            project = get_project()
            with reapy.inside_reaper():
                proj_name = project.name
                proj_path = project.path
                bpm = project.bpm
                # REAPER's time signature API returns (tempo, beats_per_measure),
                # not a traditional numerator/denominator. 4/4 → (bpm, 4.0).
                _, tempo_raw, beats_per_measure = RPR.GetProjectTimeSignature2(
                    project.id, 0.0, 0.0
                )
                # GetSetProjectInfo returns 0 through the distant API (pointer
                # serialization issue); leave sample_rate as None if unavailable.
                sample_rate = RPR.GetSetProjectInfo(
                    project.id, "SAMPLE_RATE", 0.0, False
                )
                if sample_rate == 0:
                    sample_rate = None
                n_tracks = project.n_tracks

                tracks = []
                for i in range(n_tracks):
                    track = project.tracks[i]
                    tracks.append(_read_track(track, i))

                master = _read_master(project)

            snapshot = {
                "version": SNAPSHOT_VERSION,
                "artist": artist,
                "song": song,
                "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "project": {
                    "name": proj_name,
                    "path": proj_path,
                    "tempo": float(bpm),
                    "beats_per_measure": int(beats_per_measure) if beats_per_measure else 4,
                    "sample_rate": sample_rate,
                },
                "master": master,
                "tracks": tracks,
            }

            # Summary counts for quick inspection.
            n_fx = sum(len(t["fx"]) for t in tracks)
            n_sends = sum(len(t["sends"]) for t in tracks)
            n_items = sum(t["n_items"] for t in tracks)
            n_params = sum(
                len(fx["params"]) for t in tracks for fx in t["fx"]
            ) + sum(len(fx["params"]) for fx in master["fx"])
            roles = {}
            for t in tracks:
                roles[t["role"]] = roles.get(t["role"], 0) + 1

            return {
                "success": True,
                "snapshot": snapshot,
                "summary": {
                    "track_count": len(tracks),
                    "item_count": n_items,
                    "fx_count": n_fx + len(master["fx"]),
                    "param_count": n_params,
                    "send_count": n_sends,
                    "roles": roles,
                    "master_fx_count": len(master["fx"]),
                },
            }
        except Exception as e:
            logger.error(f"snapshot_session failed: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def export_fxchains(snapshot_path: str, output_dir: str = "") -> dict:
        """
        Export sidecar .rfxchain files from a snapshot's embedded FX chain chunks.

        Writes one .rfxchain file per track (and master) to the output directory.
        These files can be manually loaded in REAPER via right-click FX chain >
        Load FX chain, providing a human-inspectable backup of each track's FX.

        Args:
            snapshot_path: Path to the snapshot JSON file.
            output_dir: Directory to write .rfxchain files. Defaults to
                <snapshot_dir>/<song>-fxchains/.
        """
        try:
            if not os.path.exists(snapshot_path):
                return {"success": False, "error": f"Snapshot not found: {snapshot_path}"}

            with open(snapshot_path) as f:
                snapshot = json.load(f)

            if not output_dir:
                snap_dir = os.path.dirname(snapshot_path)
                song = snapshot.get("song", "snapshot")
                output_dir = os.path.join(snap_dir, f"{song}-fxchains")

            os.makedirs(output_dir, exist_ok=True)

            exported = []
            skipped = 0

            for track in snapshot.get("tracks", []):
                chunk_b64 = track.get("fxchain_chunk")
                if not chunk_b64:
                    skipped += 1
                    continue
                fxchain_text = _decode_fxchain_chunk(chunk_b64)
                if not fxchain_text:
                    skipped += 1
                    continue
                name = track.get("name", f"track_{track['index']}") or f"track_{track['index']}"
                safe_name = name.replace("/", "_").replace(":", "_").strip()
                filename = f"{track['index']:03d}_{safe_name}.rfxchain"
                filepath = os.path.join(output_dir, filename)
                with open(filepath, "w") as f:
                    f.write(fxchain_text)
                exported.append(filename)

            master = snapshot.get("master", {})
            master_chunk = master.get("fxchain_chunk")
            if master_chunk:
                fxchain_text = _decode_fxchain_chunk(master_chunk)
                if fxchain_text:
                    filepath = os.path.join(output_dir, "MASTER.rfxchain")
                    with open(filepath, "w") as f:
                        f.write(fxchain_text)
                    exported.append("MASTER.rfxchain")

            return {
                "success": True,
                "output_dir": output_dir,
                "exported_count": len(exported),
                "skipped_count": skipped,
                "files": exported,
            }
        except Exception as e:
            logger.error(f"export_fxchains failed: {e}")
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Phase 3: Small composable write tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def set_item_gain(track_index: int, item_index: int, gain_db: float) -> dict:
        """Set the gain of a specific media item (clip) in dB."""
        try:
            project = get_project()
            track = project.tracks[track_index]
            n_items = RPR.CountTrackMediaItems(track.id)
            if item_index >= n_items:
                return {"success": False, "error": f"Item {item_index} not found (track has {n_items} items)"}
            item_id = RPR.GetTrackMediaItem(track.id, item_index)
            RPR.SetMediaItemInfo_Value(item_id, "D_VOL", _db_to_linear(gain_db))
            readback = _linear_to_db(RPR.GetMediaItemInfo_Value(item_id, "D_VOL"))
            return {"success": True, "track_index": track_index, "item_index": item_index, "gain_db": readback}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_track_folder_depth(track_index: int, folder_depth: int) -> dict:
        """
        Set track folder depth: 1 = folder start, 0 = normal, -1 = last in folder.
        Use this to recreate folder hierarchy when prepping a mix.
        """
        try:
            project = get_project()
            track = project.tracks[track_index]
            RPR.SetMediaTrackInfo_Value(track.id, "I_FOLDERDEPTH", folder_depth)
            readback = int(RPR.GetMediaTrackInfo_Value(track.id, "I_FOLDERDEPTH"))
            return {"success": True, "track_index": track_index, "folder_depth": readback}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_send_type(source_track_index: int, send_index: int, send_type: str) -> dict:
        """
        Set the send type/mode: 'post-fader', 'pre-fx', or 'post-fx'.
        """
        try:
            project = get_project()
            track = project.tracks[source_track_index]
            mode = _SENDMODE_RAW.get(send_type)
            if mode is None:
                return {"success": False, "error": f"Unknown send type '{send_type}'. Use: {list(_SENDMODE_RAW.keys())}"}
            RPR.SetTrackSendInfo_Value(track.id, 0, send_index, "I_SENDMODE", mode)
            return {"success": True, "source_track_index": source_track_index, "send_index": send_index, "send_type": send_type}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_send_pan(source_track_index: int, send_index: int, pan: float) -> dict:
        """Set send pan. -1.0 = full left, 0.0 = center, 1.0 = full right."""
        try:
            project = get_project()
            track = project.tracks[source_track_index]
            RPR.SetTrackSendInfo_Value(track.id, 0, send_index, "D_PAN", pan)
            readback = RPR.GetTrackSendInfo_Value(track.id, 0, send_index, "D_PAN")
            return {"success": True, "source_track_index": source_track_index, "send_index": send_index, "pan": float(readback)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_send_phase(source_track_index: int, send_index: int, phase_flipped: bool) -> dict:
        """Set send phase flip state."""
        try:
            project = get_project()
            track = project.tracks[source_track_index]
            RPR.SetTrackSendInfo_Value(track.id, 0, send_index, "B_PHASE", 1 if phase_flipped else 0)
            return {"success": True, "source_track_index": source_track_index, "send_index": send_index, "phase_flipped": phase_flipped}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool()
    def set_master_pan(pan: float) -> dict:
        """Set the master track pan. -1.0 = full left, 0.0 = center, 1.0 = full right."""
        try:
            project = get_project()
            master = project.master_track
            RPR.SetMediaTrackInfo_Value(master.id, "D_PAN", pan)
            readback = RPR.GetMediaTrackInfo_Value(master.id, "D_PAN")
            return {"success": True, "pan": float(readback)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Phase 4: Fat apply tool
    # ------------------------------------------------------------------

    @mcp.tool()
    def apply_snapshot(
        snapshot_path: str,
        track_mapping: dict,
        skip_fx: bool = False,
        skip_sends: bool = False,
        skip_items: bool = False,
        match_clip_levels: bool = True,
        reorder_tracks: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """
        Apply a previously captured session snapshot to the current REAPER project.
        This is the core of the mix-prep workflow: it creates missing tracks, reorders
        tracks to match the snapshot, recreates folder structure, applies track settings,
        FX chains (with all parameters), per-clip gain (loudness-matched), send routing,
        master/parent send state, and the master chain.

        DESTRUCTIVE: overwrites volume, pan, FX parameters, sends, and item gain on
        matched tracks. Always review the track_mapping before applying.

        Args:
            snapshot_path: Path to the snapshot JSON file (from reaper_snapshot_session).
            track_mapping: Dict mapping snapshot track index (as string key) to new
                session track index (int), or "create" to create a new track. Any
                snapshot track NOT in the mapping will be automatically created if it's
                a bus/folder, or skipped with a warning if it's a source track. Example:
                {"0": 5, "1": 6, "81": "create", "82": "create"}
            skip_fx: If True, don't add FX or replay parameters.
            skip_sends: If True, don't recreate sends.
            skip_items: If True, don't adjust per-clip gain.
            match_clip_levels: If True (default), measure each new clip's peak and
                adjust gain so it matches the snapshot clip's peak level. If False,
                blindly copies the snapshot's gain_db values.
            reorder_tracks: If True (default), reorders tracks to match the snapshot's
                track order. This is necessary for folder structure to work correctly.
            dry_run: If True, simulate and report without modifying REAPER.
        """
        try:
            if not os.path.exists(snapshot_path):
                return {"success": False, "error": f"Snapshot file not found: {snapshot_path}"}

            with open(snapshot_path) as f:
                snapshot = json.load(f)

            snapshot_tracks = snapshot.get("tracks", [])
            master_snap = snapshot.get("master", {})

            # Normalize mapping keys to ints, values to int or "create".
            mapping = {}
            for k, v in track_mapping.items():
                key = int(k)
                if v == "create":
                    mapping[key] = "create"
                else:
                    mapping[key] = int(v)

            # Auto-create unmapped bus/folder tracks. Unmapped source tracks are
            # also auto-created (trig tracks, copied tracks, etc. that have no
            # match in the new session but should exist with their settings).
            for snap_track in snapshot_tracks:
                snap_idx = snap_track["index"]
                if snap_idx not in mapping:
                    role = snap_track.get("role", "source")
                    if role in ("bus", "folder", "source", "empty"):
                        mapping[snap_idx] = "create"

            report = {
                "tracks_applied": 0,
                "tracks_created": 0,
                "tracks_reordered": 0,
                "fx_added": 0,
                "fx_skipped": 0,
                "params_set": 0,
                "sends_created": 0,
                "sends_skipped": 0,
                "items_adjusted": 0,
                "master_fx_added": 0,
                "master_params_set": 0,
                "failed": [],
                "warnings": [],
                "dry_run": dry_run,
            }

            project = get_project()

            with reapy.inside_reaper():
                # --- Step 1: Create all tracks marked "create" ---
                # Process in snapshot order so folder hierarchy is preserved.
                for snap_track in snapshot_tracks:
                    snap_idx = snap_track["index"]
                    mapped = mapping.get(snap_idx)
                    if mapped == "create":
                        if dry_run:
                            report["tracks_created"] += 1
                            mapping[snap_idx] = -1
                            continue
                        new_idx = project.n_tracks
                        project.add_track(new_idx, snap_track["name"])
                        mapping[snap_idx] = new_idx
                        report["tracks_created"] += 1

                # --- Step 2: Reorder tracks to match snapshot order ---
                if reorder_tracks and not dry_run:
                    # Build the desired order: list of track IDs in snapshot order.
                    desired_track_ids = []
                    for snap_track in snapshot_tracks:
                        snap_idx = snap_track["index"]
                        new_idx = mapping.get(snap_idx)
                        if new_idx is not None and new_idx >= 0:
                            try:
                                desired_track_ids.append(project.tracks[new_idx].id)
                            except Exception:
                                desired_track_ids.append(None)
                        else:
                            desired_track_ids.append(None)

                    _reorder_tracks(project, desired_track_ids)

                    # Re-resolve mapping by track ID. _reorder_tracks moves tracks
                    # via insert+copy+delete, so track IDs change, but it updates
                    # desired_track_ids in place to reflect the new IDs. We look up
                    # each track's current index by its post-reorder ID. This is
                    # reliable even when the new session's track names differ from
                    # the snapshot (the whole point of prep-mix), unlike a
                    # name-based lookup which fails to match renamed tracks and
                    # falls back to stale pre-reorder indices (scrambling settings).
                    new_mapping = {}
                    for pos, snap_track in enumerate(snapshot_tracks):
                        snap_idx = snap_track["index"]
                        tid = desired_track_ids[pos]
                        if tid is None:
                            old = mapping.get(snap_idx)
                            if old is not None:
                                new_mapping[snap_idx] = old
                            continue
                        found_idx = None
                        for i in range(project.n_tracks):
                            if project.tracks[i].id == tid:
                                found_idx = i
                                break
                        if found_idx is not None:
                            new_mapping[snap_idx] = found_idx
                        else:
                            report["warnings"].append(
                                f"Could not re-resolve track '{snap_track['name']}' after reorder"
                            )
                            old = mapping.get(snap_idx)
                            if old is not None:
                                new_mapping[snap_idx] = old

                    mapping = new_mapping
                    report["tracks_reordered"] = len([v for v in mapping.values() if v is not None and v >= 0])

                # --- Step 3: Apply per-track settings (volume, pan, FX, items) ---
                # Build a set of folder parent indices for send skipping.
                folder_parents = set()
                for snap_track in snapshot_tracks:
                    if snap_track["folder_depth"] == 1:
                        folder_parents.add(snap_track["index"])

                for snap_track in snapshot_tracks:
                    snap_idx = snap_track["index"]
                    new_idx = mapping.get(snap_idx)
                    if new_idx is None:
                        report["warnings"].append(
                            f"Snapshot track [{snap_idx}] '{snap_track['name']}' — explicitly skipped (not in mapping)"
                        )
                        continue
                    if new_idx == "create" or new_idx < 0:
                        # Track is being created — settings will be applied after creation.
                        # In dry-run, we can't apply settings to non-existent tracks.
                        continue

                    try:
                        track = project.tracks[new_idx]
                        _apply_track_settings(
                            track.id, snap_track, report,
                            skip_items=skip_items, dry_run=dry_run,
                            match_clip_levels=match_clip_levels,
                        )
                        if not skip_fx:
                            _apply_fx_chain(
                                track.id, snap_track["fx"], report, dry_run=dry_run,
                                fxchain_chunk=snap_track.get("fxchain_chunk"),
                            )
                        report["tracks_applied"] += 1
                    except Exception as e:
                        report["failed"].append({
                            "item": f"Track [{snap_idx}] '{snap_track['name']}'",
                            "error": str(e),
                        })

                # --- Step 4: Recreate sends (after all tracks exist + reorder) ---
                if not skip_sends:
                    for snap_track in snapshot_tracks:
                        snap_idx = snap_track["index"]
                        new_idx = mapping.get(snap_idx)
                        if new_idx is None or new_idx == "create" or new_idx < 0:
                            continue
                        if not snap_track["sends"]:
                            continue
                        try:
                            track = project.tracks[new_idx]
                            _apply_sends(
                                track.id, snap_track, mapping, report,
                                dry_run=dry_run,
                                folder_parent_indices=folder_parents,
                                project=project,
                            )
                        except Exception as e:
                            report["failed"].append({
                                "item": f"Sends for track [{snap_idx}] '{snap_track['name']}'",
                                "error": str(e),
                            })

                # --- Step 5: Apply master chain ---
                master = project.master_track
                mid = master.id

                # Master volume and pan.
                if not dry_run:
                    RPR.SetMediaTrackInfo_Value(mid, "D_VOL",
                                               _db_to_linear(master_snap["volume_db"]))
                    RPR.SetMediaTrackInfo_Value(mid, "D_PAN", master_snap["pan"])

                # Master FX chain.
                if not skip_fx and master_snap.get("fx"):
                    master_report = {"fx_added": 0, "fx_skipped": 0, "params_set": 0,
                                     "failed": report["failed"]}
                    _apply_fx_chain(mid, master_snap["fx"], master_report,
                                    dry_run=dry_run,
                                    fxchain_chunk=master_snap.get("fxchain_chunk"))
                    report["master_fx_added"] = master_report["fx_added"]
                    report["master_params_set"] = master_report["params_set"]

            return {"success": True, "report": report}
        except Exception as e:
            logger.error(f"apply_snapshot failed: {e}")
            return {"success": False, "error": str(e)}
