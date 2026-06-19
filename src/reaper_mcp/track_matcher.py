"""Fuzzy track-name matcher for the prep-mix workflow.

Matches snapshot track names against new session track names using
difflib.SequenceMatcher with name normalization. Designed to be
importable for unit tests and called by scripts/match_tracks.py.
"""

import re
from difflib import SequenceMatcher

# Common suffixes/prefixes to strip during normalization.
_NOISE_TOKENS = {"track", "trk", "audio", "ch", "channel", "bus", "grp", "group"}

# Threshold above which a match is considered "auto" (user still confirms).
AUTO_MATCH_THRESHOLD = 0.60


def normalize_name(name: str) -> str:
    """Normalize a track name for fuzzy matching.

    - Lowercase
    - Strip leading numbers (e.g. "01 Kick" -> "kick")
    - Remove punctuation (underscores become spaces)
    - Remove common noise tokens (track, trk, audio, etc.)
    - Strip whitespace
    """
    s = name.lower().strip()
    # Strip leading numbers and separators.
    s = re.sub(r"^\d+[\s._\-]*", "", s)
    # Replace underscores and punctuation with spaces.
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"_", " ", s)
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    # Remove noise tokens.
    tokens = [t for t in s.split() if t not in _NOISE_TOKENS]
    return " ".join(tokens)


def similarity(a: str, b: str) -> float:
    """Compute similarity ratio between two normalized names."""
    na = normalize_name(a)
    nb = normalize_name(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    # Direct ratio.
    ratio = SequenceMatcher(None, na, nb).ratio()
    # Also check if one is a substring of the other (e.g. "kick" in "kick drum").
    if na in nb or nb in na:
        ratio = max(ratio, 0.75)
    return ratio


def match_tracks(
    snapshot_tracks: list,
    new_tracks: list,
    threshold: float = AUTO_MATCH_THRESHOLD,
) -> dict:
    """Match snapshot tracks to new session tracks.

    Uses greedy best-match with role priority: source tracks are matched
    first (they have the actual audio), then folders, then buses, then
    empty tracks. This prevents a folder named "KICK" from stealing the
    match from a source track named "Kick 1".

    Args:
        snapshot_tracks: List of dicts with at least "index" and "name" keys.
        new_tracks: List of dicts with at least "index" and "name" keys.
        threshold: Minimum similarity score for auto-matching.

    Returns:
        {
            "auto_matches": [{"snap_index": int, "snap_name": str,
                              "new_index": int, "new_name": str, "score": float}, ...],
            "unmatched_snapshot": [{"snap_index": int, "snap_name": str,
                                    "snap_role": str, "candidates": [...]}, ...],
            "unmatched_new": [{"new_index": int, "new_name": str}, ...],
        }
    """
    # Sort snapshot tracks by role priority so sources match first.
    role_priority = {"source": 0, "folder": 1, "bus": 2, "empty": 3}
    ordered_snaps = sorted(
        snapshot_tracks,
        key=lambda t: (role_priority.get(t.get("role", "source"), 0), t["index"]),
    )

    auto_matches = []
    used_new_indices = set()

    for snap in ordered_snaps:
        snap_idx = snap["index"]
        snap_name = snap["name"]
        snap_role = snap.get("role", "source")

        best_score = 0.0
        best_new_idx = None
        best_new_name = None

        for new in new_tracks:
            new_idx = new["index"]
            if new_idx in used_new_indices:
                continue
            score = similarity(snap_name, new["name"])
            if score > best_score:
                best_score = score
                best_new_idx = new_idx
                best_new_name = new["name"]

        if best_score >= threshold and best_new_idx is not None:
            auto_matches.append({
                "snap_index": snap_idx,
                "snap_name": snap_name,
                "snap_role": snap_role,
                "new_index": best_new_idx,
                "new_name": best_new_name,
                "score": round(best_score, 3),
            })
            used_new_indices.add(best_new_idx)

    # Sort auto-matches back into snapshot index order for display.
    auto_matches.sort(key=lambda m: m["snap_index"])

    # Unmatched snapshot tracks (with top candidates for manual mapping).
    matched_snap_indices = {m["snap_index"] for m in auto_matches}
    unmatched_snapshot = []
    for snap in snapshot_tracks:
        if snap["index"] in matched_snap_indices:
            continue
        # Find top 3 candidates from unused new tracks.
        candidates = []
        for new in new_tracks:
            if new["index"] in used_new_indices:
                continue
            score = similarity(snap["name"], new["name"])
            if score > 0.1:
                candidates.append({
                    "new_index": new["index"],
                    "new_name": new["name"],
                    "score": round(score, 3),
                })
        candidates.sort(key=lambda c: c["score"], reverse=True)
        unmatched_snapshot.append({
            "snap_index": snap["index"],
            "snap_name": snap["name"],
            "snap_role": snap.get("role", "source"),
            "candidates": candidates[:3],
        })

    # Unmatched new tracks.
    unmatched_new = [
        {"new_index": new["index"], "new_name": new["name"]}
        for new in new_tracks
        if new["index"] not in used_new_indices
    ]

    return {
        "auto_matches": auto_matches,
        "unmatched_snapshot": unmatched_snapshot,
        "unmatched_new": unmatched_new,
    }


def build_mapping_dict(
    auto_matches: list,
    manual_mappings: dict = None,
    create_tracks: list = None,
) -> dict:
    """Build the track_mapping dict for reaper_apply_snapshot.

    Args:
        auto_matches: List from match_tracks()["auto_matches"].
        manual_mappings: Dict of snap_index -> new_index (user-confirmed).
        create_tracks: List of snap_indices to create as new tracks.

    Returns:
        Dict mapping string snap_index -> int new_index or "create".
    """
    mapping = {}
    for m in auto_matches:
        mapping[str(m["snap_index"])] = m["new_index"]
    if manual_mappings:
        for k, v in manual_mappings.items():
            mapping[str(k)] = int(v)
    if create_tracks:
        for idx in create_tracks:
            mapping[str(idx)] = "create"
    return mapping
