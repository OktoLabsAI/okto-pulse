"""PostgreSQL parity contracts for the append-only research decision ledger."""

from __future__ import annotations

import pytest

from okto_pulse.community.adapters.relational_schema_steps import (
    audit_research_decision_postgresql_trigger_rows,
    research_decision_postgresql_ddl,
)


def _catalog_rows() -> list[dict[str, object]]:
    _function_sql, trigger_specs = research_decision_postgresql_ddl()
    return [
        {
            "trigger_name": trigger_name,
            "table_name": table_name,
            "function_name": "pulse_research_decision_immutable_guard",
            "trigger_type": trigger_type,
            "trigger_enabled": "O",
        }
        for trigger_name, (
            table_name,
            _operations,
            trigger_type,
        ) in trigger_specs.items()
    ]


def test_rdl_postgresql_manifest_guards_all_immutable_tables() -> None:
    function_sql, trigger_specs = research_decision_postgresql_ddl()

    assert trigger_specs == {
        "trg_rdl_entries_immutable": (
            "research_decision_entries",
            "UPDATE OR DELETE",
            27,
        ),
        "trg_rdl_history_immutable": (
            "research_decision_history",
            "UPDATE OR DELETE",
            27,
        ),
        "trg_rdl_snapshots_immutable": (
            "research_decision_snapshots",
            "UPDATE OR DELETE",
            27,
        ),
        "trg_rdl_derivations_immutable": (
            "research_decision_derivations",
            "UPDATE OR DELETE",
            27,
        ),
    }
    assert "research_decision_entry_immutable" in function_sql
    assert "kg_board_erasure_permits" in function_sql
    assert "quality_assessment_subject_erasure_permits" in function_sql
    assert "TG_TABLE_NAME = 'research_decision_derivations'" in function_sql
    assert "to_jsonb(OLD) ->> 'spec_id'" in function_sql
    assert "to_jsonb(OLD) ->> 'source_refinement_id'" in function_sql
    assert "to_jsonb(OLD) ->> 'refinement_id'" in function_sql
    assert "IF TG_OP = 'DELETE'" in function_sql
    assert "IF TG_OP = 'UPDATE'" not in function_sql


def test_rdl_postgresql_auditor_accepts_exact_or_reports_missing() -> None:
    rows = _catalog_rows()

    assert audit_research_decision_postgresql_trigger_rows(rows) == ()
    asyncpg_rows = [dict(row, trigger_enabled=b"O") for row in rows]
    assert audit_research_decision_postgresql_trigger_rows(asyncpg_rows) == ()
    missing = rows.pop()
    assert audit_research_decision_postgresql_trigger_rows(rows) == (
        missing["trigger_name"],
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("table_name", "research_decision_heads"),
        ("function_name", "unsafe_guard"),
        ("trigger_type", 19),
        ("trigger_enabled", "D"),
    ),
)
def test_rdl_postgresql_auditor_rejects_corruption(
    field: str,
    value: object,
) -> None:
    rows = _catalog_rows()
    rows[0][field] = value

    with pytest.raises(
        RuntimeError,
        match="research decision ledger PostgreSQL trigger is corrupt",
    ):
        audit_research_decision_postgresql_trigger_rows(rows)


def test_rdl_postgresql_auditor_rejects_unexpected_owned_trigger() -> None:
    rows = _catalog_rows()
    rows.append(
        {
            "trigger_name": "trg_rdl_rogue",
            "table_name": "research_decision_entries",
            "function_name": "pulse_research_decision_immutable_guard",
            "trigger_type": 27,
            "trigger_enabled": "O",
        }
    )

    with pytest.raises(
        RuntimeError,
        match="unexpected PostgreSQL triggers",
    ):
        audit_research_decision_postgresql_trigger_rows(rows)
