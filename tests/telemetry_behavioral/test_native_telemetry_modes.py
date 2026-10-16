"""Only current telemetry modes are admitted; refused state is never repaired."""
import json
import sys

import pytest

from okto_pulse.community import cli
from okto_pulse.community.adapters.telemetry_state import load_state, save_state
from okto_pulse.community.adapters.telemetry_runtime import resolve_telemetry_config
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.telemetry.settings import record_consent
from telemetry_behavioral.test_telemetry_local_first import _metrics_client


@pytest.mark.parametrize("state", [
    {"mode": "local_only"}, {"mode": "enable_beacon"},
    {"mode": "disabled", "normalized_from": "local_only"},
    {"mode": "disabled", "migration_notices": {}},
    {"mode": "disabled", "history": [{"mode": "local_only"}]},
    {"mode": "disabled", "history": [{"requested_mode": "local_only"}]},
    [], "{",
])
def test_incompatible_state_is_refused_without_replacement(tmp_path, state):
    folder = tmp_path / "metrics"
    folder.mkdir()
    path = folder / "state.json"
    path.write_text(state if isinstance(state, str) else json.dumps(state))
    before = path.read_bytes()
    settings = CoreSettings(metrics_dir=str(folder), metrics_mode="disabled")
    for action in (
        lambda: load_state(folder),
        lambda: save_state(folder, {"mode": "disabled"}),
        lambda: resolve_telemetry_config(settings),
        lambda: record_consent(settings, mode="disabled", source="cli"),
    ):
        with pytest.raises(ValueError):
            action()
        assert path.read_bytes() == before
        assert sorted(p.name for p in folder.iterdir()) == ["state.json"]


@pytest.mark.parametrize("mode", ["local_only", "enable_beacon", "local-only", "unknown"])
def test_old_mode_inputs_never_create_state(tmp_path, mode):
    folder = tmp_path / "metrics"
    settings = CoreSettings(metrics_dir=str(folder), metrics_mode=mode)
    with pytest.raises(ValueError):
        resolve_telemetry_config(settings)
    with pytest.raises(ValueError):
        record_consent(settings, mode=mode, source="cli")
    assert not folder.exists()


def test_current_state_roundtrip_preserves_native_history_and_extensions(tmp_path):
    state = {"mode": "disabled", "history": [{"mode": "anonymous_beacon", "source": "cli"}],
             "failure_state": {"status": "unknown"}, "native_extension": {"counter": 2}}
    save_state(tmp_path, state)
    assert load_state(tmp_path) == state
    settings = CoreSettings(metrics_dir=str(tmp_path), metrics_mode="")
    config = resolve_telemetry_config(settings)
    assert config.mode == "disabled"
    assert not hasattr(config, "normalized_from")
    assert not hasattr(config, "migration_notice")


@pytest.mark.parametrize("source", ["settings_ui", "cli"])
def test_rest_refuses_removed_mode_before_any_service_write(tmp_path, monkeypatch, source):
    client = _metrics_client(tmp_path, monkeypatch)
    response = client.post("/api/v1/metrics/settings", json={"mode": "local_only", "source": source})
    assert response.status_code == 422
    assert not (tmp_path / "metrics" / "state.json").exists()
    assert client.post("/api/v1/metrics/settings/migration-notice/seen",
                       json={"notice_key": "local_only_to_disabled"}).status_code == 404


def test_cli_rejects_removed_alias_before_accessing_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["okto-pulse", "metrics", "local-only"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not (tmp_path / "metrics").exists()


def test_injected_snapshot_is_subject_to_the_same_admission(tmp_path):
    snapshot = {"mode": "local_only"}
    with pytest.raises(ValueError, match="incompatible_telemetry"):
        resolve_telemetry_config(CoreSettings(metrics_dir=str(tmp_path)), state_snapshot=snapshot)
    assert snapshot == {"mode": "local_only"}
    assert not (tmp_path / "state.json").exists()
