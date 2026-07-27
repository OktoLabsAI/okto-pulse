"""Community-side producer for R15A boundary evidence payloads.

The core consumes this output as data through
``validate_community_boundary_evidence``. Producing the payload here keeps
Community-specific proof collection out of the core runtime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

EvidenceStatus = Literal["passed", "failed", "skipped"]
EvidenceSurface = Literal["boundary", "packaging", "readiness", "conformance"]

COMMUNITY_EVIDENCE_SCHEMA_VERSION = "1"
COMMUNITY_EVIDENCE_PRODUCER = "okto-pulse-community"
COMMUNITY_EVIDENCE_EDITION = "community"


@dataclass(frozen=True)
class CommunityBoundaryCheckResult:
    """One Community-owned boundary evidence check."""

    name: str
    surface: EvidenceSurface
    status: EvidenceStatus
    details: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "surface": self.surface,
            "status": self.status,
            "details": dict(self.details or {}),
        }


def build_community_boundary_evidence(
    *,
    checks: Iterable[CommunityBoundaryCheckResult],
    core_commit: str,
    community_commit: str,
    artifact_hash: str,
    ledger_path: str,
    generated_at: datetime | None = None,
    max_age_seconds: int = 3600,
) -> dict[str, Any]:
    """Build a versioned CommunityBoundaryEvidence payload for core gates."""
    created = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema_version": COMMUNITY_EVIDENCE_SCHEMA_VERSION,
        "producer": COMMUNITY_EVIDENCE_PRODUCER,
        "edition": COMMUNITY_EVIDENCE_EDITION,
        "generated_at": created.isoformat(),
        "max_age_seconds": max_age_seconds,
        "expires_at": (created + timedelta(seconds=max_age_seconds)).isoformat(),
        "core_commit": core_commit,
        "community_commit": community_commit,
        "artifact_hash": artifact_hash,
        "ledger_path": ledger_path,
        "checks": [check.as_dict() for check in checks],
    }


__all__ = [
    "COMMUNITY_EVIDENCE_EDITION",
    "COMMUNITY_EVIDENCE_PRODUCER",
    "COMMUNITY_EVIDENCE_SCHEMA_VERSION",
    "CommunityBoundaryCheckResult",
    "build_community_boundary_evidence",
]
