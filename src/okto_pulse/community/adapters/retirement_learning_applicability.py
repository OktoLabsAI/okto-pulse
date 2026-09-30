"""Read-only applicability on the private candidate's authenticated snapshot."""

from dataclasses import asdict
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.composition import isolated_runtime_provider_scope
from okto_pulse.core.ports.application_persistence import register_application_persistence_port
from okto_pulse.core.ports.bug_cognitive_context import register_bug_cognitive_context_assembler
from okto_pulse.core.ports.kg_cognitive_source import register_cognitive_source_store
from okto_pulse.core.ports.learning_reconciliation import (
    LearningReconciliationExecution, observe_learning_reconciliation_applicability,
)
from okto_pulse.core.ports.test_evidence import register_test_evidence_write_verifier

from .bug_cognitive_context import CommunityBugCognitiveContextAssembler
from .joint_recovery_snapshot import _explicit_path
from .relational_recovery_snapshot import _check_time, _deadline, _sidecars_absent
from .sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from .sqlalchemy_database import build_community_session_factory
from .sqlalchemy_kg_cognitive_source import CommunitySqlAlchemyCognitiveSourceStore
from .test_evidence import CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier


async def observe_candidate_learning_applicability(target, executions, *, max_seconds):
    """Recheck source/evidence without composing a writable Pulse runtime.

    The caller authenticates the candidate inventory and holds its offline
    window before and after this call. Each result describes this SQL snapshot,
    not a durable authority to mutate later. Missing/changed evidence raises.
    """
    if (type(executions) is not tuple or len(executions) > 100_000
            or any(type(row) is not LearningReconciliationExecution for row in executions)
            or len({(row.board_id, row.work_ref) for row in executions}) != len(executions)):
        raise ValueError('retirement_learning_applicability_scope_invalid')
    if len(json.dumps([asdict(row) for row in executions]).encode('utf-8')) > 64 * 1024 * 1024:
        raise ValueError('retirement_learning_applicability_limit')
    deadline, target = _deadline(max_seconds), _explicit_path(Path(target))
    database = _explicit_path(target / 'database.sqlite3')
    if not database.is_file():
        raise ValueError('retirement_learning_applicability_database_missing')
    _sidecars_absent(database)
    engine = create_async_engine(f'sqlite+aiosqlite:///{database}')
    try:
        factory = build_community_session_factory(engine)
        with isolated_runtime_provider_scope(inherit=False):
            register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
            register_bug_cognitive_context_assembler(CommunityBugCognitiveContextAssembler())
            register_cognitive_source_store(CommunitySqlAlchemyCognitiveSourceStore(factory))
            register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(
                ledger=CommunityEvidenceLedger(evidence_root=target / 'evidence')))
            async with engine.connect() as connection:
                await connection.exec_driver_sql('PRAGMA query_only=ON')
                await connection.exec_driver_sql('BEGIN')
                try:
                    async with factory(bind=connection, autoflush=False,
                            join_transaction_mode='create_savepoint') as session:
                        observed, size = [], 0
                        for execution in executions:
                            _check_time(deadline)
                            row = await observe_learning_reconciliation_applicability(session, execution=execution)
                            size += len(json.dumps(asdict(row)).encode('utf-8'))
                            if size > 64 * 1024 * 1024:
                                raise ValueError('retirement_learning_applicability_limit')
                            observed.append(row)
                finally:
                    await connection.rollback()
    finally:
        await engine.dispose()
    _check_time(deadline)
    _sidecars_absent(database)
    return tuple(observed)


async def observe_verified_learning_phase_applicability(target, phase, *, max_seconds):
    """Compose fresh observations after the caller rederives the entire phase.

    Unmaterialized work remains explicit. Neither an empty list nor this report
    certifies complete selection, history ownership or runtime admission.
    """
    from .retirement_learning_execution import VerifiedLearningPhase

    if type(phase) is not VerifiedLearningPhase:
        raise TypeError('retirement_learning_verified_phase_required')
    executions = tuple(LearningReconciliationExecution(**step['execution'])
        for board in phase.boards for step in board['steps'])
    if len(executions) > 100_000 or any(type(row.materialized) is not bool for row in executions):
        raise ValueError('retirement_learning_applicability_scope_invalid')
    observations = await observe_candidate_learning_applicability(target,
        tuple(row for row in executions if row.materialized), max_seconds=max_seconds)
    result = {'format': 'retirement-learning-applicability/v1', 'state': 'observed_not_admitted',
        'observations': [asdict(row) for row in observations],
        'unmaterialized': [asdict(row) for row in executions if not row.materialized]}
    if len(json.dumps(result).encode('utf-8')) > 64 * 1024 * 1024:
        raise ValueError('retirement_learning_applicability_limit')
    return result
