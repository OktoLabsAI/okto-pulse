"""Community publish-health source descriptors (spec R10-D, IMP3).

Provides :class:`~okto_pulse.core.ports.telemetry.PublishHealthSource`
implementations for the four distinguished health sources (``local`` /
``install_lifecycle`` / ``aws_ingest`` / ``report_athena``). The EXTERNAL sources
(``aws_ingest`` / ``report_athena``) have NO real S3 / Athena / crawler / repo-
metrics integration in this spec — they are DESCRIPTORS that default to an
explicit observability GAP (``SRC_GAP``). The pure ``resolve_publish_health``
classifier (which stays in core) maps a gap / stale / expired / unavailable /
absent descriptor to a STRUCTURED non-healthy state — AWS / report can NEVER be
inferred healthy from a local send.

The registered provider feeds the ``(aws_ingest, report_athena)`` descriptors to
``TelemetryService.publish_health`` via the core registry; the pure classifier is
untouched. A deployment that wires real adapters can override the descriptors via
``settings.metrics_health_external_sources`` (forward-compatible), but the default
can never mask the missing AWS/report visibility as healthy.
"""

from __future__ import annotations

from typing import Any

from okto_pulse.core.ports.telemetry import PublishHealthSource  # noqa: F401  (typing/contract)
from okto_pulse.core.telemetry import publish_health as ph
from okto_pulse.core.telemetry.publish_health_source_registry import (
    register_external_source_provider,
)


class _DescriptorSource:
    """A PublishHealthSource backed by a bounded, secret-free descriptor dict
    (``{"availability": ..., "last_success_at": ...}``)."""

    def __init__(self, name: str, descriptor: Any = None):
        self._name = name
        raw = descriptor if isinstance(descriptor, dict) else {}
        self._availability = str(raw.get("availability") or ph.SRC_UNAVAILABLE)
        last = raw.get("last_success_at")
        self._last_success_at = last if isinstance(last, str) else None

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        # "available" means the source could be read and is fresh enough to count
        # (a gap/expired/unavailable source is NOT available and never healthy).
        return self._availability in (ph.SRC_AVAILABLE, ph.SRC_STALE)

    def signal(self) -> dict[str, Any]:
        out: dict[str, Any] = {"availability": self._availability}
        if self._last_success_at is not None:
            out["last_success_at"] = self._last_success_at
        return out


class LocalPublishHealthSource(_DescriptorSource):
    def __init__(self, descriptor: Any = None):
        super().__init__(ph.SOURCE_LOCAL, descriptor or {"availability": ph.SRC_AVAILABLE})


class InstallLifecycleSource(_DescriptorSource):
    def __init__(self, descriptor: Any = None):
        super().__init__(ph.SOURCE_INSTALL_LIFECYCLE, descriptor or {"availability": ph.SRC_AVAILABLE})


class AwsIngestSource(_DescriptorSource):
    """No real ingest adapter in this build -> default explicit GAP (never healthy)."""

    def __init__(self, descriptor: Any = None):
        super().__init__(ph.SOURCE_AWS_INGEST, descriptor or {"availability": ph.SRC_GAP})


class ReportAthenaSource(_DescriptorSource):
    """No real Athena report adapter in this build -> default explicit GAP."""

    def __init__(self, descriptor: Any = None):
        super().__init__(ph.SOURCE_REPORT_ATHENA, descriptor or {"availability": ph.SRC_GAP})


def community_external_source_descriptors(settings: Any) -> tuple[Any, Any]:
    """Return the ``(aws_ingest, report_athena)`` descriptors as PublishHealthSource
    signals. No real integration here: default to an explicit GAP (degraded, never
    healthy). ``settings.metrics_health_external_sources`` may override per-source
    (forward-compatible) but never masks a missing source as healthy."""
    configured = getattr(settings, "metrics_health_external_sources", None)
    if isinstance(configured, dict):
        aws_desc = configured.get(ph.SOURCE_AWS_INGEST, {"availability": ph.SRC_GAP})
        report_desc = configured.get(ph.SOURCE_REPORT_ATHENA, {"availability": ph.SRC_GAP})
    else:
        aws_desc = {"availability": ph.SRC_GAP}
        report_desc = {"availability": ph.SRC_GAP}
    return (
        AwsIngestSource(aws_desc).signal(),
        ReportAthenaSource(report_desc).signal(),
    )


def register_community_publish_health_sources() -> None:
    """Register the Community external publish-health source provider at the core
    registry (composition root). Idempotent."""
    register_external_source_provider(community_external_source_descriptors)


__all__ = [
    "LocalPublishHealthSource",
    "InstallLifecycleSource",
    "AwsIngestSource",
    "ReportAthenaSource",
    "community_external_source_descriptors",
    "register_community_publish_health_sources",
]
