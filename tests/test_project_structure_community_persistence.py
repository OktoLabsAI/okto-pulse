"""Current Project structure storage, CAS, exact replay and reference fences."""

from __future__ import annotations

import pytest
from sqlalchemy import JSON, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.community.adapters.sqlalchemy_models import (
    ProjectStructureMutationReceiptRow,
)
from okto_pulse.community.adapters.sqlalchemy_structured_spec import (
    CommunitySqlAlchemyStructuredSpecStore,
)
from okto_pulse.core.ports.structured_spec import (
    ProjectStructureMutationPersistenceState,
    ProjectStructureMutationReceipt,
    StructuredSpecRecord,
)


def test_project_structure_orm_column_is_nullable_json_without_defaults() -> None:
    column = Spec.__table__.c.project_structure

    assert isinstance(column.type, JSON)
    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None
    assert list(Spec.__table__.columns)[-1] is column
    assert Spec.__table__.c.project_structure_revision.nullable is True
    assert Spec.__table__.c.project_structure_digest.nullable is True


def _record_for_atomic_write(*, version: int = 2) -> StructuredSpecRecord:
    return StructuredSpecRecord(
        id="spec-atomic",
        board_id="board-atomic",
        status="draft",
        version=version,
        archived=False,
        functional_requirements=None,
        business_rules=None,
        technical_requirements=None,
        decisions=None,
        acceptance_criteria=None,
        api_contracts=None,
        integration_requirements=None,
        observability_requirements=None,
        test_scenarios=None,
        project_structure=[],
        project_structure_revision=1,
        project_structure_digest="a" * 64,
    )


@pytest.mark.asyncio
async def test_atomic_project_structure_write_cas_and_exact_replay(tmp_path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'project-structure-atomic.db'}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE specs ("
                "id VARCHAR(36) PRIMARY KEY, version INTEGER NOT NULL, "
                "updated_at DATETIME, project_structure_revision INTEGER, "
                "project_structure_digest VARCHAR(64), project_structure JSON)"
            )
        )
        await connection.run_sync(ProjectStructureMutationReceiptRow.__table__.create)
        await connection.execute(
            text(
                "INSERT INTO specs (id, version, project_structure_revision, project_structure) "
                "VALUES ('spec-atomic', 1, 0, NULL)"
            )
        )
    store = CommunitySqlAlchemyStructuredSpecStore()
    receipt = ProjectStructureMutationReceipt(
        spec_id="spec-atomic",
        idempotency_key="project-structure:key-1",
        request_digest="b" * 64,
        result={"success": True, "spec_version": 2},
    )

    async with sessions() as session:
        applied = await store.save_project_structure_mutation(
            session,
            _record_for_atomic_write(),
            expected_spec_version=1,
            expected_project_structure_revision=0,
            bump_spec_version=True,
            changed_fields=(
                "project_structure",
                "project_structure_revision",
                "project_structure_digest",
            ),
            receipt=receipt,
        )
        await session.commit()
    assert applied.state is ProjectStructureMutationPersistenceState.APPLIED

    receipt.result["spec_version"] = 999
    async with sessions() as session:
        replay = await store.save_project_structure_mutation(
            session,
            _record_for_atomic_write(),
            expected_spec_version=1,
            expected_project_structure_revision=0,
            bump_spec_version=True,
            changed_fields=(
                "project_structure",
                "project_structure_revision",
                "project_structure_digest",
            ),
            receipt=ProjectStructureMutationReceipt(
                spec_id="spec-atomic",
                idempotency_key="project-structure:key-1",
                request_digest="b" * 64,
                result={"success": True, "spec_version": 2},
            ),
        )
        assert replay.state is ProjectStructureMutationPersistenceState.REPLAYED
        assert replay.receipt is not None
        assert replay.receipt.result["spec_version"] == 2

        conflict = await store.save_project_structure_mutation(
            session,
            _record_for_atomic_write(),
            expected_spec_version=2,
            expected_project_structure_revision=1,
            bump_spec_version=False,
            changed_fields=(),
            receipt=ProjectStructureMutationReceipt(
                spec_id="spec-atomic",
                idempotency_key="project-structure:key-1",
                request_digest="c" * 64,
                result={"success": True},
            ),
        )
        assert (
            conflict.state
            is ProjectStructureMutationPersistenceState.IDEMPOTENCY_CONFLICT
        )

        relation_only = _record_for_atomic_write(version=2)
        relation_only.project_structure_revision = 2
        relation_only.project_structure_digest = "c" * 64
        applied_relation = await store.save_project_structure_mutation(
            session,
            relation_only,
            expected_spec_version=2,
            expected_project_structure_revision=1,
            bump_spec_version=False,
            changed_fields=(
                "project_structure",
                "project_structure_revision",
                "project_structure_digest",
            ),
            receipt=ProjectStructureMutationReceipt(
                spec_id="spec-atomic",
                idempotency_key="project-structure:key-relation",
                request_digest="e" * 64,
                result={
                    "success": True,
                    "spec_version": 2,
                    "structure_revision": 2,
                },
            ),
        )
        assert (
            applied_relation.state
            is ProjectStructureMutationPersistenceState.APPLIED
        )

        stale_revision = _record_for_atomic_write(version=2)
        stale_revision.project_structure_revision = 2
        stale_revision.project_structure_digest = "f" * 64
        rejected_relation = await store.save_project_structure_mutation(
            session,
            stale_revision,
            expected_spec_version=2,
            expected_project_structure_revision=1,
            bump_spec_version=False,
            changed_fields=(
                "project_structure",
                "project_structure_revision",
                "project_structure_digest",
            ),
            receipt=ProjectStructureMutationReceipt(
                spec_id="spec-atomic",
                idempotency_key="project-structure:key-stale-revision",
                request_digest="f" * 64,
                result={"success": True},
            ),
        )
        assert (
            rejected_relation.state
            is ProjectStructureMutationPersistenceState.VERSION_CONFLICT
        )

        stale = await store.save_project_structure_mutation(
            session,
            _record_for_atomic_write(version=8),
            expected_spec_version=7,
            expected_project_structure_revision=0,
            bump_spec_version=True,
            changed_fields=(
                "project_structure",
                "project_structure_revision",
                "project_structure_digest",
            ),
            receipt=ProjectStructureMutationReceipt(
                spec_id="spec-atomic",
                idempotency_key="project-structure:key-stale",
                request_digest="d" * 64,
                result={"success": True, "spec_version": 8},
            ),
        )
        assert stale.state is ProjectStructureMutationPersistenceState.VERSION_CONFLICT
        missing = await store.get_project_structure_receipt(
            session,
            spec_id="spec-atomic",
            idempotency_key="project-structure:key-stale",
        )
        assert missing is None
        row = (
            await session.execute(
                text(
                    "SELECT version, project_structure_revision, "
                    "project_structure_digest, project_structure "
                    "FROM specs WHERE id='spec-atomic'"
                )
            )
        ).one()
        assert tuple(row) == (2, 2, "c" * 64, "[]")
    await engine.dispose()


@pytest.mark.asyncio
async def test_reference_validation_accepts_normal_and_test_but_rejects_bug(
    tmp_path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'project-structure-references.db'}"
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE cards ("
                "id VARCHAR(36) PRIMARY KEY, board_id VARCHAR(36) NOT NULL, "
                "spec_id VARCHAR(36), card_type VARCHAR(20) NOT NULL, "
                "archived BOOLEAN NOT NULL DEFAULT 0)"
            )
        )
        await connection.execute(
            text(
                "CREATE TABLE code_evidence ("
                "id VARCHAR(64) PRIMARY KEY, board_id VARCHAR(36) NOT NULL, "
                "spec_id VARCHAR(36), lifecycle_status VARCHAR(16) NOT NULL)"
            )
        )
        await connection.execute(
            text(
                "CREATE TABLE code_evidence_spec_links ("
                "evidence_id VARCHAR(64), board_id VARCHAR(36) NOT NULL, "
                "spec_id VARCHAR(36) NOT NULL)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO cards (id, board_id, spec_id, card_type, archived) "
                "VALUES ('task-normal', 'board-ref', 'spec-ref', 'normal', 0), "
                "('test-card', 'board-ref', 'spec-ref', 'test', 0), "
                "('bug-card', 'board-ref', 'spec-ref', 'bug', 0)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO code_evidence "
                "(id, board_id, spec_id, lifecycle_status) VALUES "
                "('evidence-active', 'board-ref', NULL, 'active'), "
                "('evidence-revoked', 'board-ref', NULL, 'revoked')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO code_evidence_spec_links "
                "(evidence_id, board_id, spec_id) VALUES "
                "('evidence-active', 'board-ref', 'spec-ref'), "
                "('evidence-revoked', 'board-ref', 'spec-ref')"
            )
        )
    store = CommunitySqlAlchemyStructuredSpecStore()
    async with sessions() as session:
        await store.validate_project_structure_references(
            session,
            board_id="board-ref",
            spec_id="spec-ref",
            task_ids=("task-normal",),
            test_ids=("test-card",),
            evidence_ids=("evidence-active",),
        )
        with pytest.raises(
            ValueError,
            match="project_structure_task_reference_invalid",
        ):
            await store.validate_project_structure_references(
                session,
                board_id="board-ref",
                spec_id="spec-ref",
                task_ids=("bug-card",),
                test_ids=(),
                evidence_ids=(),
            )
        with pytest.raises(
            ValueError,
            match="project_structure_evidence_reference_invalid",
        ):
            await store.validate_project_structure_references(
                session,
                board_id="board-ref",
                spec_id="spec-ref",
                task_ids=(),
                test_ids=(),
                evidence_ids=("evidence-revoked",),
            )
    await engine.dispose()
