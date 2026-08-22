"""Audited repair of Spec Validation pointers lost by legacy side effects."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Spec,
    SpecValidationPointerRepairRow,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _attempt(
    validation_id: str,
    *,
    outcome: str,
    head: int,
    edition: int = 2,
    created_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": validation_id,
        "validation_id": validation_id,
        "spec_id": "placeholder",
        "board_id": "board-1",
        "outcome": outcome,
        "edition": edition,
        "validation_edition": edition,
        "subject_version": 4,
        "head_revision": head,
        "created_at": (created_at or NOW + timedelta(minutes=head)).isoformat(),
    }


async def _seed_spec(
    session,
    *,
    spec_id: str,
    status: str,
    validations: list[object],
) -> None:
    canonical = []
    for raw in validations:
        if isinstance(raw, dict):
            canonical.append({**raw, "spec_id": spec_id})
        else:
            canonical.append(raw)
    await session.execute(
        Spec.__table__.insert().values(
            id=spec_id,
            board_id="board-1",
            title=spec_id,
            status=status,
            edition=2,
            version=4,
            validations=canonical,
            current_validation_id=None,
            created_by="owner",
        )
    )


async def test_repair_restores_only_latest_unambiguous_success_and_replays(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'pointer-repair.db').as_posix()}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES ('board-1', 'Board', 'owner', 'local')"
            )
        )
    async with factory.begin() as session:
        await _seed_spec(
            session,
            spec_id="restored-approved",
            status="approved",
            validations=[_attempt("val_approved", outcome="success", head=1)],
        )
        await _seed_spec(
            session,
            spec_id="restored-validated",
            status="validated",
            validations=[
                _attempt("val_failed", outcome="failed", head=1),
                _attempt("val_success", outcome="success", head=2),
            ],
        )
        await _seed_spec(
            session,
            spec_id="restored-started",
            status="in_progress",
            validations=[_attempt("val_started", outcome="success", head=1)],
        )
        await _seed_spec(
            session,
            spec_id="newer-failure",
            status="done",
            validations=[
                _attempt("val_old_ok", outcome="success", head=1),
                _attempt("val_new_fail", outcome="failed", head=2),
            ],
        )
        await _seed_spec(
            session,
            spec_id="draft-excluded",
            status="draft",
            validations=[_attempt("val_draft", outcome="success", head=1)],
        )

    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        assert await steps._migrate_restore_spec_validation_pointers() is None
        async with factory() as session:
            pointers = {
                row.id: row.current_validation_id
                for row in (
                    await session.execute(select(Spec).order_by(Spec.id))
                ).scalars()
            }
            assert pointers == {
                "draft-excluded": None,
                "newer-failure": None,
                "restored-approved": "val_approved",
                "restored-started": "val_started",
                "restored-validated": "val_success",
            }
            audits = {
                row.spec_id: row
                for row in (
                    await session.execute(
                        select(SpecValidationPointerRepairRow).order_by(
                            SpecValidationPointerRepairRow.spec_id
                        )
                    )
                ).scalars()
            }
            assert set(audits) == {
                "newer-failure",
                "restored-approved",
                "restored-started",
                "restored-validated",
            }
            assert audits["restored-validated"].migration_state == "restored"
            assert audits["restored-validated"].candidate_validation_id == (
                "val_success"
            )
            assert audits["newer-failure"].migration_state == "latest_not_success"
            assert audits["newer-failure"].candidate_validation_id == "val_new_fail"
            assert audits["newer-failure"].reason_code == (
                "latest_current_edition_validation_failed"
            )
        assert await steps._migrate_restore_spec_validation_pointers() == "skipped"
        async with factory() as session:
            assert (
                await session.scalar(
                    select(text("count(*)")).select_from(
                        SpecValidationPointerRepairRow
                    )
                )
                == 4
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("spec_id", "validations", "reason_code"),
    (
        (
            "duplicate-id",
            [
                _attempt("val_dup", outcome="failed", head=1),
                _attempt("val_dup", outcome="success", head=2),
            ],
            "validation_id_duplicate",
        ),
        (
            "head-conflict",
            [
                _attempt("val_one", outcome="failed", head=2),
                _attempt("val_two", outcome="success", head=1),
            ],
            "validation_head_order_invalid",
        ),
        (
            "timestamp-conflict",
            [
                _attempt("val_one", outcome="failed", head=1, created_at=NOW),
                _attempt(
                    "val_two",
                    outcome="success",
                    head=2,
                    created_at=NOW - timedelta(minutes=1),
                ),
            ],
            "validation_timestamp_order_invalid",
        ),
        (
            "malformed-record",
            [_attempt("val_one", outcome="success", head=1), "not-an-object"],
            "validation_record_malformed",
        ),
    ),
)
async def test_repair_audits_ambiguous_evidence_without_pointer_mutation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    spec_id: str,
    validations: list[object],
    reason_code: str,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / f'{spec_id}.db').as_posix()}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES ('board-1', 'Board', 'owner', 'local')"
            )
        )
    async with factory.begin() as session:
        await _seed_spec(
            session,
            spec_id=spec_id,
            status="validated",
            validations=validations,
        )

    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        assert await steps._migrate_restore_spec_validation_pointers() is None
        async with factory() as session:
            spec = await session.get(Spec, spec_id)
            assert spec is not None
            assert spec.current_validation_id is None
            audit = await session.scalar(
                select(SpecValidationPointerRepairRow).where(
                    SpecValidationPointerRepairRow.spec_id == spec_id
                )
            )
            assert audit is not None
            assert audit.migration_state == "ambiguous_evidence"
            assert audit.candidate_validation_id is None
            assert audit.reason_code == reason_code
        assert await steps._migrate_restore_spec_validation_pointers() == "skipped"
    finally:
        await engine.dispose()
