"""Refuse legacy outcome reconstruction before touching a persisted state."""
import json
import pytest
from okto_pulse.community.adapters.telemetry_state import load_state, save_state


@pytest.mark.parametrize("state", [
    {"mode": "anonymous_beacon", "last_send_at": "2026-06-01T00:00:00Z"},
    {"mode": "anonymous_beacon", "next_batch_seq": 7},
    {"mode": "anonymous_beacon", "next_batch_seq": 7, "failure_state": {"status": "ok"}},
    {"mode": "anonymous_beacon", "next_batch_seq": 7, "watermark": None, "watermark_event_id": None},
    {"mode": "disabled", "failure_state": None},
    {"mode": "disabled", "next_batch_seq": "7"},
])
def test_incompatible_recovery_state_cannot_be_read_or_overwritten(tmp_path, state):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        load_state(tmp_path)
    with pytest.raises(ValueError):
        save_state(tmp_path, {"mode": "disabled"})
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
