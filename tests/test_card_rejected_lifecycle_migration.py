"""Community convergence for the authored Card Rejected lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from okto_pulse.community.adapters import relational_schema_steps as steps
from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.domain.card_completion import resolve_current_rejection_record

pytestmark = pytest.mark.asyncio

_LONG_VALIDATION_ID = "validation-" + ("x" * 100)


async def _legacy_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE cards ("
                "id VARCHAR(36) PRIMARY KEY, board_id VARCHAR(36) NOT NULL, "
                "card_type VARCHAR(50) NOT NULL DEFAULT 'normal', "
                "status VARCHAR(50) NOT NULL, position INTEGER NOT NULL DEFAULT 0, "
                "archived BOOLEAN NOT NULL DEFAULT 0, validations JSON, "
                "created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL, "
                "policy_version INTEGER NOT NULL DEFAULT 1)"
            )
        )
    return engine


def _validation(
    validation_id: str,
    *,
    outcome: str,
    card_id: str,
    board_id: str = "b1",
    justification: str | None = None,
) -> str:
    return json.dumps(
        [
            {
                "id": validation_id,
                "card_id": card_id,
                "board_id": board_id,
                "outcome": outcome,
                "verdict": "fail" if outcome == "failed" else "pass",
                "recommendation": "reject" if outcome == "failed" else "approve",
                "general_justification": justification,
                "threshold_violations": (
                    ["completeness_below_minimum"] if outcome == "failed" else []
                ),
            }
        ]
    )


async def _seed(engine: AsyncEngine) -> None:
    values = (
        {
            "id": "existing",
            "card_type": "normal",
            "status": "rejected",
            "position": 10,
            "archived": 0,
            "validations": _validation(
                "v-existing", outcome="failed", card_id="existing"
            ),
        },
        {
            "id": "existing-archived",
            "card_type": "normal",
            "status": "rejected",
            "position": 0,
            "archived": 1,
            "validations": _validation(
                "v-existing-archived",
                outcome="failed",
                card_id="existing-archived",
            ),
        },
        {
            "id": "normal-failed",
            "card_type": "normal",
            "status": "validation",
            "position": 3,
            "archived": 0,
            "validations": _validation(
                _LONG_VALIDATION_ID,
                outcome="failed",
                card_id="normal-failed",
                justification="  Implementation needs explicit rework.  ",
            ),
        },
        {
            "id": "bug-failed",
            "card_type": "bug",
            "status": "validation",
            "position": 1,
            "archived": 0,
            "validations": _validation("v-bug", outcome="failed", card_id="bug-failed"),
        },
        {
            "id": "normal-failed-archived",
            "card_type": "normal",
            "status": "validation",
            "position": 0,
            "archived": 1,
            "validations": _validation(
                "v-archived", outcome="failed", card_id="normal-failed-archived"
            ),
        },
        {
            "id": "test-failed",
            "card_type": "test",
            "status": "validation",
            "position": 0,
            "archived": 0,
            "validations": _validation(
                "v-test", outcome="failed", card_id="test-failed"
            ),
        },
        {
            "id": "normal-success",
            "card_type": "normal",
            "status": "validation",
            "position": 2,
            "archived": 0,
            "validations": _validation(
                "v-success", outcome="success", card_id="normal-success"
            ),
        },
        {
            "id": "normal-ambiguous",
            "card_type": "normal",
            "status": "validation",
            "position": 4,
            "archived": 0,
            "validations": json.dumps([{"outcome": "failed"}]),
        },
    )
    async with engine.begin() as connection:
        for value in values:
            await connection.execute(
                text(
                    "INSERT INTO cards "
                    "(id, board_id, card_type, status, position, archived, "
                    "validations, created_at, updated_at, policy_version) "
                    "VALUES (:id, 'b1', :card_type, :status, :position, :archived, "
                    ":validations, '2026-08-14 10:00:00', "
                    "'2026-08-14 10:00:00', 7)"
                ),
                value,
            )


async def _snapshot(engine: AsyncEngine) -> list[dict[str, object]]:
    async with engine.connect() as connection:
        return [
            dict(row)
            for row in (
                await connection.execute(
                    text(
                        "SELECT id, status, position, archived, policy_version, "
                        "current_rejection_kind, current_rejection_id, "
                        "current_rejection_code, current_rejection_summary "
                        "FROM cards ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        ]


async def _rejection_histories(
    engine: AsyncEngine,
) -> dict[str, list[dict[str, object]]]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT id, rejection_records FROM cards ORDER BY id")
            )
        ).mappings()
        result: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            raw = row["rejection_records"]
            decoded = json.loads(raw) if isinstance(raw, str) else raw
            result[str(row["id"])] = list(decoded or [])
        return result


async def _validation_histories(
    engine: AsyncEngine,
) -> dict[str, list[dict[str, object]]]:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT id, validations FROM cards ORDER BY id")
            )
        ).mappings()
        result: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            raw = row["validations"]
            decoded = json.loads(raw) if isinstance(raw, str) else raw
            result[str(row["id"])] = list(decoded or [])
        return result


async def test_legacy_failed_normal_and_bug_cards_converge_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "legacy.db")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        await _seed(engine)
        assert await steps._migrate_card_rejected_lifecycle() is None

        snapshot = {row["id"]: row for row in await _snapshot(engine)}
        histories = await _rejection_histories(engine)
        validation_histories = await _validation_histories(engine)
        normal_history = histories["normal-failed"]
        assert len(normal_history) == 1
        normal_record = normal_history[0]
        assert normal_record["kind"] == "task_validation"
        assert normal_record["source_id"] == _LONG_VALIDATION_ID
        assert normal_record["code"] == "task_validation_failed"
        assert normal_record["reason_codes"] == [
            "completeness_below",
            "reject_recommendation",
        ]
        assert normal_record["subject_version"] == 7
        normal_validation = validation_histories["normal-failed"][-1]
        assert normal_validation["card_id"] == "normal-failed"
        assert normal_validation["board_id"] == "b1"
        assert normal_validation["expected_subject_version"] == 7
        assert normal_validation["validation_outcome"] == "failed"
        assert normal_validation["completion_outcome"] == "rejected"
        assert snapshot["normal-failed"] == {
            "id": "normal-failed",
            "status": "rejected",
            "position": 2,
            "archived": 0,
            "policy_version": 8,
            "current_rejection_kind": "task_validation",
            "current_rejection_id": normal_record["id"],
            "current_rejection_code": "task_validation_failed",
            "current_rejection_summary": "Implementation needs explicit rework.",
        }
        assert snapshot["bug-failed"]["status"] == "rejected"
        assert snapshot["bug-failed"]["position"] == 1
        assert snapshot["normal-failed-archived"]["status"] == "rejected"
        assert snapshot["normal-failed-archived"]["position"] == 4
        assert snapshot["existing"]["position"] == 0
        assert snapshot["existing-archived"]["position"] == 3
        assert snapshot["existing"]["policy_version"] == 8
        assert snapshot["existing"]["current_rejection_kind"] == "task_validation"
        assert (
            snapshot["existing"]["current_rejection_id"]
            == histories["existing"][0]["id"]
        )
        assert histories["existing"][0]["source_id"] == "v-existing"
        assert snapshot["existing-archived"]["policy_version"] == 8
        assert (
            resolve_current_rejection_record(
                {
                    **snapshot["normal-failed"],
                    "board_id": "b1",
                    "validations": validation_histories["normal-failed"],
                    "rejection_records": normal_history,
                }
            )
            is not None
        )

        assert snapshot["test-failed"]["status"] == "validation"
        assert snapshot["test-failed"]["position"] == 0
        assert snapshot["normal-success"]["status"] == "validation"
        assert snapshot["normal-success"]["position"] == 1
        assert snapshot["normal-ambiguous"]["status"] == "validation"
        assert snapshot["normal-ambiguous"]["position"] == 2
        assert snapshot["test-failed"]["policy_version"] == 7

        async with engine.connect() as connection:
            columns = {
                row[1]: row[2]
                for row in (
                    await connection.execute(text("PRAGMA table_info(cards)"))
                ).all()
            }
            audit = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT card_id, migration_state, reason_code "
                            "FROM card_rejected_lifecycle_migrations "
                            "ORDER BY card_id"
                        )
                    )
                )
                .mappings()
                .all()
            ]
        assert {
            "rejection_records",
            "current_rejection_kind",
            "current_rejection_id",
            "current_rejection_code",
            "current_rejection_summary",
        } <= set(columns)
        assert columns["current_rejection_id"].upper() == "VARCHAR(128)"
        assert len(audit) == 8
        assert {(row["card_id"], row["migration_state"]) for row in audit} == {
            ("normal-failed", "migrated"),
            ("bug-failed", "migrated"),
            ("normal-failed-archived", "migrated"),
            ("test-failed", "excluded_test"),
            ("normal-success", "not_rejected"),
            ("normal-ambiguous", "ambiguous_evidence"),
            ("existing", "already_rejected"),
            ("existing-archived", "already_rejected"),
        }

        first = await _snapshot(engine)
        first_histories = await _rejection_histories(engine)
        assert await steps._migrate_card_rejected_lifecycle() == "skipped"
        assert await _snapshot(engine) == first
        assert await _rejection_histories(engine) == first_histories

        # The same audited legacy evidence is not allowed to drag a human's
        # rework transition back to Rejected on a later startup.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE cards SET status='validation', position=9, "
                    "current_rejection_kind=NULL, current_rejection_id=NULL, "
                    "current_rejection_code=NULL, current_rejection_summary=NULL "
                    "WHERE id='normal-failed'"
                )
            )
        # The hand-authored fixture move leaves both columns sparse, so the
        # recurring convergence may resequence positions; it must not change
        # the lifecycle decision or restore the cleared Current pointer.
        assert await steps._migrate_card_rejected_lifecycle() is None
        after_rework = {row["id"]: row for row in await _snapshot(engine)}[
            "normal-failed"
        ]
        assert after_rework["status"] == "validation"
        assert after_rework["current_rejection_id"] is None
        assert (await _rejection_histories(engine))["normal-failed"] == normal_history

        # The recurring step audits its own ledger instead of trusting a row
        # merely because the unique key exists.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE card_rejected_lifecycle_migrations SET details='{}' "
                    "WHERE card_id='test-failed'"
                )
            )
        with pytest.raises(
            RuntimeError,
            match="card rejected lifecycle migration audit conflict",
        ):
            await steps._migrate_card_rejected_lifecycle()
    finally:
        await engine.dispose()


async def test_legacy_rejected_without_verifiable_cause_is_quarantined_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "legacy-rejected-quarantine.db")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO cards "
                    "(id, board_id, card_type, status, position, archived, "
                    "validations, created_at, updated_at, policy_version) "
                    "VALUES ('ambiguous-rejected', 'b1', 'normal', 'rejected', "
                    "4, 0, :validations, '2026-08-14 10:00:00', "
                    "'2026-08-14 10:00:00', 7)"
                ),
                {"validations": json.dumps([{"outcome": "failed"}])},
            )

        assert await steps._migrate_card_rejected_lifecycle() is None
        first = (await _snapshot(engine))[0]
        assert first["status"] == "rejected"
        assert first["policy_version"] == 8
        assert first["current_rejection_kind"] == "completion_gate"
        assert first["current_rejection_code"] == ("legacy_rejected_cause_unresolved")
        assert "no verifiable rejection cause" in str(
            first["current_rejection_summary"]
        )

        async with engine.connect() as connection:
            history = json.loads(
                (
                    await connection.execute(
                        text(
                            "SELECT rejection_records FROM cards "
                            "WHERE id='ambiguous-rejected'"
                        )
                    )
                ).scalar_one()
            )
            audit = (
                (
                    await connection.execute(
                        text(
                            "SELECT migration_state, reason_code FROM "
                            "card_rejected_lifecycle_migrations WHERE "
                            "card_id='ambiguous-rejected'"
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert len(history) == 1
        assert history[0]["id"] == first["current_rejection_id"]
        assert history[0]["kind"] == "completion_gate"
        validation_history = (await _validation_histories(engine))["ambiguous-rejected"]
        quarantine_validation = validation_history[-1]
        assert quarantine_validation["id"] == history[0]["source_id"]
        assert quarantine_validation["validation_outcome"] == "success"
        assert quarantine_validation["completion_outcome"] == "rejected"
        assert (
            quarantine_validation["expected_subject_version"]
            == (history[0]["subject_version"])
        )
        assert (
            quarantine_validation["completion_gate_failures"][0]["code"]
            == "legacy_rejected_cause_unresolved"
        )
        assert (
            resolve_current_rejection_record(
                {
                    **first,
                    "board_id": "b1",
                    "validations": validation_history,
                    "rejection_records": history,
                }
            )
            is not None
        )
        assert audit == {
            "migration_state": "quarantined",
            "reason_code": "legacy_rejected_cause_unresolved",
        }

        snapshot = await _snapshot(engine)
        assert await steps._migrate_card_rejected_lifecycle() == "skipped"
        assert await _snapshot(engine) == snapshot
    finally:
        await engine.dispose()


async def test_existing_rejected_task_cause_uses_record_pointer_and_repairs_direct_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "legacy-task-record-pointer.db")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        # Materialize the new columns first, then emulate both the final authored
        # contract and the short-lived direct-validation-pointer bridge.
        assert await steps._migrate_card_rejected_lifecycle() is None
        valid_record = {
            "id": "rej-authored",
            "card_id": "authored",
            "board_id": "b1",
            "kind": "task_validation",
            "source_id": "validation-authored",
            "code": "task_validation_failed",
            "summary": "Authored rejection summary.",
            "reason_codes": ["completeness_below"],
            "created_by": "reviewer",
            "created_at": "2026-08-14T10:00:00+00:00",
            "subject_version": 7,
        }
        async with engine.begin() as connection:
            for card_id, validation_id, records, pointer, summary in (
                (
                    "authored",
                    "validation-authored",
                    [valid_record],
                    "rej-authored",
                    "Authored rejection summary.",
                ),
                (
                    "direct-bridge",
                    "validation-direct",
                    [],
                    "validation-direct",
                    "Bridge rejection summary.",
                ),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO cards "
                        "(id, board_id, card_type, status, position, archived, "
                        "validations, rejection_records, current_rejection_kind, "
                        "current_rejection_id, current_rejection_code, "
                        "current_rejection_summary, created_at, updated_at, "
                        "policy_version) VALUES "
                        "(:id, 'b1', 'normal', 'rejected', 0, 0, :validations, "
                        ":records, 'task_validation', :pointer, "
                        "'task_validation_failed', :summary, "
                        "'2026-08-14 10:00:00', '2026-08-14 10:00:00', 7)"
                    ),
                    {
                        "id": card_id,
                        "validations": _validation(
                            validation_id,
                            outcome="failed",
                            card_id=card_id,
                            justification=summary,
                        ),
                        "records": json.dumps(records),
                        "pointer": pointer,
                        "summary": summary,
                    },
                )

        assert await steps._migrate_card_rejected_lifecycle() is None
        snapshot = {row["id"]: row for row in await _snapshot(engine)}
        histories = await _rejection_histories(engine)

        assert snapshot["authored"]["current_rejection_id"] == "rej-authored"
        assert snapshot["authored"]["policy_version"] == 8
        assert histories["authored"] == [valid_record]

        repaired_history = histories["direct-bridge"]
        assert len(repaired_history) == 1
        assert repaired_history[0]["source_id"] == "validation-direct"
        assert repaired_history[0]["kind"] == "task_validation"
        assert (
            snapshot["direct-bridge"]["current_rejection_id"]
            == (repaired_history[0]["id"])
        )
        assert snapshot["direct-bridge"]["current_rejection_id"] != (
            "validation-direct"
        )
        assert snapshot["direct-bridge"]["policy_version"] == 8

        validation_histories = await _validation_histories(engine)
        for card_id in ("authored", "direct-bridge"):
            validation = validation_histories[card_id][-1]
            record = histories[card_id][0]
            assert validation["card_id"] == card_id
            assert validation["board_id"] == "b1"
            assert validation["expected_subject_version"] == record["subject_version"]
            assert validation["validation_outcome"] == "failed"
            assert validation["completion_outcome"] == "rejected"
            assert (
                resolve_current_rejection_record(
                    {
                        **snapshot[card_id],
                        "board_id": "b1",
                        "validations": validation_histories[card_id],
                        "rejection_records": histories[card_id],
                    }
                )
                is not None
            )

        first_snapshot = await _snapshot(engine)
        first_histories = await _rejection_histories(engine)
        assert await steps._migrate_card_rejected_lifecycle() == "skipped"
        assert await _snapshot(engine) == first_snapshot
        assert await _rejection_histories(engine) == first_histories
    finally:
        await engine.dispose()


async def test_incomplete_completion_gate_current_is_quarantined_with_resolvable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _legacy_engine(tmp_path / "legacy-incomplete-completion-gate.db")
    monkeypatch.setattr(steps, "get_engine", lambda: engine)
    try:
        assert await steps._migrate_card_rejected_lifecycle() is None
        incomplete_record = {
            "id": "gate-without-source",
            "card_id": "gate-incomplete",
            "board_id": "b1",
            "kind": "completion_gate",
            # Deliberately no source_id: the old structural check accepted this
            # even though the Core causal resolver correctly fails it closed.
            "code": "required_evidence_missing",
            "summary": "Evidence could not be verified.",
            "reason_codes": ["required_evidence_missing"],
            "created_by": "legacy-reviewer",
            "created_at": "2026-08-14T10:00:00+00:00",
            "subject_version": 7,
        }
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO cards "
                    "(id, board_id, card_type, status, position, archived, "
                    "validations, rejection_records, current_rejection_kind, "
                    "current_rejection_id, current_rejection_code, "
                    "current_rejection_summary, created_at, updated_at, "
                    "policy_version) VALUES "
                    "('gate-incomplete', 'b1', 'normal', 'rejected', 0, 0, "
                    ":validations, :records, 'completion_gate', "
                    "'gate-without-source', 'required_evidence_missing', "
                    "'Evidence could not be verified.', "
                    "'2026-08-14 10:00:00', '2026-08-14 10:00:00', 7)"
                ),
                {
                    "validations": _validation(
                        "validation-success",
                        outcome="success",
                        card_id="gate-incomplete",
                    ),
                    "records": json.dumps([incomplete_record]),
                },
            )

        assert await steps._migrate_card_rejected_lifecycle() is None
        snapshot = {row["id"]: row for row in await _snapshot(engine)}[
            "gate-incomplete"
        ]
        histories = (await _rejection_histories(engine))["gate-incomplete"]
        validations = (await _validation_histories(engine))["gate-incomplete"]

        assert snapshot["policy_version"] == 8
        assert snapshot["current_rejection_kind"] == "completion_gate"
        assert snapshot["current_rejection_code"] == (
            "legacy_rejected_cause_unresolved"
        )
        assert snapshot["current_rejection_id"] != "gate-without-source"
        assert len(histories) == 2
        assert histories[0] == incomplete_record
        current_record = next(
            item for item in histories if item["id"] == snapshot["current_rejection_id"]
        )
        assert current_record["source_id"] == validations[-1]["id"]
        assert (
            resolve_current_rejection_record(
                {
                    **snapshot,
                    "board_id": "b1",
                    "validations": validations,
                    "rejection_records": histories,
                }
            )
            is not None
        )

        first_snapshot = await _snapshot(engine)
        first_histories = await _rejection_histories(engine)
        first_validations = await _validation_histories(engine)
        assert await steps._migrate_card_rejected_lifecycle() == "skipped"
        assert await _snapshot(engine) == first_snapshot
        assert await _rejection_histories(engine) == first_histories
        assert await _validation_histories(engine) == first_validations
    finally:
        await engine.dispose()


async def test_rejected_lifecycle_step_is_registered() -> None:
    from okto_pulse.community.adapters.relational_schema_migrator import (
        build_community_migration_ledger,
        make_community_relational_schema_migrator,
    )

    step = next(
        item
        for item in build_community_migration_ledger()
        if item.step_id == "_migrate_card_rejected_lifecycle"
    )
    assert step.phase == "post_create_all"
    assert step.idempotent is True
    assert step.destructive is False
    migrator = make_community_relational_schema_migrator()
    migrator.validate_plan(migrator.plan(target="community-sqlite"))

    assert Card.__table__.c.current_rejection_id.type.length == 128
    postgres_ddl = str(
        CreateTable(Card.__table__).compile(dialect=postgresql.dialect())
    )
    assert "current_rejection_id VARCHAR(128)" in postgres_ddl
