"""R15B — Community produces runtime smoke evidence for the core gate."""

from __future__ import annotations

from datetime import datetime, timezone

from okto_pulse.community.adapters.smoke_evidence import (
    build_community_runtime_smoke_evidence,
)
from okto_pulse.core.application.boundary import (
    CommunityRebuildReinstallSmokeGate,
    CommunitySmokeEvidenceInput,
)


def test_community_runtime_smoke_evidence_passes_core_contract() -> None:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    payload = build_community_runtime_smoke_evidence(
        core_version="0.3.0",
        community_version="0.3.0",
        core_commit="core-sha",
        community_commit="community-sha",
        core_wheel_hash="sha256:core",
        community_wheel_hash="sha256:community",
        routes=("/health", "/api/v1/boards"),
        mcp_tools=("okto_pulse_create_ideation",),
        cli_commands=("init", "serve", "status", "reset"),
        commands_executed=("python scripts/r05e_community_preservation_smoke.py",),
        artifact_paths={"runner": "scripts/r05e_community_preservation_smoke.py"},
        baseline_routes=("/health", "/api/v1/boards"),
        baseline_mcp_tools=("okto_pulse_create_ideation",),
        baseline_cli_commands=("init", "serve", "status", "reset"),
        removed_dependencies=("asyncpg",),
        community_adapters_registered=("asyncpg",),
        generated_at=now,
    )

    report = CommunityRebuildReinstallSmokeGate().run_evidence(
        CommunitySmokeEvidenceInput(
            payload=payload,
            now=now,
            expected_core_commit="core-sha",
            expected_community_commit="community-sha",
            expected_wheel_hashes={"core": "sha256:core", "community": "sha256:community"},
            expected_removed_dependencies=("asyncpg",),
        )
    )

    assert report.status == "passed", report.evidence
    assert payload["gate_report"]["axis"] == "community_smoke"
    assert payload["gate_report"]["status"] == "passed"
    assert payload["gate_report"]["baseline_policy"] == "exact"
    assert payload["gate_report"]["symmetric_diff"]["routes"] == {"missing": [], "extra": []}
    assert set(payload["checks"]) == {
        "install",
        "imports",
        "composition",
        "seed",
        "routes",
        "mcp",
        "cli",
        "metadata",
    }


def test_community_runtime_smoke_evidence_surfaces_exact_diffs() -> None:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    payload = build_community_runtime_smoke_evidence(
        core_version="0.3.0",
        community_version="0.3.0",
        core_commit="core-sha",
        community_commit="community-sha",
        core_wheel_hash="sha256:core",
        community_wheel_hash="sha256:community",
        routes=("/health",),
        mcp_tools=("okto_pulse_create_ideation",),
        cli_commands=("init", "serve", "status", "reset"),
        commands_executed=("python scripts/r05e_community_preservation_smoke.py",),
        artifact_paths={"runner": "scripts/r05e_community_preservation_smoke.py"},
        baseline_routes=("/health", "/api/v1/boards"),
        baseline_mcp_tools=("okto_pulse_create_ideation",),
        generated_at=now,
    )

    assert payload["gate_report"]["status"] == "blocking"
    assert payload["gate_report"]["symmetric_diff"]["routes"] == {
        "missing": ["/api/v1/boards"],
        "extra": [],
    }

    report = CommunityRebuildReinstallSmokeGate().run_evidence(
        CommunitySmokeEvidenceInput(payload=payload, now=now)
    )
    assert report.status == "blocking"
    assert report.evidence["error"] == "smoke_evidence_failing"
