"""R15A — Community produces boundary evidence as data."""

from __future__ import annotations

from datetime import datetime, timezone

from okto_pulse.community.adapters.boundary_evidence import (
    CommunityBoundaryCheckResult,
    build_community_boundary_evidence,
)
from okto_pulse.core.application.boundary.community_boundary_evidence import (
    validate_community_boundary_evidence,
)


def test_community_boundary_evidence_payload_passes_core_contract():
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    payload = build_community_boundary_evidence(
        checks=(
            CommunityBoundaryCheckResult(
                name="community-ledger",
                surface="boundary",
                status="passed",
                details={"references": 4},
            ),
            CommunityBoundaryCheckResult(
                name="readiness-provider",
                surface="readiness",
                status="passed",
                details={"producer": "community"},
            ),
        ),
        core_commit="core-sha",
        community_commit="community-sha",
        artifact_hash="sha256:abc",
        ledger_path="reports/community-boundary-ledger.json",
        generated_at=now,
        max_age_seconds=3600,
    )

    report = validate_community_boundary_evidence(
        payload,
        now=now,
        expected_core_commit="core-sha",
        expected_community_commit="community-sha",
        expected_artifact_hash="sha256:abc",
        required_surfaces=("boundary", "readiness"),
    )

    assert report.ok, report.as_dict()
    assert payload["producer"] == "okto-pulse-community"
    assert payload["edition"] == "community"
    assert payload["checks"][0]["details"] == {"references": 4}
