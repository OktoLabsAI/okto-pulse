"""Candidate SQL qualification follows real reuse, scoped claims and debt CAS."""
import pytest

from okto_pulse.community.adapters import relational_schema_steps
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from okto_pulse.community.adapters.retirement_learning_sql_delta import verify_learning_sql_delta
from okto_pulse.core.domain.learning_materialization_work import LearningCaptureWorkRef
from okto_pulse.core.ports.learning_reconciliation import execute_learning_reconciliation
from test_learning_reconciliation_effects import sql_snapshot
from test_learning_materialization_writer import (
    BOARD, runtime as _runtime, independent_gates as _independent_gates, graph_runtime as _graph_runtime,
)
from test_learning_reuse_materialization import add_second_bug, stage_reuse
from test_learning_scoped_materialization import prepare_replacement

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('kind', ['reuse', 'scoped', 'debt'])
async def test_sql_ownership_for_existing_governed_variants(graph_runtime, monkeypatch, tmp_path, kind):
    runtime, original, selection, persister = graph_runtime
    factory, _, _, _ = runtime
    bug_id = 'second-bug'
    if kind == 'scoped':
        capture, _, _ = await prepare_replacement(graph_runtime)
    else:
        assert await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
        if kind == 'reuse':
            await add_second_bug(factory)
            capture, _ = await stage_reuse(graph_runtime, bug_id=bug_id)
        else:
            from okto_pulse.community.adapters.sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore
            from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
            from okto_pulse.core.services.canonical_debt_service import upsert_canonical_debt
            from okto_pulse.core.kg.canonical_learning_partition import (
                HISTORICAL_DEBT_REASON, PARTITION_TARGET_STATUS, _stable_content_hash,
            )
            register_canonical_debt_store(CommunitySqlAlchemyCanonicalDebtStore())
            capture, bug_id = original, 'bug-context'
            async with factory() as session:
                await upsert_canonical_debt(session, board_id=BOARD, artifact_type='bug', artifact_id=bug_id,
                    source_ref='bug:' + bug_id, content_hash=_stable_content_hash('bug:' + bug_id, capture.node_id),
                    target_status=PARTITION_TARGET_STATUS, canonical_state='pending', failure_reason=HISTORICAL_DEBT_REASON)
                await session.commit()
    monkeypatch.setattr(relational_schema_steps, 'get_engine', lambda: factory.kw['bind'])
    await relational_schema_steps._migrate_global_discovery_recovery_control_plane()
    work = LearningCaptureWorkRef(bug_id, capture.node_id, capture.generation, capture.record_fingerprint).encode()
    sql_snapshot(factory, tmp_path / 'before.sqlite3')
    execution = await execute_learning_reconciliation(board_id=BOARD, work_ref=work, relational_scope_factory=factory)
    assert execution.materialized
    sql_snapshot(factory, tmp_path / 'after.sqlite3')
    verified = await verify_learning_sql_delta(tmp_path / 'before.sqlite3', tmp_path / 'after.sqlite3',
        execution=execution, deadline=_deadline(60))
    assert verified['source_append_count'] == {'reuse': 1, 'scoped': 2, 'debt': 0}[kind]
    assert verified['technical_debt_count'] == (1 if kind == 'debt' else 0)
    assert verified['node_ref_count'] == (1 if kind == 'scoped' else 0)
