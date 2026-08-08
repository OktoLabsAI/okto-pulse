"""SK-B3 PostgreSQL trigger proof v1.

The proof is intentionally opt-in: it uses an externally managed PostgreSQL
database and never starts a container or an Okto Pulse process.
"""

from __future__ import annotations

import os
from importlib import import_module
from typing import Any
from uuid import uuid4

import pytest

from okto_pulse.community.adapters.relational_schema_steps import (
    audit_research_decision_postgresql_trigger_rows,
    audit_semantic_guideline_postgresql_trigger_rows,
    research_decision_postgresql_ddl,
    semantic_guideline_postgresql_ddl,
)

pytestmark = pytest.mark.e2e

_POSTGRES_DSN_ENV = "OKTO_PULSE_TEST_POSTGRES_DSN"
_POSTGRES_DSN = os.environ.get(_POSTGRES_DSN_ENV)
asyncpg = import_module("asyncpg") if _POSTGRES_DSN else None

_SEMANTIC_TRIGGER_SPECS = {
    "trg_sgv3_revision": (
        "semantic_guideline_revisions",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_binding": (
        "semantic_guideline_binding_configurations",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_subject_head": (
        "semantic_subject_versions",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_subject_event": (
        "semantic_subject_version_events",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_receipt": (
        "semantic_guideline_assessment_receipts",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_metric": (
        "semantic_guideline_metric_results",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_finding": (
        "semantic_guideline_findings",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_waiver": (
        "semantic_guideline_waivers",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_waiver_event": (
        "semantic_guideline_waiver_events",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_skip": (
        "semantic_guideline_skips",
        "INSERT OR UPDATE OR DELETE",
        31,
    ),
    "trg_sgv3_migration": (
        "semantic_guideline_legacy_migrations",
        "UPDATE OR DELETE",
        27,
    ),
}

_RDL_TRIGGER_SPECS = {
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

_CATALOG_SQL = """
SELECT trigger.tgname AS trigger_name,
       relation.relname AS table_name,
       function.proname AS function_name,
       trigger.tgtype AS trigger_type,
       trigger.tgenabled AS trigger_enabled
FROM pg_trigger AS trigger
JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
JOIN pg_namespace AS relation_namespace
  ON relation_namespace.oid = relation.relnamespace
JOIN pg_proc AS function ON function.oid = trigger.tgfoid
WHERE NOT trigger.tgisinternal
  AND relation_namespace.nspname = current_schema()
  AND (
      trigger.tgname LIKE 'trg_sgv3_%'
      OR trigger.tgname LIKE 'trg_rdl_%'
  )
ORDER BY trigger.tgname
"""


def _catalog_for_prefix(
    rows: list[dict[str, object]],
    prefix: str,
) -> list[dict[str, object]]:
    return [
        row for row in rows if str(row["trigger_name"]).startswith(prefix)
    ]


async def _catalog_rows(connection: Any) -> list[dict[str, object]]:
    return [dict(row) for row in await connection.fetch(_CATALOG_SQL)]


async def _converge_triggers(connection: Any) -> tuple[str, ...]:
    semantic_function_sql, semantic_specs = semantic_guideline_postgresql_ddl()
    rdl_function_sql, rdl_specs = research_decision_postgresql_ddl()
    assert semantic_specs == _SEMANTIC_TRIGGER_SPECS
    assert rdl_specs == _RDL_TRIGGER_SPECS

    await connection.execute(semantic_function_sql)
    await connection.execute(rdl_function_sql)
    rows = await _catalog_rows(connection)
    missing_semantic = audit_semantic_guideline_postgresql_trigger_rows(
        _catalog_for_prefix(rows, "trg_sgv3_"),
        trigger_specs=semantic_specs,
    )
    missing_rdl = audit_research_decision_postgresql_trigger_rows(
        _catalog_for_prefix(rows, "trg_rdl_"),
        trigger_specs=rdl_specs,
    )

    missing = set((*missing_semantic, *missing_rdl))
    for trigger_name, (table_name, operations, _trigger_type) in {
        **semantic_specs,
        **rdl_specs,
    }.items():
        if trigger_name not in missing:
            continue
        function_name = (
            "semantic_guideline_guard_v3"
            if trigger_name.startswith("trg_sgv3_")
            else "pulse_research_decision_immutable_guard"
        )
        await connection.execute(
            f'CREATE TRIGGER "{trigger_name}" BEFORE {operations} '
            f'ON "{table_name}" FOR EACH ROW '
            f'EXECUTE FUNCTION "{function_name}"()'
        )
    return tuple(sorted(missing))


def _expected_catalog() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for function_name, specs in (
        ("semantic_guideline_guard_v3", _SEMANTIC_TRIGGER_SPECS),
        ("pulse_research_decision_immutable_guard", _RDL_TRIGGER_SPECS),
    ):
        rows.extend(
            {
                "trigger_name": trigger_name,
                "table_name": table_name,
                "function_name": function_name,
                "trigger_type": trigger_type,
                "trigger_enabled": "O",
            }
            for trigger_name, (
                table_name,
                _operations,
                trigger_type,
            ) in specs.items()
        )
    return sorted(rows, key=lambda row: str(row["trigger_name"]))


async def _create_minimal_authority_tables(connection: Any) -> None:
    table_names = {
        table_name
        for table_name, _operations, _trigger_type in (
            *_SEMANTIC_TRIGGER_SPECS.values(),
            *_RDL_TRIGGER_SPECS.values(),
        )
    }
    for table_name in sorted(table_names):
        await connection.execute(
            f'CREATE TABLE "{table_name}" ('
            "id text PRIMARY KEY, board_id text, refinement_id text, "
            "spec_id text, source_refinement_id text)"
        )
    await connection.execute(
        "CREATE TABLE kg_board_erasure_permits (board_id text PRIMARY KEY)"
    )
    await connection.execute(
        "CREATE TABLE quality_assessment_subject_erasure_permits ("
        "board_id text, subject_type text, subject_id text)"
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason=f"set {_POSTGRES_DSN_ENV} to run the PostgreSQL trigger proof",
)
async def test_skb3_postgresql_trigger_proof_v1() -> None:
    assert asyncpg is not None
    dsn = _POSTGRES_DSN.replace("postgresql+asyncpg://", "postgresql://", 1)
    schema_name = f"skb3_trigger_proof_v1_{uuid4().hex}"
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(f'CREATE SCHEMA "{schema_name}"')
        await connection.execute(f'SET search_path TO "{schema_name}"')
        await _create_minimal_authority_tables(connection)

        first_install = await _converge_triggers(connection)
        first_catalog = await _catalog_rows(connection)
        second_install = await _converge_triggers(connection)
        second_catalog = await _catalog_rows(connection)

        assert len(_SEMANTIC_TRIGGER_SPECS) == 11
        assert len(_RDL_TRIGGER_SPECS) == 4
        assert first_install == tuple(
            sorted((*_SEMANTIC_TRIGGER_SPECS, *_RDL_TRIGGER_SPECS))
        )
        assert first_catalog == _expected_catalog()
        assert second_install == ()
        assert second_catalog == first_catalog
        assert audit_semantic_guideline_postgresql_trigger_rows(
            _catalog_for_prefix(second_catalog, "trg_sgv3_")
        ) == ()
        assert audit_research_decision_postgresql_trigger_rows(
            _catalog_for_prefix(second_catalog, "trg_rdl_")
        ) == ()

        await connection.execute(
            "INSERT INTO semantic_guideline_legacy_migrations "
            "(id, board_id) VALUES ('migration-1', 'board-1')"
        )
        with pytest.raises(
            asyncpg.exceptions.RaiseError,
            match="semantic_guideline_migration_audit_immutable",
        ):
            await connection.execute(
                "UPDATE semantic_guideline_legacy_migrations "
                "SET board_id = 'board-2' WHERE id = 'migration-1'"
            )
    finally:
        await connection.execute("SET search_path TO pg_catalog")
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        await connection.close()
