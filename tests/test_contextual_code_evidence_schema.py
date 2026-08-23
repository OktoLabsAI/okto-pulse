"""I1 persistence contract for contextual and legacy-classified Evidence."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
import sqlalchemy
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

import okto_pulse.community.adapters.relational_schema_steps as schema_steps
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.relational_schema_migrator import (
    build_community_migration_ledger,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    CODE_EVIDENCE_CLASSIFICATION_BATCH_ID_MAX_LENGTH,
    CODE_EVIDENCE_CLASSIFICATION_IDEMPOTENCY_KEY_MAX_LENGTH,
    Base,
    CodeEvidenceClassificationEventRow,
    CodeEvidenceClassificationHeadRow,
    CodeEvidenceRow,
)
from okto_pulse.core.domain import code_traceability as domain
from okto_pulse.core.models.code_traceability import (
    LegacyEvidenceClassificationBatchInput,
)
from okto_pulse.core.services.legacy_code_evidence_classification import (
    _default_id_factory,
)
from test_code_traceability_persistence import _attestation_bundle
from test_legacy_code_evidence_classification_persistence import (
    _classification_batch,
    _database_with_legacy_evidence,
)


_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64


async def _install_legacy_classification_identifier_widths(connection: object) -> None:
    event_table = CodeEvidenceClassificationEventRow.__table__
    head_table = CodeEvidenceClassificationHeadRow.__table__
    await connection.exec_driver_sql(f'DROP TABLE "{head_table.name}"')
    await connection.exec_driver_sql(f'DROP TABLE "{event_table.name}"')
    legacy_event_ddl = (
        str(CreateTable(event_table).compile(dialect=connection.dialect))
        .replace(
            "batch_id VARCHAR(255)",
            "batch_id VARCHAR(64)",
        )
        .replace(
            "idempotency_key VARCHAR(512)",
            "idempotency_key VARCHAR(255)",
        )
    )
    await connection.exec_driver_sql(legacy_event_ddl)
    for index in sorted(event_table.indexes, key=lambda item: str(item.name)):
        await connection.exec_driver_sql(
            str(CreateIndex(index).compile(dialect=connection.dialect))
        )
    await connection.exec_driver_sql(
        str(CreateTable(head_table).compile(dialect=connection.dialect))
    )
    for index in sorted(head_table.indexes, key=lambda item: str(item.name)):
        await connection.exec_driver_sql(
            str(CreateIndex(index).compile(dialect=connection.dialect))
        )


def _legacy_evidence(
    now: datetime,
    receipt: domain.CodeInvestigationReceipt,
    workspace: domain.ObservedWorkspaceStateRef,
) -> domain.CodeEvidence:
    return domain.CodeEvidence(
        id="evidence-legacy-1",
        board_id="board-1",
        investigation_receipt_id=receipt.id,
        source_ref=receipt.source_ref,
        parent_type=domain.CodeTraceabilitySubjectType.CARD,
        parent_id="card-1",
        parent_version=1,
        evidence_type=domain.CodeEvidenceType.STRUCTURE,
        claim="Legacy evidence deliberately has no inferred contextual meaning.",
        workspace_state=workspace,
        selector_kind=domain.CodeEvidenceSelectorKind.FILE,
        relative_path="src/module.py",
        language="python",
        symbol_kind=None,
        qualified_symbol=None,
        symbol_signature=None,
        snapshot_line_start=None,
        snapshot_line_end=None,
        excerpt=None,
        excerpt_sha256=None,
        declared_file_blob_sha256=_A,
        declared_source_content_sha256=_B,
        excerpt_omitted_reason="not_submitted",
        attestation_state=domain.CodeEvidenceAttestationState.AGENT_ATTESTED,
        attestation_basis=(
            domain.CodeEvidenceAttestationBasis.AUTHENTICATED_AGENT_RECEIPT
        ),
        lifecycle_status=domain.CodeTraceabilityLifecycleStatus.ACTIVE,
        supersedes_evidence_id=None,
        revocation_reason=None,
        submitted_by="agent-1",
        received_at=now + timedelta(seconds=2),
        payload_sha256=_C,
        idempotency_key="legacy-evidence-1",
    )


def test_contextual_migration_is_between_traceability_and_dependencies() -> None:
    ids = [step.step_id for step in build_community_migration_ledger()]
    contextual = ids.index("_migrate_contextual_code_evidence_schema")
    assert contextual == ids.index("_migrate_code_traceability_schema") + 1
    assert contextual + 1 == ids.index("_migrate_spec_dependency_schema")


def test_contextual_metadata_has_only_event_and_head_authority() -> None:
    assert CodeEvidenceClassificationEventRow.__tablename__ == (
        "code_evidence_classification_events"
    )
    assert CodeEvidenceClassificationHeadRow.__tablename__ == (
        "code_evidence_classification_heads"
    )
    assert "code_evidence_classification_batches" not in Base.metadata.tables
    assert "code_evidence_classifications" not in Base.metadata.tables

    event_columns = set(CodeEvidenceClassificationEventRow.__table__.columns.keys())
    assert {
        "batch_id",
        "evidence_payload_sha256",
        "predecessor_classification_id",
        "source_role",
        "baseline_presence",
        "baseline_workspace_state_id",
        "batch_item_count",
        "batch_item_index",
        "classification_sha256",
    } <= event_columns


def test_core_request_identifier_limits_fit_the_relational_contract() -> None:
    default_batch_id = _default_id_factory("code_evidence_classification_batch")
    command = LegacyEvidenceClassificationBatchInput.model_validate(
        {
            "board_id": "board-1",
            "items": [
                {
                    "evidence_id": "evidence-1",
                    "expected_evidence_payload_sha256": _A,
                    "expected_classification_revision": 0,
                    "source_role": "existing_constraint",
                    "relevance_summary": "Constrains the planned change.",
                    "scope_relation": "Same bounded delivery scope.",
                    "source_origin": "Frozen repository baseline.",
                    "baseline_provenance": {
                        "presence": "committed_snapshot",
                        "workspace_state_id": "workspace-1",
                        "provenance_note": None,
                    },
                }
            ],
            "justification": "Human classification.",
            "idempotency_key": "i" * 512,
        }
    )
    event_table = CodeEvidenceClassificationEventRow.__table__
    batch_type = event_table.c.batch_id.type
    idempotency_type = event_table.c.idempotency_key.type

    assert len(default_batch_id) == 67
    assert len(default_batch_id) > 64
    assert batch_type.length == (CODE_EVIDENCE_CLASSIFICATION_BATCH_ID_MAX_LENGTH)
    assert len(default_batch_id) <= batch_type.length
    assert len(command.idempotency_key) == 512
    assert idempotency_type.length == (
        CODE_EVIDENCE_CLASSIFICATION_IDEMPOTENCY_KEY_MAX_LENGTH
    )
    assert len(command.idempotency_key) <= idempotency_type.length

    postgresql_table_ddl = str(
        CreateTable(event_table).compile(dialect=postgresql.dialect())
    )
    assert "batch_id VARCHAR(255) NOT NULL" in postgresql_table_ddl
    assert "idempotency_key VARCHAR(512) NOT NULL" in postgresql_table_ddl
    assert "ck_code_evidence_classification_request_identity" in postgresql_table_ddl
    assert schema_steps.contextual_code_evidence_postgresql_width_ddl() == {
        "batch_id": (
            'ALTER TABLE "code_evidence_classification_events" '
            'ALTER COLUMN "batch_id" TYPE VARCHAR(255)'
        ),
        "idempotency_key": (
            'ALTER TABLE "code_evidence_classification_events" '
            'ALTER COLUMN "idempotency_key" TYPE VARCHAR(512)'
        ),
        "request_identity_constraint": (
            'ALTER TABLE "code_evidence_classification_events" ADD '
            "CONSTRAINT "
            '"ck_code_evidence_classification_request_identity" CHECK '
            "(length(trim(batch_id)) >= 1 AND length(batch_id) <= 255 "
            "AND length(trim(idempotency_key)) >= 1 AND "
            "length(idempotency_key) <= 512)"
        ),
    }
    sqlite_manifest = schema_steps.contextual_code_evidence_sqlite_trigger_manifest()
    sqlite_insert_guard = " ".join(
        sqlite_manifest["trg_contextual_code_evidence_v2_classification_event_insert"][
            1
        ].split()
    )
    assert 'length(NEW."batch_id") > 255' in sqlite_insert_guard
    assert 'length(NEW."idempotency_key") > 512' in sqlite_insert_guard


def test_sqlite_classification_guards_are_cas_and_permit_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'contextual.db').as_posix()}"
        )
        monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        request, consumed, receipt, head, workspace = _attestation_bundle(now)
        evidence = _legacy_evidence(now, receipt, workspace)

        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await _install_legacy_classification_identifier_widths(connection)
            await connection.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES (?, ?, ?, ?)",
                ("board-1", "Board", "owner-1", "local"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO cards "
                "(id, board_id, title, status, position, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("card-1", "board-1", "Card", "not_started", 0, "owner-1"),
            )

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            adapter = CommunityRelationalApplicationAdapter()
            investigations = adapter.code_investigations(session)
            traceability = adapter.code_traceability(session)
            await investigations.create_request(request)
            await investigations.consume_request_append_receipt_and_advance_head(
                request=consumed,
                receipt=receipt,
                head=head,
                expected_head_revision=None,
            )
            await traceability.create_evidence(
                evidence=evidence,
                expected_head_revision=1,
            )
            await session.commit()

        assert await schema_steps._migrate_contextual_code_evidence_schema() is None
        assert (
            await schema_steps._migrate_contextual_code_evidence_schema() == "skipped"
        )

        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    table_name: {
                        str(column["name"])
                        for column in sa_inspect(sync_connection).get_columns(
                            table_name
                        )
                    }
                    for table_name in (
                        "refinements",
                        "refinement_snapshots",
                        "specs",
                        "code_investigation_receipts",
                        "code_evidence",
                    )
                }
            )
            classification_widths = await connection.run_sync(
                lambda sync_connection: {
                    str(column["name"]): getattr(column["type"], "length", None)
                    for column in sa_inspect(sync_connection).get_columns(
                        "code_evidence_classification_events"
                    )
                }
            )
        assert "delivery_context" in columns["refinements"]
        assert {
            "delivery_context",
            "source_context_manifest",
            "source_context_sha256",
        } <= columns["refinement_snapshots"]
        assert {
            "delivery_context",
            "delivery_context_provenance",
            "source_context_manifest",
            "source_context_sha256",
        } <= columns["specs"]
        assert {
            "delivery_context",
            "contextual_outcome",
            "context_contract_version",
        } <= columns["code_investigation_receipts"]
        assert {
            "source_role",
            "relevance_summary",
            "baseline_presence",
            "baseline_workspace_state_id",
            "context_contract_version",
        } <= columns["code_evidence"]
        assert classification_widths["batch_id"] == 255
        assert classification_widths["idempotency_key"] == 512

        event_values = {
            "id": "classification-1",
            "batch_id": _default_id_factory("code_evidence_classification_batch"),
            "board_id": "board-1",
            "evidence_id": evidence.id,
            "evidence_payload_sha256": evidence.payload_sha256,
            "revision": 1,
            "predecessor_classification_id": None,
            "source_role": "current_implementation",
            "relevance_summary": "Current implementation of the bounded behavior.",
            "scope_relation": "Directly supports the requested delivery scope.",
            "source_origin": "Existing source at the frozen baseline.",
            "interpretation_limit": None,
            "baseline_presence": "committed_snapshot",
            "baseline_workspace_state_id": workspace.workspace_state_id,
            "baseline_provenance_note": None,
            "classified_by": "human-1",
            "classified_at": now + timedelta(seconds=3),
            "justification": "Human reviewed the legacy Evidence context.",
            "idempotency_key": "i" * 512,
            "request_sha256": _D,
            "batch_item_count": 1,
            "batch_item_index": 1,
            "context_contract_version": 2,
            "classification_sha256": _E,
        }
        event_insert = text(
            "INSERT INTO code_evidence_classification_events "
            "(id, batch_id, board_id, evidence_id, evidence_payload_sha256, "
            "revision, predecessor_classification_id, source_role, "
            "relevance_summary, scope_relation, source_origin, "
            "interpretation_limit, baseline_presence, "
            "baseline_workspace_state_id, baseline_provenance_note, "
            "classified_by, classified_at, justification, idempotency_key, "
            "request_sha256, batch_item_count, batch_item_index, "
            "context_contract_version, classification_sha256) VALUES "
            "(:id, :batch_id, :board_id, :evidence_id, "
            ":evidence_payload_sha256, :revision, "
            ":predecessor_classification_id, :source_role, "
            ":relevance_summary, :scope_relation, :source_origin, "
            ":interpretation_limit, :baseline_presence, "
            ":baseline_workspace_state_id, :baseline_provenance_note, "
            ":classified_by, :classified_at, :justification, "
            ":idempotency_key, :request_sha256, :batch_item_count, "
            ":batch_item_index, :context_contract_version, "
            ":classification_sha256)"
        )
        async with engine.begin() as connection:
            await connection.execute(event_insert, event_values)
            await connection.execute(
                text(
                    "INSERT INTO code_evidence_classification_heads "
                    "(board_id, evidence_id, current_classification_id, "
                    "evidence_payload_sha256, revision, updated_at) VALUES "
                    "(:board_id, :evidence_id, :current_id, :payload, 1, :at)"
                ),
                {
                    "board_id": "board-1",
                    "evidence_id": evidence.id,
                    "current_id": event_values["id"],
                    "payload": evidence.payload_sha256,
                    "at": now + timedelta(seconds=4),
                },
            )

        invalid = dict(event_values)
        invalid.update(
            {
                "id": "classification-invalid",
                "batch_id": "classification-batch-invalid",
                "revision": 2,
                "predecessor_classification_id": event_values["id"],
                "baseline_workspace_state_id": "wrong-workspace",
                "idempotency_key": "classification-request-invalid",
            }
        )
        overlong_request = dict(invalid)
        overlong_request.update(
            {
                "id": "classification-overlong-request",
                "batch_id": "classification-batch-overlong-request",
                "baseline_workspace_state_id": workspace.workspace_state_id,
                "idempotency_key": "i" * 513,
            }
        )
        with pytest.raises(IntegrityError, match="classification_event_invalid"):
            async with engine.begin() as connection:
                await connection.execute(event_insert, overlong_request)

        overlong_batch = dict(overlong_request)
        overlong_batch.update(
            {
                "id": "classification-overlong-batch",
                "batch_id": "b" * 256,
                "idempotency_key": "classification-overlong-batch",
            }
        )
        with pytest.raises(IntegrityError, match="classification_event_invalid"):
            async with engine.begin() as connection:
                await connection.execute(event_insert, overlong_batch)

        with pytest.raises(IntegrityError, match="classification_event_invalid"):
            async with engine.begin() as connection:
                await connection.execute(event_insert, invalid)

        with pytest.raises(IntegrityError, match="event_immutable"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE code_evidence_classification_events "
                        "SET justification = 'rewritten' WHERE id = :id"
                    ),
                    {"id": event_values["id"]},
                )

        with pytest.raises(IntegrityError, match="delete_forbidden"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM code_evidence_classification_heads "
                        "WHERE board_id = 'board-1' "
                        "AND evidence_id = :evidence_id"
                    ),
                    {"evidence_id": evidence.id},
                )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO kg_board_erasure_permits "
                    "(board_id, permit_token) VALUES ('board-1', :token)"
                ),
                {"token": _A},
            )
            await connection.execute(
                text(
                    "DELETE FROM code_evidence_classification_heads "
                    "WHERE board_id = 'board-1'"
                )
            )
            await connection.execute(
                text(
                    "DELETE FROM code_evidence_classification_events "
                    "WHERE board_id = 'board-1'"
                )
            )
        await engine.dispose()

    asyncio.run(exercise())


def test_populated_legacy_authority_upgrade_preserves_rows_links_and_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine, sessions, now, _workspace, evidence = (
            await _database_with_legacy_evidence(
                tmp_path / "contextual-populated-upgrade.db",
                evidence_count=1,
            )
        )
        monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO ideations "
                "(id, board_id, title, status, edition, version, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("ideation-1", "board-1", "Idea", "done", 1, 1, "owner-1"),
            )
            await connection.exec_driver_sql(
                "INSERT INTO refinements "
                "(id, ideation_id, board_id, title, status, edition, version, "
                "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "refinement-1",
                    "ideation-1",
                    "board-1",
                    "Refinement",
                    "done",
                    1,
                    1,
                    "owner-1",
                ),
            )
            await connection.exec_driver_sql(
                "INSERT INTO specs "
                "(id, board_id, ideation_id, refinement_id, title, status, "
                "edition, version, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "spec-1",
                    "board-1",
                    "ideation-1",
                    "refinement-1",
                    "Spec",
                    "draft",
                    1,
                    1,
                    "owner-1",
                ),
            )
            # The row represents a valid legacy link shape. The modern CAS
            # trigger additionally expects frozen lineage that did not exist
            # in that legacy state; the contextual migration restores this
            # owned trigger after preserving the row.
            await connection.exec_driver_sql(
                'DROP TRIGGER "trg_code_traceability_v1_spec_link_i"'
            )
            await connection.exec_driver_sql(
                "INSERT INTO code_evidence_spec_links "
                "(id, board_id, spec_id, evidence_id, entity_type, entity_id, "
                "relation_type, rationale, evidence_content_sha256, "
                "source_refinement_version, spec_version, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "link-1",
                    "board-1",
                    "spec-1",
                    evidence[0].id,
                    "technical_requirement",
                    "tr-1",
                    "supports",
                    "Legacy Evidence supports the frozen obligation.",
                    evidence[0].content_sha256,
                    1,
                    1,
                    "owner-1",
                    now,
                ),
            )

        receipt = _classification_batch(evidence, now=now, batch_sequence=1)
        async with sessions() as session:
            await CommunityRelationalApplicationAdapter().code_traceability(
                session
            ).append_legacy_evidence_classification_batch(
                receipt=receipt,
                expected_revisions={evidence[0].id: 0},
            )
            await session.commit()

        async with engine.begin() as connection:
            event_rows = tuple(
                dict(row)
                for row in (
                    (
                        await connection.execute(
                            select(
                                CodeEvidenceClassificationEventRow.__table__
                            ).order_by(
                                CodeEvidenceClassificationEventRow.revision,
                                CodeEvidenceClassificationEventRow.id,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            )
            head_rows = tuple(
                dict(row)
                for row in (
                    (
                        await connection.execute(
                            select(
                                CodeEvidenceClassificationHeadRow.__table__
                            ).order_by(
                                CodeEvidenceClassificationHeadRow.board_id,
                                CodeEvidenceClassificationHeadRow.evidence_id,
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            )
            await _install_legacy_classification_identifier_widths(connection)
            await connection.execute(
                CodeEvidenceClassificationEventRow.__table__.insert(),
                event_rows,
            )
            await connection.execute(
                CodeEvidenceClassificationHeadRow.__table__.insert(),
                head_rows,
            )

        async def snapshot() -> dict[str, tuple[tuple[object, ...], ...]]:
            queries = {
                "receipts": (
                    "SELECT id, payload_sha256, observation_sha256, generation "
                    "FROM code_investigation_receipts ORDER BY id"
                ),
                "evidence": (
                    "SELECT id, payload_sha256, declared_source_content_sha256, "
                    "source_role, lifecycle_status FROM code_evidence ORDER BY id"
                ),
                "links": (
                    "SELECT id, evidence_id, evidence_content_sha256, spec_version "
                    "FROM code_evidence_spec_links ORDER BY id"
                ),
                "events": (
                    "SELECT id, evidence_payload_sha256, revision, request_sha256, "
                    "classification_sha256 FROM code_evidence_classification_events "
                    "ORDER BY revision, id"
                ),
                "heads": (
                    "SELECT board_id, evidence_id, current_classification_id, "
                    "evidence_payload_sha256, revision "
                    "FROM code_evidence_classification_heads "
                    "ORDER BY board_id, evidence_id"
                ),
            }
            async with engine.connect() as connection:
                return {
                    name: tuple(
                        tuple(row)
                        for row in (await connection.execute(text(sql))).all()
                    )
                    for name, sql in queries.items()
                }

        before = await snapshot()
        assert {name: len(rows) for name, rows in before.items()} == {
            "receipts": 1,
            "evidence": 1,
            "links": 1,
            "events": 1,
            "heads": 1,
        }
        assert await schema_steps._migrate_contextual_code_evidence_schema() is None
        assert await snapshot() == before
        assert (
            await schema_steps._migrate_contextual_code_evidence_schema() == "skipped"
        )
        assert await snapshot() == before
        await engine.dispose()

    asyncio.run(exercise())


def test_sqlite_contextual_artifact_failure_rolls_back_and_retry_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine, _sessions, _now, _workspace, _evidence = (
            await _database_with_legacy_evidence(
                tmp_path / "contextual-safe-retry.db",
                evidence_count=1,
            )
        )
        monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                'DROP TABLE "code_evidence_classification_heads"'
            )
            await connection.exec_driver_sql(
                'DROP TABLE "code_evidence_classification_events"'
            )

        async def authority_objects() -> tuple[tuple[str, str], ...]:
            async with engine.connect() as connection:
                return tuple(
                    (str(row.type), str(row.name))
                    for row in (
                        await connection.execute(
                            text(
                                "SELECT type, name FROM sqlite_master "
                                "WHERE name IN ("
                                "'code_evidence_classification_events', "
                                "'code_evidence_classification_heads') "
                                "OR name LIKE "
                                "'trg_contextual_code_evidence_v2_classification_%' "
                                "ORDER BY type, name"
                            )
                        )
                    ).all()
                )

        assert await authority_objects() == ()
        original_manifest = (
            schema_steps.contextual_code_evidence_sqlite_trigger_manifest
        )

        def injected_failure():
            raise RuntimeError("injected_contextual_sqlite_artifact_failure")

        monkeypatch.setattr(
            schema_steps,
            "contextual_code_evidence_sqlite_trigger_manifest",
            injected_failure,
        )
        with pytest.raises(
            RuntimeError,
            match="injected_contextual_sqlite_artifact_failure",
        ):
            await schema_steps._migrate_contextual_code_evidence_schema()
        assert await authority_objects() == ()

        monkeypatch.setattr(
            schema_steps,
            "contextual_code_evidence_sqlite_trigger_manifest",
            original_manifest,
        )
        assert await schema_steps._migrate_contextual_code_evidence_schema() is None
        objects_after_retry = await authority_objects()
        assert (
            "table",
            "code_evidence_classification_events",
        ) in objects_after_retry
        assert (
            "table",
            "code_evidence_classification_heads",
        ) in objects_after_retry
        assert sum(kind == "trigger" for kind, _name in objects_after_retry) == 6
        assert (
            await schema_steps._migrate_contextual_code_evidence_schema() == "skipped"
        )
        assert await authority_objects() == objects_after_retry
        await engine.dispose()

    asyncio.run(exercise())


def test_postgresql_contextual_artifact_failure_has_no_half_state_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeInspector:
        def get_table_names(self) -> list[str]:
            return list(Base.metadata.tables)

        def get_columns(self, table_name: str) -> list[dict[str, object]]:
            return [
                {"name": column.name, "type": column.type}
                for column in Base.metadata.tables[table_name].columns
            ]

    class FakeResult:
        def __init__(
            self,
            *,
            rows: tuple[object, ...] = (),
            scalar: object | None = None,
            rowcount: int = 0,
        ) -> None:
            self._rows = rows
            self._scalar = scalar
            self.rowcount = rowcount

        def scalar_one_or_none(self) -> object | None:
            return self._scalar

        def mappings(self) -> "FakeResult":
            return self

        def all(self) -> list[object]:
            return list(self._rows)

    canonical_constraints = (
        "ck_refinement_delivery_context",
        "ck_refinement_snapshot_delivery_context",
        "ck_refinement_snapshot_source_context",
        "ck_spec_delivery_context",
        "ck_spec_delivery_context_provenance",
        "ck_spec_source_context",
        "ck_code_investigation_receipt_context_v2",
        "ck_code_evidence_source_role",
        "ck_code_evidence_context_v2",
    )

    class FakePostgresqlConnection:
        def __init__(self, engine: "FakePostgresqlEngine") -> None:
            self.engine = engine
            self.dialect = SimpleNamespace(name="postgresql")
            self.statements = list(engine.statements)
            self.triggers = dict(engine.triggers)

        async def run_sync(self, callback):
            return callback(object())

        async def execute(self, statement, parameters=None) -> FakeResult:
            sql = " ".join(str(statement).split())
            if self.engine.fail_token and self.engine.fail_token in sql:
                raise RuntimeError("injected_contextual_postgresql_artifact_failure")
            if "information_schema.table_constraints" in sql:
                if "constraint_name =" in sql:
                    return FakeResult(scalar=1)
                return FakeResult(rows=tuple((name,) for name in canonical_constraints))
            if "FROM pg_trigger AS trigger" in sql:
                return FakeResult(rows=tuple(self.triggers.values()))
            if sql.startswith("CREATE TRIGGER"):
                match = re.search(
                    r'CREATE TRIGGER "([^"]+)" BEFORE .* ON "([^"]+)"',
                    sql,
                )
                assert match is not None
                name, table_name = match.groups()
                self.triggers[name] = {
                    "trigger_name": name,
                    "table_name": table_name,
                    "function_name": "pulse_contextual_code_evidence_guard_v2",
                    "trigger_type": 31,
                    "trigger_enabled": "O",
                }
            self.statements.append(sql)
            return FakeResult(rowcount=0)

    class FakeTransaction:
        def __init__(self, engine: "FakePostgresqlEngine") -> None:
            self.engine = engine
            self.connection = FakePostgresqlConnection(engine)

        async def __aenter__(self) -> FakePostgresqlConnection:
            return self.connection

        async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
            if exc_type is None:
                self.engine.statements = self.connection.statements
                self.engine.triggers = self.connection.triggers
            return False

    class FakePostgresqlEngine:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.triggers: dict[str, dict[str, object]] = {}
            self.fail_token: str | None = None

        def begin(self) -> FakeTransaction:
            return FakeTransaction(self)

    fake_engine = FakePostgresqlEngine()
    monkeypatch.setattr(schema_steps, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(sqlalchemy, "inspect", lambda _connection: FakeInspector())
    monkeypatch.setattr(
        schema_steps,
        "_postgresql_owned_table_contract",
        lambda _connection, _table: {
            "observed": ("canonical",),
            "expected": ("canonical",),
        },
    )
    original_artifact = schema_steps.contextual_code_evidence_postgresql_ddl
    _function_sql, trigger_specs = original_artifact()
    sentinel = "SELECT 'injected_contextual_postgresql_artifact_failure'"
    monkeypatch.setattr(
        schema_steps,
        "contextual_code_evidence_postgresql_ddl",
        lambda: (sentinel, trigger_specs),
    )
    fake_engine.fail_token = "injected_contextual_postgresql_artifact_failure"

    with pytest.raises(
        RuntimeError,
        match="injected_contextual_postgresql_artifact_failure",
    ):
        asyncio.run(schema_steps._migrate_contextual_code_evidence_schema())
    assert fake_engine.statements == []
    assert fake_engine.triggers == {}

    monkeypatch.setattr(
        schema_steps,
        "contextual_code_evidence_postgresql_ddl",
        original_artifact,
    )
    fake_engine.fail_token = None
    assert asyncio.run(schema_steps._migrate_contextual_code_evidence_schema()) is None
    assert set(fake_engine.triggers) == set(trigger_specs)
    assert any(
        "pulse_contextual_code_evidence_guard_v2" in statement
        for statement in fake_engine.statements
    )
    stable_triggers = dict(fake_engine.triggers)
    assert (
        asyncio.run(schema_steps._migrate_contextual_code_evidence_schema())
        == "skipped"
    )
    assert fake_engine.triggers == stable_triggers


def test_postgresql_guard_ddl_is_source_blind_and_closed() -> None:
    ddl, trigger_specs = schema_steps.contextual_code_evidence_postgresql_ddl()
    normalized_ddl = " ".join(ddl.split())
    assert set(trigger_specs) == {
        "trg_contextual_code_evidence_v2_event_iud",
        "trg_contextual_code_evidence_v2_head_iud",
    }
    assert "pulse_contextual_code_evidence_guard_v2" in ddl
    assert "uncategorized_legacy" in ddl
    assert "kg_board_erasure_permits" in ddl
    assert 'length(NEW."batch_id") > 255' in normalized_ddl
    assert 'length(NEW."idempotency_key") > 512' in normalized_ddl
    forbidden = ("git ", "filesystem", "pathlib", "subprocess", "provider")
    assert not any(token in ddl.casefold() for token in forbidden)


def test_postgresql_artifact_is_deterministic_and_carries_every_context_field() -> None:
    table_ddl = " ".join(
        str(
            CreateTable(CodeEvidenceRow.__table__).compile(dialect=postgresql.dialect())
        ).split()
    )
    event_ddl = " ".join(
        str(
            CreateTable(CodeEvidenceClassificationEventRow.__table__).compile(
                dialect=postgresql.dialect()
            )
        ).split()
    )
    first = schema_steps.contextual_code_evidence_postgresql_ddl()
    second = schema_steps.contextual_code_evidence_postgresql_ddl()

    assert first == second
    for role in (
        "current_implementation",
        "existing_scaffold",
        "existing_constraint",
        "reference_pattern",
        "uncategorized_legacy",
    ):
        assert role in table_ddl
    for field in (
        "source_role",
        "relevance_summary",
        "scope_relation",
        "source_origin",
        "interpretation_limit",
        "baseline_presence",
        "baseline_workspace_state_id",
        "baseline_provenance_note",
        "context_contract_version",
    ):
        assert f'"{field}"' in table_ddl or field in table_ddl

    function_sql, trigger_specs = first
    assert set(trigger_specs) == {
        "trg_contextual_code_evidence_v2_event_iud",
        "trg_contextual_code_evidence_v2_head_iud",
    }
    for field in (
        "evidence_payload_sha256",
        "source_role",
        "interpretation_limit",
        "baseline_presence",
        "baseline_workspace_state_id",
        "baseline_provenance_note",
        "classification_sha256",
        "request_sha256",
    ):
        assert field in event_ddl
    for guarded_field in (
        "evidence_payload_sha256",
        "source_role",
        "baseline_presence",
        "baseline_workspace_state_id",
        "request_sha256",
    ):
        assert f'"{guarded_field}"' in function_sql
