"""Tests for the fuzzy track matcher.

These tests don't require REAPER — they test the pure Python matching logic.
Run with: .venv/bin/python -m pytest tests/ -v
"""

import pytest
from reaper_mcp.track_matcher import (
    normalize_name,
    similarity,
    match_tracks,
    build_mapping_dict,
)


class TestNormalizeName:
    def test_lowercases(self):
        assert normalize_name("KICK DRUM") == "kick drum"

    def test_strips_leading_numbers(self):
        assert normalize_name("01 Kick") == "kick"
        assert normalize_name("12. Snare Top") == "snare top"

    def test_removes_punctuation(self):
        assert normalize_name("Kick_In_Drum") == "kick in drum"
        assert normalize_name("Kick (Sub)") == "kick sub"

    def test_removes_noise_tokens(self):
        assert normalize_name("Kick Track") == "kick"
        assert normalize_name("Audio Track 01") == "01"
        assert normalize_name("Snare TRK") == "snare"

    def test_empty_string(self):
        assert normalize_name("") == ""

    def test_only_numbers(self):
        assert normalize_name("123") == ""


class TestSimilarity:
    def test_identical(self):
        assert similarity("Kick", "Kick") == 1.0

    def test_case_insensitive(self):
        assert similarity("KICK", "kick") == 1.0

    def test_substring(self):
        score = similarity("kick", "kick drum")
        assert score >= 0.75

    def test_unrelated(self):
        score = similarity("kick", "vocal")
        assert score < 0.4

    def test_both_empty(self):
        assert similarity("", "") == 1.0

    def test_one_empty(self):
        assert similarity("kick", "") == 0.0

    def test_abbreviations(self):
        score = similarity("KD", "kick drum")
        # Should have some positive similarity even if not high.
        assert score > 0.0

    def test_snare_variants(self):
        score = similarity("Sn Top", "snare top")
        assert score >= 0.5


class TestMatchTracks:
    @pytest.fixture
    def snapshot_tracks(self):
        return [
            {"index": 0, "name": "KICK", "role": "folder"},
            {"index": 1, "name": "Kick 1", "role": "source"},
            {"index": 2, "name": "Sn Top", "role": "source"},
            {"index": 3, "name": "Acoustic Gtr", "role": "source"},
            {"index": 4, "name": "Para 1176", "role": "bus"},
            {"index": 5, "name": "VintageVrb", "role": "bus"},
        ]

    @pytest.fixture
    def new_tracks(self):
        return [
            {"index": 0, "name": "Kick Drum"},
            {"index": 1, "name": "Snare Top"},
            {"index": 2, "name": "Acoustic Guitar"},
            {"index": 3, "name": "Bass DI"},
            {"index": 4, "name": "Lead Vocal"},
        ]

    def test_auto_matches_found(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        assert len(result["auto_matches"]) > 0

    def test_kick_matches(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        kick_match = [m for m in result["auto_matches"] if m["snap_index"] == 1]
        assert len(kick_match) == 1
        assert kick_match[0]["new_index"] == 0  # -> Kick Drum

    def test_snare_matches(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        snare_match = [m for m in result["auto_matches"] if m["snap_index"] == 2]
        assert len(snare_match) == 1
        assert snare_match[0]["new_index"] == 1  # -> Snare Top

    def test_acoustic_matches(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        gtr_match = [m for m in result["auto_matches"] if m["snap_index"] == 3]
        assert len(gtr_match) == 1
        assert gtr_match[0]["new_index"] == 2  # -> Acoustic Guitar

    def test_bus_unmatched(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        bus_indices = {m["snap_index"] for m in result["unmatched_snapshot"]}
        assert 4 in bus_indices  # Para 1176
        assert 5 in bus_indices  # VintageVrb

    def test_unmatched_new_tracks(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        new_indices = {u["new_index"] for u in result["unmatched_new"]}
        # Bass DI and Lead Vocal should be unmatched.
        assert 3 in new_indices
        assert 4 in new_indices

    def test_no_duplicate_new_assignments(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        new_indices = [m["new_index"] for m in result["auto_matches"]]
        assert len(new_indices) == len(set(new_indices))

    def test_scores_included(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        for m in result["auto_matches"]:
            assert "score" in m
            assert 0.0 <= m["score"] <= 1.0

    def test_unmatched_snapshot_has_candidates(self, snapshot_tracks, new_tracks):
        result = match_tracks(snapshot_tracks, new_tracks)
        for u in result["unmatched_snapshot"]:
            assert "candidates" in u
            assert isinstance(u["candidates"], list)


class TestBuildMappingDict:
    def test_auto_matches_only(self):
        auto = [
            {"snap_index": 0, "new_index": 5},
            {"snap_index": 1, "new_index": 6},
        ]
        mapping = build_mapping_dict(auto)
        assert mapping == {"0": 5, "1": 6}

    def test_with_manual_mappings(self):
        auto = [{"snap_index": 0, "new_index": 5}]
        manual = {2: 10, 3: 11}
        mapping = build_mapping_dict(auto, manual_mappings=manual)
        assert mapping == {"0": 5, "2": 10, "3": 11}

    def test_with_create_tracks(self):
        auto = [{"snap_index": 0, "new_index": 5}]
        mapping = build_mapping_dict(auto, create_tracks=[10, 11])
        assert mapping == {"0": 5, "10": "create", "11": "create"}

    def test_manual_overrides_auto(self):
        auto = [{"snap_index": 0, "new_index": 5}]
        manual = {0: 99}
        mapping = build_mapping_dict(auto, manual_mappings=manual)
        assert mapping["0"] == 99
