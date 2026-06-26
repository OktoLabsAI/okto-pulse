"""R10-D (Community side) — product aggregator adapter + publish-health sources.
(Updated for R10-E Pass 2: ProductTelemetryAggregator removed from core.)

  ts_6b129804 (TS02) — CommunityProductTelemetryAggregator isinstance
        ProductAggregationPort + EXERCISES aggregate()->ProductState; Community IS
        the golden baseline (R10-E Pass 2: core shim deleted, parity test removed).
  conformance — each descriptor source isinstance PublishHealthSource + EXERCISES
        signal()->bounded dict; aws_ingest/report_athena default to GAP.
  composed-path — the REAL composition root registers the factory + provider, so
        the core registry resolves the Community adapter / gap descriptors (never
        hits the fail-closed guard), and the AWS/report invariant holds.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from okto_pulse.community.adapters.product_telemetry import (
    CommunityProductTelemetryAggregator,
    build_community_product_aggregator,
    register_community_product_aggregator,
)
from okto_pulse.community.adapters.publish_health_sources import (
    AwsIngestSource,
    InstallLifecycleSource,
    LocalPublishHealthSource,
    ReportAthenaSource,
    community_external_source_descriptors,
    register_community_publish_health_sources,
)
from okto_pulse.core.infra.config import CoreSettings
from okto_pulse.core.ports.telemetry import (
    ProductAggregationPort,
    ProductState,
    PublishHealthSource,
)
from okto_pulse.core.telemetry import publish_health as ph
from okto_pulse.core.telemetry.product_aggregator_registry import (
    get_product_aggregator,
    reset_product_aggregator_factory_for_tests,
)
from okto_pulse.core.telemetry import product_aggregator_registry as agg_registry
from okto_pulse.core.telemetry import publish_health_source_registry as src_registry
from okto_pulse.core.telemetry.publish_health_source_registry import (
    get_external_source_descriptors,
    reset_external_source_provider_for_tests,
)


@pytest.fixture(autouse=True)
def _isolate_registries():
    reset_product_aggregator_factory_for_tests()
    reset_external_source_provider_for_tests()
    try:
        yield
    finally:
        reset_product_aggregator_factory_for_tests()
        reset_external_source_provider_for_tests()


def _product_db(tmp_path: Path) -> CoreSettings:
    db = tmp_path / "pulse.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE domain_events (event_type TEXT, payload_json JSON);
        CREATE TABLE specs (id TEXT, status TEXT, ideation_id TEXT, refinement_id TEXT,
                            test_scenarios JSON, decisions JSON);
        CREATE TABLE story_ideation_links (ideation_id TEXT);
        CREATE TABLE cards (status TEXT, card_type TEXT);
        CREATE TABLE sprints (status TEXT);
        CREATE TABLE architecture_designs (id TEXT);
        """
    )
    conn.execute("INSERT INTO domain_events VALUES (?, ?)",
                 ("card.created", json.dumps({"card_type": "bug"})))
    conn.execute("INSERT INTO cards VALUES (?, ?)", ("done", "bug"))
    conn.execute("INSERT INTO sprints VALUES (?)", ("closed",))
    conn.commit()
    conn.close()
    return CoreSettings(metrics_dir=str(tmp_path / "metrics"),
                        database_url=f"sqlite:///{db.as_posix()}")


def test_ts_6b129804_community_aggregator_conformance(tmp_path):
    """R10-E Pass 2: ProductTelemetryAggregator removed from core. Community IS
    the canonical aggregator; parity test against core shim removed."""
    settings = _product_db(tmp_path)
    community = build_community_product_aggregator(settings, tmp_path / "c")
    assert isinstance(community, CommunityProductTelemetryAggregator)
    assert isinstance(community, ProductAggregationPort)

    state = community.aggregate()
    assert isinstance(state, ProductState)
    assert any(k.startswith("product_") for k in state.to_dict())

    # Community class is standalone (no core base to inherit from).
    assert CommunityProductTelemetryAggregator.__bases__ == (object,), (
        "CommunityProductTelemetryAggregator must be a standalone class"
    )


def test_descriptor_sources_conformance_and_signal(tmp_path):
    sources = [LocalPublishHealthSource(), InstallLifecycleSource(),
               AwsIngestSource(), ReportAthenaSource()]
    for src in sources:
        assert isinstance(src, PublishHealthSource)
        sig = src.signal()
        assert isinstance(sig, dict) and "availability" in sig
    # The external sources default to an explicit GAP (never healthy/available).
    assert AwsIngestSource().available is False
    assert ReportAthenaSource().available is False
    assert AwsIngestSource().signal() == {"availability": ph.SRC_GAP}
    assert ReportAthenaSource().signal() == {"availability": ph.SRC_GAP}

    # The provider returns the (aws, report) gap descriptors by default.
    aws, report = community_external_source_descriptors(object())
    assert aws == {"availability": ph.SRC_GAP}
    assert report == {"availability": ph.SRC_GAP}


def test_registration_wires_core_registries(tmp_path):
    settings = _product_db(tmp_path)
    reset_product_aggregator_factory_for_tests()
    reset_external_source_provider_for_tests()
    assert agg_registry._product_aggregator_factory is None
    assert src_registry._publish_health_source_provider is None

    register_community_product_aggregator()
    register_community_publish_health_sources()

    resolved = get_product_aggregator(settings, tmp_path / "metrics")
    assert isinstance(resolved, CommunityProductTelemetryAggregator)
    assert isinstance(resolved.aggregate(), ProductState)
    assert get_external_source_descriptors(settings) == (
        {"availability": ph.SRC_GAP}, {"availability": ph.SRC_GAP}
    )


_FALSE_MOVE_PATTERNS = (
    r"has moved",
    r"have moved",
    r"has been moved",
    r"\bmoves to the community",
    r"\bmoving to the community",
    r"\bmoved to the community",
    r"concrete\s+\w+\s+moved",
    r"concrete\s+\w+\s+has been moved",
    r"moves out of",
)


def test_guard_no_false_move_claims_in_all_product_files():
    """R10-E Pass 2: ProductTelemetryAggregator removed from core. Community OWNS
    the canonical impl. Post-absorb guard: no stale move-claim language."""
    import re

    import okto_pulse.community.adapters.product_telemetry as _c
    import okto_pulse.core.telemetry.product as _p
    import okto_pulse.core.telemetry.product_aggregator_registry as _r

    pats = [re.compile(p, re.IGNORECASE) for p in _FALSE_MOVE_PATTERNS]
    offenders: dict[str, list[str]] = {}
    for mod in (_p, _r, _c):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        hits = [p.pattern for p in pats if p.search(text)]
        if hits:
            offenders[Path(mod.__file__).name] = hits
    assert offenders == {}, offenders

    # Post-absorb: Community OWNS (not stays-as-shim).
    community_text = Path(_c.__file__).read_text(encoding="utf-8")
    assert "Community edition OWNS" in community_text, "expected Community ownership framing"
    # R10-E Pass 2: the core concrete is REMOVED — no "still a shim / pending Pass 2"
    # framing may survive (the anti-claim guard rejects the stale-shim vocabulary).
    for _stale in ("STAYS in core", "shim is still", "shim remains", "remains until PASS 2",
                   "stays as shim", "pending R10-E", "is non-destructive"):
        assert _stale.lower() not in community_text.lower(), (
            f"stale shim claim must not be present: {_stale!r}"
        )

    # R10-E Pass 2: Community class is standalone (no core base to inherit from).
    assert CommunityProductTelemetryAggregator.__bases__ == (object,), (
        "CommunityProductTelemetryAggregator must be a standalone class (no core base)"
    )

    # teeth: the guard is NOT vacuous — it catches a synthetic full-move claim.
    synthetic = "the concrete aggregator moves to the Community adapter"
    assert any(re.search(p, synthetic, re.IGNORECASE) for p in _FALSE_MOVE_PATTERNS)


def test_composed_root_wires_providers_and_invariant_holds(tmp_path, monkeypatch):
    """The REAL Community composition root registers BOTH providers, so a composed
    runtime resolves the Community aggregator and the AWS/report invariant holds
    (gap -> degraded, never healthy)."""
    import okto_pulse.core.infra.config as _config
    import okto_pulse.core.kg.interfaces.registry as _reg
    from datetime import datetime, timezone

    from okto_pulse.community.adapters.composition import configure_community_kg_registry
    from okto_pulse.core.infra.config import CoreSettings as _CS

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KG_BASE_DIR", str(tmp_path / "boards"))
    saved_settings = _config._settings_instance
    saved_reg = (_reg._registry, _reg._configured)
    _config.configure_settings(_CS())
    _reg.reset_registry_for_tests()
    try:
        reset_product_aggregator_factory_for_tests()
        reset_external_source_provider_for_tests()

        configure_community_kg_registry(None)

        settings = _product_db(tmp_path)
        resolved = get_product_aggregator(settings, tmp_path / "metrics")
        # R10-E: bind at assertion time (robust to sys.modules purges; isinstance stays strict).
        from okto_pulse.community.adapters.product_telemetry import CommunityProductTelemetryAggregator
        assert isinstance(resolved, CommunityProductTelemetryAggregator)
        aws, report = get_external_source_descriptors(settings)
        assert aws == {"availability": ph.SRC_GAP}

        # Invariant end-to-end: healthy local + composed gap AWS -> NOT healthy.
        now = datetime(2026, 6, 26, tzinfo=timezone.utc)
        dto = ph.resolve_publish_health(
            {"status": "ok", "publish_enabled": True, "last_success_at": now.isoformat()},
            now=now, aws_ingest=aws, report_athena=report,
        )
        assert dto.status != ph.HEALTHY
    finally:
        reset_product_aggregator_factory_for_tests()
        reset_external_source_provider_for_tests()
        _config._settings_instance = saved_settings
        _reg._registry, _reg._configured = saved_reg
