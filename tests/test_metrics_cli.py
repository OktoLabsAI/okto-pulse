from __future__ import annotations

import json
from pathlib import Path

from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.cli import cmd_metrics, main
from okto_pulse.core.telemetry.schema import CURRENT_SCHEMA_VERSION


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_community_settings_places_metrics_under_data_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "pulse-data"))
    settings = CommunitySettings()

    assert Path(settings.metrics_dir) == tmp_path / "pulse-data" / "metrics"
    assert Path(settings.kg_base_dir) == tmp_path / "pulse-data"
    assert settings.metrics_beacon_url == "https://metrics.oktolabs.ai"


def test_community_settings_uses_okto_pulse_home_for_all_local_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("KG_BASE_DIR", raising=False)
    monkeypatch.setenv("OKTO_PULSE_HOME", str(tmp_path / "pulse-home"))

    settings = CommunitySettings()

    assert Path(settings.data_dir) == tmp_path / "pulse-home"
    assert Path(settings.kg_base_dir) == tmp_path / "pulse-home"
    assert Path(settings.metrics_dir) == tmp_path / "pulse-home" / "metrics"


def test_metrics_cli_status_defaults_to_off(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "pulse-data"))
    monkeypatch.setenv("OKTO_PULSE_NO_BANNER", "1")

    cmd_metrics(Args(metrics_command="status", window_days=30))

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "disabled"
    assert payload["ui_mode"] == "off"
    assert payload["enabled"] is False
    assert payload["summary"]["event_count"] == 0


def test_metrics_cli_help_hides_legacy_local_only(monkeypatch, capsys) -> None:
    monkeypatch.setenv("OKTO_PULSE_NO_BANNER", "1")
    monkeypatch.setattr("sys.argv", ["okto-pulse", "metrics", "--help"])

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0
    else:  # pragma: no cover
        raise AssertionError("argparse help should exit")

    output = capsys.readouterr().out
    assert "local-only" not in output
    assert "Turn metrics Off" in output
    assert "Turn metrics On with anonymous hourly aggregates" in output


def test_metrics_cli_enable_requires_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "pulse-data"))

    try:
        cmd_metrics(
            Args(
                metrics_command="enable-beacon",
                yes=False,
                policy_version="2026-05-11",
                schema_version=CURRENT_SCHEMA_VERSION,
            )
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("enable-beacon without --yes should exit")


def test_metrics_cli_enable_beacon_persists_acknowledgements(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "pulse-data"))

    cmd_metrics(
        Args(
            metrics_command="enable-beacon",
            yes=True,
            policy_version="2026-05-11",
            schema_version=CURRENT_SCHEMA_VERSION,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    expected_ack = [
        "hourly_aggregates",
        "local_control",
        "no_pii",
        "product_aggregates",
        "privacy_policy",
        "schema",
    ]
    assert payload["mode"] == "anonymous_beacon"
    assert payload["ui_mode"] == "on"
    assert payload["acknowledged_items"] == expected_ack
    persisted = json.loads((tmp_path / "pulse-data" / "metrics" / "state.json").read_text(encoding="utf-8"))
    assert persisted["mode"] == "anonymous_beacon"
    assert persisted["acknowledged_items"] == expected_ack


def test_metrics_cli_legacy_local_only_maps_to_off_export_and_purge(
    tmp_path: Path,
    monkeypatch,
    capsys,
    caplog,
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "pulse-data"))
    caplog.set_level("INFO", logger="okto_pulse.community.metrics.cli")

    cmd_metrics(Args(metrics_command="local-only"))
    state = json.loads(capsys.readouterr().out)
    assert state["mode"] == "disabled"
    assert state["ui_mode"] == "off"
    assert state["legacy_alias"] == "local-only"
    assert state["message"] == "The legacy local-only command is deprecated; metrics are now Off."
    persisted = json.loads((tmp_path / "pulse-data" / "metrics" / "state.json").read_text(encoding="utf-8"))
    assert persisted["mode"] == "disabled"
    assert "local_only" not in json.dumps({"mode": persisted["mode"]})
    assert any(
        record.__dict__.get("metric_name") == "metrics_cli_legacy_local_only_total"
        and record.__dict__.get("outcome") == "mapped_to_disabled"
        for record in caplog.records
    )

    cmd_metrics(Args(metrics_command="export", output=None))
    exported = json.loads(capsys.readouterr().out)
    assert Path(exported["output_path"]).exists()

    cmd_metrics(Args(metrics_command="purge-local", yes=True))
    purged = json.loads(capsys.readouterr().out)
    assert "purged_files" in purged
