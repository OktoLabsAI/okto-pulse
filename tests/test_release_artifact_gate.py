"""Acceptance test for the independent paired-wheel release artifact gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "release_artifact_gate.py"


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
    assert evidence["expected_version"] == "0.3.0"
    assert evidence["core_artifact_audit"]["forbidden_wheel_paths"] == []
    assert evidence["installed"]["origin_probe"]["about_version"] == "0.3.0"
    assert evidence["installed"]["cli_version"] == (
        "okto-pulse 0.3.0 (okto-pulse-core 0.3.0)"
    )
    assert evidence["installed"]["mcp_http"]["transport"] == (
        "streamable-http-loopback"
    )
    assert evidence["installed"]["mcp_http"]["tool_count"] == 276
    assert (work_dir / "release-artifact-evidence.json").is_file()
