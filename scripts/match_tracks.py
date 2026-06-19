#!/usr/bin/env python3
"""CLI wrapper for the fuzzy track matcher.

Usage:
    python scripts/match_tracks.py <snapshot_path> <new_tracks_json>

The new_tracks_json is a JSON array of {"index": int, "name": str} dicts,
typically obtained from reaper_list_tracks.

Outputs the match results as JSON to stdout.
"""

import json
import sys
import os

# Add src to path for import.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from reaper_mcp.track_matcher import match_tracks, build_mapping_dict


def main():
    if len(sys.argv) < 3:
        print("Usage: match_tracks.py <snapshot_path> <new_tracks_json>", file=sys.stderr)
        sys.exit(1)

    snapshot_path = sys.argv[1]
    new_tracks_json = sys.argv[2]

    with open(snapshot_path) as f:
        snapshot = json.load(f)

    snapshot_tracks = snapshot.get("tracks", [])
    new_tracks = json.loads(new_tracks_json)

    result = match_tracks(snapshot_tracks, new_tracks)

    # Also build the initial mapping dict from auto-matches.
    # Buses and folders with no match should be marked "create".
    auto_indices = {m["snap_index"] for m in result["auto_matches"]}
    create_tracks = []
    for snap in snapshot_tracks:
        if snap["index"] not in auto_indices and snap.get("role") in ("bus", "folder"):
            create_tracks.append(snap["index"])

    mapping = build_mapping_dict(
        result["auto_matches"], create_tracks=create_tracks
    )

    output = {
        "match_results": result,
        "proposed_mapping": mapping,
        "create_tracks": create_tracks,
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
