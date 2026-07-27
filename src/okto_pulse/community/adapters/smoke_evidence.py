"""Community-owned runtime smoke evidence for core release gates.

The core validates this payload as data through
``CommunityRebuildReinstallSmokeGate.run_evidence``. Community owns the runtime
smoke execution and publishes a canonical GateReport-shaped result.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

COMMUNITY_SMOKE_EVIDENCE_SCHEMA_VERSION = "1"
COMMUNITY_SMOKE_EVIDENCE_PRODUCER = "okto-pulse-community"
COMMUNITY_SMOKE_EVIDENCE_ARTIFACT = "community_runtime_smoke_evidence.json"
COMMUNITY_SMOKE_AXIS = "community_smoke"
COMMUNITY_SMOKE_BASELINE_POLICY = "exact"
COMMUNITY_SMOKE_REQUIRED_SURFACES: tuple[str, ...] = (
    "install",
    "imports",
    "composition",
    "seed",
    "routes",
    "mcp_tools",
    "cli_commands",
    "metadata",
)


def _stable(items: Iterable[str]) -> list[str]:
    return sorted({str(item) for item in items})


def _symmetric_diff(observed: Iterable[str], expected: Iterable[str]) -> dict[str, list[str]]:
    observed_set = set(_stable(observed))
    expected_set = set(_stable(expected))
    return {
        "missing": sorted(expected_set - observed_set),
        "extra": sorted(observed_set - expected_set),
    }


def _check(status: str = "passed", **details: Any) -> dict[str, Any]:
    return {
        "status": status,
        **({"diagnostics": details.pop("diagnostics")} if "diagnostics" in details else {}),
        **details,
    }


def build_community_runtime_smoke_evidence(
    *,
    core_version: str,
    community_version: str,
    core_commit: str,
    community_commit: str,
    core_wheel_hash: str,
    community_wheel_hash: str,
    routes: Iterable[str],
    mcp_tools: Iterable[str],
    cli_commands: Iterable[str],
    commands_executed: Iterable[str],
    artifact_paths: Mapping[str, str],
    baseline_routes: Iterable[str] | None = None,
    baseline_mcp_tools: Iterable[str] | None = None,
    baseline_cli_commands: Iterable[str] | None = None,
    generated_at: datetime | None = None,
    max_age_seconds: int = 3600,
    removed_dependencies: Iterable[str] = (),
    community_adapters_registered: Iterable[str] = (),
    smoke_oracle_evidence_id: str = "community-runtime-smoke",
) -> dict[str, Any]:
    """Build the R15B Community runtime smoke evidence package.

    ``baseline_*`` defaults to the observed inventory so callers that already
    computed a passing GateReport can serialize it without duplicating lists.
    Tests and release jobs should pass explicit baselines to prove the exact
    symmetric-diff contract.
    """
    created = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    observed_routes = _stable(routes)
    observed_mcp_tools = _stable(mcp_tools)
    observed_cli = _stable(cli_commands)
    expected_routes = _stable(baseline_routes if baseline_routes is not None else observed_routes)
    expected_mcp_tools = _stable(
        baseline_mcp_tools if baseline_mcp_tools is not None else observed_mcp_tools
    )
    expected_cli = _stable(
        baseline_cli_commands if baseline_cli_commands is not None else observed_cli
    )
    route_diff = _symmetric_diff(observed_routes, expected_routes)
    mcp_diff = _symmetric_diff(observed_mcp_tools, expected_mcp_tools)
    cli_diff = _symmetric_diff(observed_cli, expected_cli)
    has_diff = any(
        route_diff[key] or mcp_diff[key] or cli_diff[key]
        for key in ("missing", "extra")
    )
    gate_status = "blocking" if has_diff else "passed"
    removed = _stable(removed_dependencies)
    adapters = _stable(community_adapters_registered)
    commands = list(commands_executed)
    artifacts = dict(artifact_paths)

    checks = {
        "install": _check(commands=commands),
        "imports": _check(modules=("okto_pulse.community.cli", "okto_pulse.community.main")),
        "composition": _check(adapters=adapters),
        "seed": _check(),
        "routes": _check(routes=observed_routes),
        "mcp": _check(tools=observed_mcp_tools),
        "cli": _check(commands=observed_cli),
        "metadata": _check(
            dependencies=[],
            artifact_paths=artifacts,
            wheel_hashes={"core": core_wheel_hash, "community": community_wheel_hash},
        ),
    }

    return {
        "schema_version": COMMUNITY_SMOKE_EVIDENCE_SCHEMA_VERSION,
        "producer": COMMUNITY_SMOKE_EVIDENCE_PRODUCER,
        "artifact_name": COMMUNITY_SMOKE_EVIDENCE_ARTIFACT,
        "generated_at": created.isoformat(),
        "max_age_seconds": max_age_seconds,
        "core_version": core_version,
        "community_version": community_version,
        "core_commit": core_commit,
        "community_commit": community_commit,
        "wheel_hashes": {"core": core_wheel_hash, "community": community_wheel_hash},
        "artifact_paths": artifacts,
        "commands_executed": commands,
        "gate_report": {
            "axis": COMMUNITY_SMOKE_AXIS,
            "status": gate_status,
            "baseline_policy": COMMUNITY_SMOKE_BASELINE_POLICY,
            "required_surfaces": list(COMMUNITY_SMOKE_REQUIRED_SURFACES),
            "observed_counts": {
                "routes": len(observed_routes),
                "mcp_tools": len(observed_mcp_tools),
                "cli_commands": len(observed_cli),
            },
            "symmetric_diff": {
                "routes": route_diff,
                "mcp_tools": mcp_diff,
                "cli_commands": cli_diff,
            },
            "diagnostics": [] if not has_diff else ["exact_baseline_diff"],
        },
        "register_before_remove": {
            "removed_dependencies": removed,
            "community_adapters_registered": adapters,
            "smoke_oracle": {
                "status": gate_status,
                "evidence_id": smoke_oracle_evidence_id,
                "commit": community_commit,
                "wheel_hash": community_wheel_hash,
            },
        },
        "checks": checks,
    }


__all__ = [
    "COMMUNITY_SMOKE_AXIS",
    "COMMUNITY_SMOKE_BASELINE_POLICY",
    "COMMUNITY_SMOKE_EVIDENCE_ARTIFACT",
    "COMMUNITY_SMOKE_EVIDENCE_PRODUCER",
    "COMMUNITY_SMOKE_EVIDENCE_SCHEMA_VERSION",
    "COMMUNITY_SMOKE_REQUIRED_SURFACES",
    "build_community_runtime_smoke_evidence",
]
