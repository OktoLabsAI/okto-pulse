"""AC-15 rebuild integration for relational Code Traceability sources."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.board_rebuild_ingestion import (
    CommunityBoardRebuildIngestionAdapter,
    _resolve_evidence_dependency_closure,
)
from okto_pulse.community.adapters.board_source_reader import (
    CommunityBoardSourceReader,
)
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    ConsolidationQueue,
)
from okto_pulse.core.application.processors.consolidation import (
    _run_deterministic_worker,
)
from okto_pulse.core.kg.rebuild_sources import RebuildSourceEnumerator


NOW = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64
TRACEABILITY_TYPES = {
    "code_investigation_receipt",
    "code_evidence",
    "implementation_target",
}


async def seed_complete_traceability_source(connection) -> None:
    tables = Base.metadata.tables
    await connection.execute(
        tables["boards"].insert(),
        {
            "id": "board-1",
            "name": "Traceability board",
            "owner_id": "owner-1",
            "realm_id": "local",
        },
    )
    await connection.execute(
        tables["specs"].insert(),
        {
            "id": "spec-1",
            "board_id": "board-1",
            "title": "Traceability spec",
            "description": "A deterministic rebuild fixture.",
            "functional_requirements": [
                {
                    "id": "fr-1",
                    "title": "FR-1",
                    "text": "Persist accepted Code Evidence lineage.",
                }
            ],
            "status": "done",
            "version": 1,
            "created_by": "owner-1",
        },
    )
    await connection.execute(
        tables["cards"].insert(),
        {
            "id": "card-1",
            "board_id": "board-1",
            "spec_id": "spec-1",
            "title": "Implement traceability",
            "description": "Use only agent-submitted metadata.",
            "status": "not_started",
            "position": 0,
            "created_by": "owner-1",
        },
    )
    await connection.execute(
        tables["code_investigation_requests"].insert(),
        {
            "id": "request-1",
            "board_id": "board-1",
            "subject_type": "card",
            "subject_id": "card-1",
            "subject_version": 1,
            "issued_to_actor_id": "agent-1",
            "source_ref": "source-opaque-1",
            "required_capabilities": ["file_read", "path_containment"],
            "selector_scope_digest": SHA_A,
            "expected_head_generation": 0,
            "canonicalization_profile": "code-investigation/v1",
            "limits_profile": "code-investigation-limits/v1",
            "challenge_key_id": "challenge-v1",
            "challenge_token_hash": SHA_A,
            "status": "consumed",
            "expires_at": NOW + timedelta(minutes=10),
            "requested_by": "agent-1",
            "created_at": NOW,
            "consumed_at": NOW + timedelta(seconds=1),
            "request_payload_sha256": SHA_B,
            "idempotency_key": "request-idempotency-1",
        },
    )
    await connection.execute(
        tables["code_investigation_receipts"].insert(),
        {
            "id": "receipt-1",
            "request_id": "request-1",
            "board_id": "board-1",
            "subject_type": "card",
            "subject_id": "card-1",
            "subject_version": 1,
            "attestor_actor_id": "agent-1",
            "generation": 1,
            "acceptance_status": "accepted",
            "outcome": "accessible",
            "capabilities": ["file_read", "path_containment"],
            "source_ref": "source-opaque-1",
            "source_identity_digest": SHA_A,
            "canonicalization_profile": "code-investigation/v1",
            "limits_profile": "code-investigation-limits/v1",
            "selector_scope_digest": SHA_A,
            "declared_revision": "revision-1",
            "workspace_state_id": "workspace-1",
            "declared_dirty": False,
            "reproducibility_claim": "committed",
            "fingerprint_algorithm": "sha256",
            "manifest_digest": SHA_A,
            "manifest_entry_count": 1,
            "omission_manifest": [],
            "omission_digest": SHA_A,
            "omission_count": 0,
            "tooling": {
                "tool_id": "external-agent-check",
                "tool_version": "1",
                "method_id": "deterministic",
            },
            "observed_at": NOW,
            "received_at": NOW + timedelta(seconds=1),
            "expires_at": NOW + timedelta(hours=1),
            "observation_sha256": SHA_A,
            "payload_sha256": SHA_B,
            "idempotency_key": "receipt-idempotency-1",
        },
    )
    await connection.execute(
        tables["code_investigation_heads"].insert(),
        {
            "board_id": "board-1",
            "source_ref": "source-opaque-1",
            "generation": 1,
            "latest_receipt_id": "receipt-1",
            "current_receipt_id": "receipt-1",
            "state": "current",
            "revision": 1,
            "updated_at": NOW + timedelta(seconds=1),
        },
    )
    await connection.execute(
        tables["code_evidence"].insert(),
        {
            "id": "evidence-1",
            "board_id": "board-1",
            "investigation_receipt_id": "receipt-1",
            "source_ref": "source-opaque-1",
            "parent_type": "card",
            "card_id": "card-1",
            "parent_version": 1,
            "evidence_type": "structure",
            "claim": "The external agent observed the declared module.",
            "declared_revision": "revision-1",
            "workspace_state_id": "workspace-1",
            "declared_dirty": False,
            "reproducibility_claim": "committed",
            "selector_kind": "file",
            "relative_path": "src/module.py",
            "language": "python",
            "declared_file_blob_sha256": SHA_A,
            "declared_source_content_sha256": SHA_A,
            "attestation_state": "agent_attested",
            "attestation_basis": "authenticated_agent_receipt",
            "lifecycle_status": "active",
            "submitted_by": "agent-1",
            "received_at": NOW + timedelta(seconds=2),
            "payload_sha256": SHA_B,
            "idempotency_key": "evidence-idempotency-1",
        },
    )
    await connection.execute(
        tables["code_evidence_spec_links"].insert(),
        {
            "id": "evidence-spec-link-1",
            "board_id": "board-1",
            "spec_id": "spec-1",
            "evidence_id": "evidence-1",
            "entity_type": "functional_requirement",
            "entity_id": "fr-1",
            "relation_type": "supports",
            "rationale": "Evidence supports FR-1.",
            "evidence_content_sha256": SHA_B,
            "spec_version": 1,
            "created_by": "owner-1",
            "created_at": NOW + timedelta(seconds=3),
        },
    )
    await connection.execute(
        tables["implementation_targets"].insert(),
        {
            "id": "target-1",
            "board_id": "board-1",
            "card_id": "card-1",
            "source_ref": "source-opaque-1",
            "selector_kind": "file",
            "relative_path_hint": "src/module.py",
            "language": "python",
            "role": "modify",
            "intent": "Apply the accepted requirement.",
            "required": True,
            "source_spec_version": 1,
            "baseline_evidence_id": "evidence-1",
            "lifecycle_status": "active",
            "revision": 1,
            "current_resolution_id": "resolution-1",
            "created_by": "owner-1",
            "created_at": NOW + timedelta(seconds=4),
            "updated_at": NOW + timedelta(seconds=5),
        },
    )
    await connection.execute(
        tables["implementation_target_spec_links"].insert(),
        {
            "id": "target-spec-link-1",
            "target_id": "target-1",
            "spec_id": "spec-1",
            "entity_type": "functional_requirement",
            "entity_id": "fr-1",
            "created_by": "owner-1",
            "created_at": NOW + timedelta(seconds=4),
        },
    )
    await connection.execute(
        tables["implementation_target_evidence_links"].insert(),
        {
            "id": "target-evidence-link-1",
            "target_id": "target-1",
            "evidence_id": "evidence-1",
            "relation_type": "derived_from",
            "created_by": "owner-1",
            "created_at": NOW + timedelta(seconds=4),
        },
    )
    await connection.execute(
        tables["implementation_target_resolutions"].insert(),
        {
            "id": "resolution-1",
            "board_id": "board-1",
            "target_id": "target-1",
            "investigation_receipt_id": "receipt-1",
            "source_ref": "source-opaque-1",
            "receipt_generation": 1,
            "subject_version": 1,
            "target_revision": 1,
            "declared_revision": "revision-1",
            "workspace_state_id": "workspace-1",
            "declared_dirty": False,
            "state": "resolved",
            "resolved_relative_path": "src/module.py",
            "resolved_language": "python",
            "selector_fingerprint": SHA_A,
            "confidence": 1.0,
            "candidate_count": 0,
            "candidates": [],
            "declared_tool_id": "external-agent-check",
            "declared_tool_version": "1",
            "submitted_by": "agent-1",
            "agent_observed_at": NOW,
            "received_at": NOW + timedelta(seconds=5),
            "payload_sha256": SHA_B,
            "idempotency_key": "resolution-idempotency-1",
        },
    )


async def seed_superseded_evidence_chain(connection) -> None:
    """Extend the canonical fixture with active -> superseded -> superseded."""

    table = Base.metadata.tables["code_evidence"]
    common = {
        "board_id": "board-1",
        "investigation_receipt_id": "receipt-1",
        "source_ref": "source-opaque-1",
        "parent_type": "card",
        "card_id": "card-1",
        "parent_version": 1,
        "evidence_type": "structure",
        "declared_revision": "revision-1",
        "workspace_state_id": "workspace-1",
        "declared_dirty": False,
        "reproducibility_claim": "committed",
        "selector_kind": "file",
        "relative_path": "src/module.py",
        "language": "python",
        "declared_file_blob_sha256": SHA_A,
        "declared_source_content_sha256": SHA_A,
        "attestation_state": "agent_attested",
        "attestation_basis": "authenticated_agent_receipt",
        "lifecycle_status": "superseded",
        "submitted_by": "agent-1",
        "payload_sha256": SHA_B,
    }
    await connection.execute(
        table.insert(),
        [
            {
                **common,
                "id": "evidence-y",
                "claim": "Historical root evidence.",
                "supersedes_evidence_id": None,
                "received_at": NOW - timedelta(seconds=2),
                "idempotency_key": "evidence-idempotency-y",
            },
            {
                **common,
                "id": "evidence-z",
                "claim": "Historical successor evidence.",
                "supersedes_evidence_id": "evidence-y",
                "received_at": NOW - timedelta(seconds=1),
                "idempotency_key": "evidence-idempotency-z",
            },
            {
                **common,
                "id": "evidence-unrelated",
                "claim": "Unrelated historical evidence.",
                "supersedes_evidence_id": None,
                "received_at": NOW - timedelta(seconds=3),
                "idempotency_key": "evidence-idempotency-unrelated",
            },
        ],
    )
    await connection.execute(
        table.update()
        .where(table.c.id == "evidence-1")
        .values(supersedes_evidence_id="evidence-z")
    )


def _closure_source(
    evidence_id: str,
    *,
    status: str,
    predecessor: str | None,
    content_hash: str,
    candidate: bool = False,
) -> dict[str, object]:
    row: dict[str, object] = {
        "artifact_type": "code_evidence",
        "id": evidence_id,
        "source_ref": f"code_evidence:{evidence_id}",
        "source_version": "1",
        "content_hash": content_hash,
        "status": status,
        "source_artifact_status": status,
        "supersedes_evidence_id": predecessor,
        "_rebuild_manifest_created_at": "2026-08-15T00:00:00+00:00",
    }
    if candidate:
        row.update(
            {
                "disposition": "skipped_expired_working",
                "_rebuild_dependency_closure_candidate": ("code_evidence_supersedence"),
            }
        )
    return row


def endpoint_is_materialized(
    endpoint: str,
    *,
    candidate_ids: set[str],
    source_refs: set[tuple[str, str]],
) -> bool:
    if endpoint in candidate_ids:
        return True
    if not endpoint.startswith("kgref:"):
        return False
    node_type, separator, source_ref = endpoint[len("kgref:") :].partition(":")
    return bool(separator and (node_type, source_ref) in source_refs)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_rebuild_closes_historical_evidence_chain_without_counting_it_as_canonical(
    tmp_path,
) -> None:
    database_path = tmp_path / "evidence-closure.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await seed_complete_traceability_source(connection)
        await seed_superseded_evidence_chain(connection)

    snapshot = CommunityBoardSourceReader(database_path).fetch("board-1")
    assert snapshot.complete is True
    source_set = RebuildSourceEnumerator(
        source_store=lambda _board_id: list(snapshot.rows),
        now=NOW + timedelta(days=1),
    ).enumerate(board_id="board-1")
    assert {
        row.id
        for row in source_set.skipped_expired_working
        if row.artifact_type == "code_evidence"
    } == {"evidence-unrelated", "evidence-y", "evidence-z"}

    sources = [row.to_dict() for row in source_set.materializable_sources]
    for source in sources:
        source["_rebuild_manifest_created_at"] = NOW.isoformat()
    for row in source_set.skipped_expired_working:
        if row.artifact_type != "code_evidence":
            continue
        candidate = row.to_dict()
        candidate["_rebuild_manifest_created_at"] = NOW.isoformat()
        candidate["_rebuild_dependency_closure_candidate"] = (
            "code_evidence_supersedence"
        )
        sources.append(candidate)

    resolved, closure_count = _resolve_evidence_dependency_closure(
        db_path=database_path,
        board_id="board-1",
        sources=sources,
    )
    closure = {
        row["id"]: row for row in resolved if row.get("_rebuild_dependency_closure")
    }
    assert closure_count == 2
    assert set(closure) == {"evidence-y", "evidence-z"}
    assert {
        (row["graph_layer"], row["maturity_status"], row["disposition"])
        for row in closure.values()
    } == {("working", "working_superseded", "skipped_expired_working")}

    ingestion = CommunityBoardRebuildIngestionAdapter(db_path=database_path)
    first = ingestion.enqueue_sources(
        board_id="board-1",
        run_id="evidence-closure-run",
        sources=resolved,
    )
    second = ingestion.enqueue_sources(
        board_id="board-1",
        run_id="evidence-closure-run",
        sources=resolved,
    )
    assert first["inserted"] > 0
    assert second["inserted"] == 0

    with sqlite3.connect(database_path) as connection:
        ordered_evidence = [
            str(row[0])
            for row in connection.execute(
                "SELECT artifact_id FROM consolidation_queue "
                "WHERE board_id=? AND artifact_type='code_evidence' "
                "AND source=? ORDER BY priority, triggered_at, id",
                ("board-1", "rebuild:evidence-closure-run"),
            ).fetchall()
        ]
    assert ordered_evidence == ["evidence-y", "evidence-z", "evidence-1"]

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    persistence = CommunitySqlAlchemyConsolidationPersistence()
    results = []
    async with sessions() as session:
        for evidence_id in ordered_evidence:
            artifact = await persistence.load_artifact(
                session,
                artifact_type="code_evidence",
                artifact_id=evidence_id,
            )
            assert artifact is not None
            results.append(
                _run_deterministic_worker(
                    SimpleNamespace(artifact_type="code_evidence"),
                    artifact,
                )
            )

    nodes = [node for result in results for node in result.nodes]
    edges = [edge for result in results for edge in result.edges]
    evidence_refs = {
        node.source_artifact_ref for node in nodes if node.kind_of == "code_evidence"
    }
    assert evidence_refs == {
        "code_evidence:evidence-y",
        "code_evidence:evidence-z",
        "code_evidence:evidence-1",
    }
    assert {
        (edge.from_candidate_id, edge.to_candidate_id)
        for edge in edges
        if edge.edge_type == "supersedes"
    } == {
        (
            "code_evidence_evidence-z_entity",
            "kgref:Entity:code_evidence:evidence-y",
        ),
        (
            "code_evidence_evidence-1_entity",
            "kgref:Entity:code_evidence:evidence-z",
        ),
    }
    await engine.dispose()


@pytest.mark.parametrize(
    ("case", "expected_error"),
    (
        ("missing", "rebuild_evidence_predecessor_missing"),
        ("hash_drift", "rebuild_evidence_predecessor_identity_drift"),
        ("cycle", "rebuild_evidence_supersedence_cycle"),
        ("cross_board", "rebuild_evidence_predecessor_cross_board"),
    ),
)
def test_evidence_dependency_closure_fails_closed_for_invalid_manifest_chain(
    monkeypatch,
    tmp_path,
    case: str,
    expected_error: str,
) -> None:
    active = _closure_source(
        "evidence-a",
        status="active",
        predecessor="evidence-b",
        content_hash=SHA_A,
    )
    predecessor = _closure_source(
        "evidence-b",
        status="superseded",
        predecessor=("foreign-evidence" if case == "cross_board" else "evidence-c"),
        content_hash=SHA_B,
        candidate=True,
    )
    oldest = _closure_source(
        "evidence-c",
        status="superseded",
        predecessor=("evidence-a" if case == "cycle" else None),
        content_hash="c" * 64,
        candidate=True,
    )
    current = [dict(active), dict(predecessor), dict(oldest)]
    for row in current:
        row.pop("_rebuild_dependency_closure_candidate", None)
        row.pop("disposition", None)

    sources = [active, predecessor]
    if case != "missing" and case != "cross_board":
        sources.append(oldest)
    if case == "hash_drift":
        sources[-1] = {**sources[-1], "content_hash": "d" * 64}

    database_path = tmp_path / f"closure-{case}.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE code_evidence (id TEXT PRIMARY KEY, board_id TEXT NOT NULL)"
        )
        if case == "cross_board":
            connection.execute(
                "INSERT INTO code_evidence(id, board_id) VALUES (?, ?)",
                ("foreign-evidence", "board-2"),
            )
        connection.commit()

    monkeypatch.setattr(
        CommunityBoardSourceReader,
        "fetch",
        lambda _self, _board_id: SimpleNamespace(
            complete=True,
            cause=None,
            rows=tuple(current),
        ),
    )
    with pytest.raises(RuntimeError, match=expected_error):
        _resolve_evidence_dependency_closure(
            db_path=database_path,
            board_id="board-1",
            sources=sources,
        )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_rebuild_materializes_traceability_with_zero_orphan_endpoints(
    tmp_path,
) -> None:
    database_path = tmp_path / "traceability-rebuild.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await seed_complete_traceability_source(connection)

    snapshot = CommunityBoardSourceReader(database_path).fetch("board-1")
    assert snapshot.complete is True
    traceability_sources = tuple(
        source
        for source in snapshot.rows
        if source["artifact_type"] in TRACEABILITY_TYPES
    )
    assert {source["source_ref"] for source in traceability_sources} == {
        "code_investigation_receipt:receipt-1",
        "code_evidence:evidence-1",
        "implementation_target:target-1",
    }

    ingestion = CommunityBoardRebuildIngestionAdapter(db_path=database_path)
    counts = ingestion.enqueue_sources(
        board_id="board-1",
        run_id="ac15-full-rebuild",
        sources=snapshot.rows,
    )
    assert counts["inserted"] >= 5

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    persistence = CommunitySqlAlchemyConsolidationPersistence()
    async with sessions() as session:
        queued = tuple(
            (
                await session.execute(
                    select(ConsolidationQueue).where(
                        ConsolidationQueue.board_id == "board-1"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.artifact_type, row.artifact_id)
            for row in queued
            if row.artifact_type in TRACEABILITY_TYPES
        } == {
            ("code_investigation_receipt", "receipt-1"),
            ("code_evidence", "evidence-1"),
            ("implementation_target", "target-1"),
        }

        rank = {
            "spec": 0,
            "card": 1,
            "code_investigation_receipt": 2,
            "code_evidence": 3,
            "implementation_target": 4,
        }
        rebuild_rows = sorted(
            (row for row in queued if row.artifact_type in rank),
            key=lambda row: (rank[row.artifact_type], row.artifact_id),
        )
        results = []
        for row in rebuild_rows:
            artifact = await persistence.load_artifact(
                session,
                artifact_type=row.artifact_type,
                artifact_id=row.artifact_id,
            )
            assert artifact is not None
            results.append(
                _run_deterministic_worker(
                    SimpleNamespace(artifact_type=row.artifact_type),
                    artifact,
                )
            )

    nodes = [node for result in results for node in result.nodes]
    edges = [edge for result in results for edge in result.edges]
    candidate_ids = {node.candidate_id for node in nodes}
    source_refs = {
        (node.node_type, node.source_artifact_ref)
        for node in nodes
        if node.source_artifact_ref
    }
    unresolved = {
        (edge.candidate_id, endpoint)
        for edge in edges
        for endpoint in (edge.from_candidate_id, edge.to_candidate_id)
        if not endpoint_is_materialized(
            endpoint,
            candidate_ids=candidate_ids,
            source_refs=source_refs,
        )
    }
    assert unresolved == set()

    traceability_nodes = {
        node.kind_of: node
        for node in nodes
        if node.kind_of
        in {
            "code_investigation_receipt",
            "code_evidence",
            "implementation_target",
        }
    }
    assert set(traceability_nodes) == {
        "code_investigation_receipt",
        "code_evidence",
        "implementation_target",
    }
    for node in traceability_nodes.values():
        assert any(
            node.candidate_id in {edge.from_candidate_id, edge.to_candidate_id}
            for edge in edges
        )

    evidence_node = traceability_nodes["code_evidence"]
    target_node = traceability_nodes["implementation_target"]
    assert any(
        edge.edge_type == "supports"
        and edge.from_candidate_id == evidence_node.candidate_id
        and edge.to_candidate_id == "kgref:Requirement:spec:spec-1:fr:fr-1"
        for edge in edges
    )
    assert any(
        edge.edge_type == "belongs_to"
        and edge.from_candidate_id == target_node.candidate_id
        and edge.to_candidate_id == "kgref:Entity:card:card-1"
        for edge in edges
    )
    assert any(
        edge.edge_type == "derives_from"
        and edge.from_candidate_id == target_node.candidate_id
        and edge.to_candidate_id == "kgref:Entity:code_evidence:evidence-1"
        for edge in edges
    )

    with sqlite3.connect(database_path) as connection:
        pending_traceability = connection.execute(
            "SELECT COUNT(*) FROM consolidation_queue "
            "WHERE board_id = ? AND artifact_type IN (?, ?, ?)",
            (
                "board-1",
                "code_investigation_receipt",
                "code_evidence",
                "implementation_target",
            ),
        ).fetchone()[0]
    assert pending_traceability == 3
    await engine.dispose()
