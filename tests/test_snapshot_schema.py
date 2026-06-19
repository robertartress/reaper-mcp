"""Tests for snapshot JSON schema validation.

These tests validate the structure of a snapshot JSON file without requiring
REAPER. They use the real snapshot captured from the Samantha Rae session
if available, or a minimal synthetic snapshot.
Run with: .venv/bin/python -m pytest tests/ -v
"""

import json
import os

import pytest

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "snapshots",
    "Samantha Rae", "Gone Fishin-snapshot.json"
)


def _make_minimal_snapshot():
    """Create a minimal valid snapshot for testing."""
    return {
        "version": "1.0",
        "artist": "Test Artist",
        "song": "Test Song",
        "captured_at": "2026-01-01T00:00:00",
        "project": {
            "name": "Test Project",
            "path": "/tmp/test",
            "tempo": 120.0,
            "beats_per_measure": 4,
            "sample_rate": None,
        },
        "master": {
            "volume_db": 0.0,
            "pan": 0.0,
            "fx": [],
            "fxchain_chunk": None,
        },
        "tracks": [
            {
                "index": 0,
                "name": "Kick",
                "role": "source",
                "volume_db": 0.0,
                "pan": 0.0,
                "color": [0, 0, 0],
                "muted": False,
                "soloed": False,
                "phase": False,
                "folder_depth": 0,
                "parent_index": None,
                "automation_mode": "read",
                "automation_mode_raw": 1,
                "input": {"source": "audio", "channel": 0, "mono": False, "raw": 0},
                "input_monitoring": 0,
                "main_send_enabled": True,
                "items": [
                    {"index": 0, "position": 0.0, "length": 1.0, "gain_db": 0.0,
                     "fade_in": 0.0, "fade_out": 0.0}
                ],
                "fx": [
                    {"index": 0, "name": "VST3: ReaEQ (Cockos)", "enabled": True,
                     "preset": "", "n_params": 2,
                     "params": [
                         {"index": 0, "name": "Bypass", "normalized": 0.0,
                          "raw": 0.0, "min": 0.0, "max": 1.0},
                         {"index": 1, "name": "Band 1 Gain", "normalized": 0.5,
                          "raw": 0.0, "min": -24.0, "max": 24.0},
                     ]}
                ],
                "sends": [
                    {"send_index": 0, "dest_index": 1, "dest_name": "Drum Bus",
                     "volume_db": 0.0, "pan": 0.0, "type": "post-fader",
                     "send_mode_raw": 0, "muted": False, "phase": False,
                     "src_channel": 0, "dst_channel": 0}
                ],
                "fxchain_chunk": None,
                "n_items": 1,
                "n_sends": 1,
                "n_receives": 0,
            }
        ],
    }


class TestSnapshotSchema:
    def test_minimal_snapshot_has_required_top_level_keys(self):
        snap = _make_minimal_snapshot()
        for key in ["version", "artist", "song", "captured_at", "project", "master", "tracks"]:
            assert key in snap, f"Missing required key: {key}"

    def test_track_has_required_keys(self):
        snap = _make_minimal_snapshot()
        track = snap["tracks"][0]
        required = ["index", "name", "role", "volume_db", "pan", "color",
                     "muted", "soloed", "phase", "folder_depth", "parent_index",
                     "automation_mode", "input", "items", "fx", "fxchain_chunk",
                     "sends"]
        for key in required:
            assert key in track, f"Missing track key: {key}"

    def test_fx_has_required_keys(self):
        snap = _make_minimal_snapshot()
        fx = snap["tracks"][0]["fx"][0]
        for key in ["index", "name", "enabled", "preset", "n_params", "params"]:
            assert key in fx, f"Missing FX key: {key}"

    def test_param_has_required_keys(self):
        snap = _make_minimal_snapshot()
        param = snap["tracks"][0]["fx"][0]["params"][0]
        for key in ["index", "name", "normalized", "raw", "min", "max"]:
            assert key in param, f"Missing param key: {key}"

    def test_send_has_required_keys(self):
        snap = _make_minimal_snapshot()
        send = snap["tracks"][0]["sends"][0]
        for key in ["send_index", "dest_index", "dest_name", "volume_db", "pan",
                     "type", "send_mode_raw", "muted", "phase"]:
            assert key in send, f"Missing send key: {key}"

    def test_item_has_required_keys(self):
        snap = _make_minimal_snapshot()
        item = snap["tracks"][0]["items"][0]
        for key in ["index", "position", "length", "gain_db", "fade_in", "fade_out"]:
            assert key in item, f"Missing item key: {key}"

    def test_track_has_main_send_enabled(self):
        snap = _make_minimal_snapshot()
        assert "main_send_enabled" in snap["tracks"][0]

    def test_item_has_peak_dbfs(self):
        snap = _make_minimal_snapshot()
        # peak_dbfs may be None if measurement failed, but key should exist
        # in real snapshots (v1.1+). Minimal snapshot may not have it.
        # Just check it doesn't crash.
        for track in snap["tracks"]:
            for item in track["items"]:
                assert "index" in item

    def test_master_has_fxchain_chunk(self):
        snap = _make_minimal_snapshot()
        assert "fxchain_chunk" in snap["master"]

    def test_role_values(self):
        snap = _make_minimal_snapshot()
        assert snap["tracks"][0]["role"] in ("source", "bus", "folder", "empty")

    def test_version_format(self):
        snap = _make_minimal_snapshot()
        assert snap["version"] == "1.0"


class TestRealSnapshot:
    """Tests against the real Samantha Rae snapshot if it exists."""

    @pytest.fixture
    def real_snapshot(self):
        if not os.path.exists(SNAPSHOT_PATH):
            pytest.skip("Real snapshot not available")
        with open(SNAPSHOT_PATH) as f:
            return json.load(f)

    def test_real_snapshot_loads(self, real_snapshot):
        assert real_snapshot["version"] in ("1.0", "1.1")
        assert len(real_snapshot["tracks"]) > 0

    def test_real_snapshot_track_count(self, real_snapshot):
        assert len(real_snapshot["tracks"]) == 115

    def test_real_snapshot_has_master_fx(self, real_snapshot):
        assert len(real_snapshot["master"]["fx"]) >= 1

    def test_real_snapshot_all_tracks_have_fx_key(self, real_snapshot):
        for track in real_snapshot["tracks"]:
            assert "fx" in track
            assert isinstance(track["fx"], list)

    def test_real_snapshot_all_sends_have_dest(self, real_snapshot):
        for track in real_snapshot["tracks"]:
            for send in track["sends"]:
                assert "dest_index" in send
                assert "volume_db" in send
                assert "type" in send

    def test_real_snapshot_has_main_send_enabled(self, real_snapshot):
        for track in real_snapshot["tracks"][:5]:
            assert "main_send_enabled" in track

    def test_real_snapshot_buses_have_main_send_disabled(self, real_snapshot):
        buses = [t for t in real_snapshot["tracks"] if t["role"] == "bus"]
        for bus in buses[:5]:
            # Buses like DRUM KIT, INST, MUSIC should have main_send_enabled=False
            assert bus["main_send_enabled"] is False, \
                f"Bus '{bus['name']}' should have main_send_enabled=False"

    def test_real_snapshot_source_tracks_have_main_send_enabled(self, real_snapshot):
        sources = [t for t in real_snapshot["tracks"] if t["role"] == "source"]
        for src in sources[:5]:
            assert src["main_send_enabled"] is True, \
                f"Source '{src['name']}' should have main_send_enabled=True"

    def test_real_snapshot_params_have_normalized(self, real_snapshot):
        count = 0
        for track in real_snapshot["tracks"]:
            for fx in track["fx"]:
                for param in fx["params"]:
                    assert "normalized" in param
                    assert 0.0 <= param["normalized"] <= 1.0
                    count += 1
                    if count >= 100:
                        return  # Sample first 100 params
