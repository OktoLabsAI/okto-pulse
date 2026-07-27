from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.core.kg.candidate_decision_store import CandidateDecisionStore
from okto_pulse.core.kg.rebuild_audit import (
    CognitiveConsolidationItemStore,
)
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
from okto_pulse.core.kg.rebuild_confirmation import (
    ConfirmationOutcome,
    RebuildConfirmationStore,
)
from okto_pulse.core.kg.rebuild_report import (
    RebuildReportPayload,
    RebuildReportStore,
    RebuildReportSummary,
)
from okto_pulse.core.kg.rebuild_sources import (
    KGRebuildSourceManifest,
    RebuildSourceRow,
    RebuildSourceSet,
    SourceSetRevalidation,
    _compose_source_set_hash_v1,
    read_spec_manifest_rebaseline_audit,
)


def test_af29_s3r_community_store_preserves_rebuild_audit_roots(tmp_path):
    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    board_id = "board-af29-s3r"
    now = datetime.now(timezone.utc).isoformat()

    source_set = RebuildSourceSet(
        board_id=board_id,
        sources=(
            RebuildSourceRow(
                artifact_type="spec",
                source_ref="spec:1",
                source_version="1",
                content_hash="a" * 64,
                content_hash_v1="0" * 64,
                created_at=now,
                id="1",
            ),
        ),
        skipped_cancelled_count=0,
        has_non_deterministic_inputs=False,
        generated_at=now,
    )
    manifest = KGRebuildSourceManifest(artifact_store=store).build(
        source_set=source_set,
        preflight_hash="b" * 64,
    )
    assert (tmp_path / "rebuild" / "manifests" / f"{manifest.manifest_ref}.json").exists()
    assert KGRebuildSourceManifest(artifact_store=store).load(manifest.manifest_ref)
    legacy_manifest = dataclasses.replace(
        manifest,
        manifest_schema_version=1,
        source_set_hash=_compose_source_set_hash_v1(source_set),
    )
    rebaseline = KGRebuildSourceManifest(artifact_store=store).revalidate(
        manifest=legacy_manifest,
        current_source_set=source_set,
    )
    assert rebaseline.outcome == SourceSetRevalidation.REBASELINE
    rebaseline_records = read_spec_manifest_rebaseline_audit(
        None,
        board_id,
        artifact_store=store,
    )
    assert rebaseline_records
    assert (
        tmp_path
        / "rebuild"
        / "rebaseline_audit"
        / "records.json"
    ).exists()

    confirmation_store = RebuildConfirmationStore(artifact_store=store)
    token = confirmation_store.issue(
        board_id=board_id,
        actor_id="agent:test",
        operation="rebuild",
        preflight_hash="b" * 64,
        manifest_ref=manifest.manifest_ref,
    )
    token_path = tmp_path / "rebuild" / "confirmations" / f"{token.confirmation_id}.json"
    assert token_path.exists()
    consumed = confirmation_store.consume(
        confirmation_id=token.confirmation_id,
        expected_board_id=board_id,
        expected_actor_id="agent:test",
        expected_operation="rebuild",
        expected_preflight_hash="b" * 64,
        expected_manifest_ref=manifest.manifest_ref,
    )
    assert consumed.outcome == ConfirmationOutcome.CONSUMED.value
    assert not token_path.exists()

    report_store = RebuildReportStore(artifact_store=store)
    report = report_store.persist(
        payload=RebuildReportPayload(
            summary=RebuildReportSummary(
                board_id=board_id,
                run_id="run-1",
                status="completed",
                started_at=now,
                finished_at=now,
                counts={"spec": 1},
            ),
            hashes={"source": "c" * 64},
            source_refs=("spec:1",),
        )
    )
    assert report.report_id is not None
    assert (tmp_path / "rebuild" / "reports" / f"{report.report_id}.json").exists()
    assert report.report_ref is not None
    assert report_store.load(report.report_ref)["report_id"] == report.report_id

    candidate_store = CandidateDecisionStore(artifact_store=store)
    candidate = candidate_store.record(
        board_id=board_id,
        source_ref="spec:1",
        source_generation_id="kg-1",
        consolidation_session_id="sess-1",
        title="Use adapter boundary",
        rationale="The candidate store should use the rebuild artifact store.",
        created_by_agent_id="agent:test",
    )
    assert (
        tmp_path
        / "candidate_decisions"
        / board_id
        / f"{candidate.candidate_id}.json"
    ).exists()
    assert candidate_store.get(board_id, candidate.candidate_id) == candidate

    item_store = CognitiveConsolidationItemStore(artifact_store=store)
    materialized = item_store.materialize_from_marker(
        board_id=board_id,
        kg_generation_id="kg-1",
        event_ref="evt-1",
        source_set=(
            {
                "artifact_type": "spec",
                "source_ref": "spec:1",
                "content_hash": "d" * 64,
            },
        ),
        recorded_at=now,
    )
    assert materialized.item_count == 1
    assert (
        tmp_path
        / "rebuild"
        / "audit"
        / "cognitive_pending"
        / board_id
        / "kg-1.json"
    ).exists()

    source_manifest_rows = store.list_json(
        RebuildAuditKey(namespace="source_manifest", board_id=board_id)
    )
    assert any(row.get("manifest_ref") == manifest.manifest_ref for row in source_manifest_rows)

    rebuild_report_rows = store.list_json(
        RebuildAuditKey(namespace="rebuild_report", board_id=board_id)
    )
    assert any(row.get("report_id") == report.report_id for row in rebuild_report_rows)

    candidate_rows = store.list_json(
        RebuildAuditKey(namespace="candidate_decision", board_id=board_id)
    )
    assert [row.get("candidate_id") for row in candidate_rows] == [
        candidate.candidate_id
    ]

    cognitive_pending_rows = store.list_json(
        RebuildAuditKey(namespace="cognitive_pending", board_id=board_id)
    )
    assert [row.get("kg_generation_id") for row in cognitive_pending_rows] == ["kg-1"]
