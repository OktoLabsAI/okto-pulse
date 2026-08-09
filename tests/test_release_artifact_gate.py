"""Acceptance test for the independent paired-wheel release artifact gate."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "release_artifact_gate.py"


def test_ts24_release_harness_freezes_installed_inventory_and_provenance() -> None:
    spec = importlib.util.spec_from_file_location("release_artifact_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.EXPECTED_VERSION == "0.3.2"
    assert module.EXPECTED_MCP_TOOL_COUNT == 313
    assert module.EXPECTED_CANONICAL_TOOL_COUNT == 305
    assert module.EXPECTED_TOOL_ALIAS_COUNT == 8
    assert module.EXPECTED_RESOURCE_COUNT == 53
    assert module.MINIMUM_SUPPORTED_PYTHON == (3, 11)
    assert module.CORE_REPO.name in {
        "okto-pulse-core",
        "okto_labs_pulse_core",
    }
    assert "site-packages" not in str(module.CORE_REPO).lower()
    forbidden_names = {path.name for path in module.FORBIDDEN_CHECKOUT_ROOTS}
    assert "okto_labs_pulse_core" in forbidden_names
    assert "okto_labs_pulse_community" in forbidden_names
    assert "ska_tool_manifest.json" in module._INSTALLED_ORIGIN_PROBE
    assert "ska_resource_manifest.json" in module._INSTALLED_ORIGIN_PROBE
    assert "__EXPECTED_" not in module._MCP_CLIENT_PROBE
    assert "installed runtime" in module._INSTALLED_RUNTIME_VERSION_PROBE
    assert 'metadata.version("pydantic")' in (
        module._INSTALLED_RUNTIME_VERSION_PROBE
    )
    assert module.RUNTIME_MATRIX_PROBE.is_file()
    runtime_probe = module.RUNTIME_MATRIX_PROBE.read_text(encoding="utf-8")
    assert '"coverage": "TS24-C11 covered"' in runtime_probe
    assert '"productive_kuzu_materialized": False' in runtime_probe
    assert '"productive_kuzu_purged": False' in runtime_probe
    assert '"family_projection_hashes": family_hashes' in runtime_probe
    assert "third_rerun_snapshot" in runtime_probe


def test_release_gate_resolves_core_from_explicit_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    expected = workspace / "okto-pulse-core"
    (expected / "src" / "okto_pulse" / "core").mkdir(parents=True)
    (expected / "pyproject.toml").write_text(
        "[project]\nname = 'okto-pulse-core'\nversion = '0.0.0'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OKTO_PULSE_CORE_REPO", raising=False)
    monkeypatch.setenv("OKTO_PULSE_WORKSPACE_ROOT", str(workspace))

    spec = importlib.util.spec_from_file_location(
        "release_artifact_gate_workspace_resolution",
        GATE,
    )
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    assert gate.CORE_REPO == expected.resolve()


def test_release_gate_rejects_invalid_explicit_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OKTO_PULSE_CORE_REPO", raising=False)
    monkeypatch.setenv("OKTO_PULSE_WORKSPACE_ROOT", str(tmp_path))

    spec = importlib.util.spec_from_file_location(
        "release_artifact_gate_invalid_workspace",
        GATE,
    )
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    with pytest.raises(
        RuntimeError,
        match="OKTO_PULSE_WORKSPACE_ROOT contains no valid Core checkout",
    ):
        spec.loader.exec_module(gate)


@pytest.mark.skipif(
    os.environ.get("OKTO_SKIP_RELEASE_ARTIFACT_GATE") == "1",
    reason="explicit operator opt-out: OKTO_SKIP_RELEASE_ARTIFACT_GATE=1",
)
def test_fresh_wheels_install_and_serve_from_isolated_venv(tmp_path: Path) -> None:
    work_dir = tmp_path / "release-artifact-gate"
    command = [sys.executable, str(GATE), "--work-dir", str(work_dir)]
    if os.environ.get("OKTO_RELEASE_ARTIFACT_OFFLINE") == "1":
        command.append("--offline")

    completed = subprocess.run(
        command,
        cwd=REPO,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1200,
    )

    assert completed.returncode == 0, (
        f"release artifact gate failed\nstdout={completed.stdout[-4000:]}\n"
        f"stderr={completed.stderr[-4000:]}"
    )
    results = [
        line.removeprefix("RELEASE_ARTIFACT_GATE=")
        for line in completed.stdout.splitlines()
        if line.startswith("RELEASE_ARTIFACT_GATE=")
    ]
    assert len(results) == 1, completed.stdout[-4000:]
    evidence = json.loads(results[0])

    assert evidence["status"] == "passed"
    assert evidence["expected_version"] == "0.3.2"
    assert evidence["installed"]["runtime_version"]["python_major_minor"] == [3, 11]
    assert evidence["installed"]["runtime_version"]["required_major_minor"] == [3, 11]
    assert evidence["installed"]["runtime_version"]["pydantic"]
    assert evidence["core_artifact_audit"]["forbidden_wheel_paths"] == []
    assert evidence["core_artifact_audit"]["missing_required_resources"] == []
    origin = evidence["installed"]["origin_probe"]
    assert origin["about_version"] == "0.3.2"
    assert origin["ska_contract_manifests"]["tool_count"] == 11
    assert origin["ska_contract_manifests"]["resource_count"] == 21
    assert len(origin["required_core_resources"]) == 2
    for distribution in ("core", "community"):
        provenance = evidence["installed"]["payload_provenance"][distribution]
        assert provenance["version"] == "0.3.2"
        assert provenance["commit"]
        assert provenance["repository_root"]
        assert provenance["wheel"]["sha256"]
        assert provenance["byte_identical"] is True
        assert provenance["byte_identical_member_count"] > 0
        assert len(set(provenance["chain_hashes"].values())) == 1
        assert provenance["mismatches"] == []
    runtime_matrix = evidence["installed"]["runtime_matrix"]
    assert runtime_matrix["status"] == "passed"
    sqlite = runtime_matrix["sqlite"]
    assert sqlite["fresh"]["foreign_key_errors"] == []
    assert (
        sqlite["rerun"]["logical_sha256"]
        == sqlite["fresh"]["logical_sha256"]
    )
    assert (
        sqlite["crash_resume"]["logical_sha256"]
        == sqlite["fresh"]["logical_sha256"]
    )
    concurrency = sqlite["concurrency"]
    assert concurrency["returncodes"] == [0, 0]
    assert concurrency["both_workers_succeeded"] is True
    assert concurrency["hash_identical_to_single"] is True
    assert (
        concurrency["third_rerun_snapshot"]["logical_sha256"]
        == concurrency["single_start_baseline"]["logical_sha256"]
    )
    assert sqlite["non_sqlite"]["rejected"] is True
    assert sqlite["non_sqlite"]["before_bootstrap"] is True
    kg_parity = runtime_matrix["kg_parity"]
    assert kg_parity["coverage"] == "TS24-C11 covered"
    assert kg_parity["artifact_families"] == [
        "story",
        "ideation",
        "refinement",
        "spec",
        "sprint",
        "card",
    ]
    assert kg_parity["artifact_family_count"] == 6
    assert kg_parity["mismatches"] == []
    assert kg_parity["oracle"]["productive_kuzu_materialized"] is False
    assert kg_parity["oracle"]["productive_kuzu_purged"] is False
    assert all(
        family["match"]
        and family["incremental_sha256"] == family["rebuild_sha256"]
        for family in kg_parity["family_projection_hashes"].values()
    )
    assert kg_parity["ska"]["quality"] == "covered"
    assert kg_parity["ska"]["research_decision_ledger"] == "covered"
    assert evidence["installed"]["cli_version"] == (
        "okto-pulse 0.3.2 (okto-pulse-core 0.3.2)"
    )
    mcp_http = evidence["installed"]["mcp_http"]
    assert mcp_http["transport"] == "streamable-http-loopback"
    assert mcp_http["tool_count"] == 313
    assert mcp_http["canonical_tool_count"] == 304
    assert mcp_http["tool_alias_count"] == 8
    assert mcp_http["resource_count"] == 53
    assert mcp_http["ska_tool_count"] == 11
    assert (work_dir / "release-artifact-evidence.json").is_file()
