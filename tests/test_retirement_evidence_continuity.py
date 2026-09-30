"""A private candidate must still authenticate the original signed evidence."""
from contextlib import closing
import json
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import retirement_graph_candidate as candidate
from okto_pulse.community.adapters.sqlalchemy_database import CommunityDatabaseRuntime, build_community_session_factory
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier,
)
from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.core.ports.test_evidence import TestVerificationReportRequest as ReportRequest
from test_retirement_offline_run import SOURCE, MIGRATION
from test_retirement_v034_source import restore_source
from test_verification_report_admission import report


@pytest.mark.asyncio
async def test_signed_receipt_remains_authentic_in_restored_private_candidate(tmp_path):
    source = restore_source(tmp_path)
    for name in ('uploads', 'kg', 'backups', 'candidate-backups'):
        (tmp_path / name).mkdir()
    original_ledger = CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence')
    issued = await CommunityTestVerificationReportIssuer(ledger=original_ledger).admit(
        ReportRequest(board_id='board-a', spec_id='spec-a', scenario_id='signed-scenario',
            scenario_sha256='sha256:' + 'a' * 64, actor_id='reviewer', report=report()))
    evidence = dict(issued.evidence)
    verification = dict(board_id='board-a', spec_id='spec-a', scenario_id='signed-scenario',
        scenario_sha256=evidence['scenario_sha256'], status='passed', actor_id=None, evidence=evidence)
    assert CommunityTestEvidenceWriteVerifier(ledger=original_ledger).verify(**verification).verified
    # Populate only disposable predecessor data, before the original backup.
    with closing(sqlite3.connect(source)) as sql:
        with sql:
            sql.execute('UPDATE specs SET test_scenarios=? WHERE id=?',
                (json.dumps([{'id': 'signed-scenario', 'status': 'passed', 'evidence': evidence}]), 'spec-a'))
    original_files = {path.relative_to(original_ledger.evidence_root): path.read_bytes()
        for path in original_ledger.evidence_root.rglob('*') if path.is_file()}
    engine = create_async_engine(f'sqlite+aiosqlite:///{source}')
    runtime = CommunityDatabaseRuntime(engine, build_community_session_factory(engine))
    storage = CommunityFileSystemStorage(str(tmp_path / 'uploads'))
    try:
        run = await offline.prepare_offline_retirement_run(runtime, storage, (),
            tmp_path / 'backups', tmp_path / 'run', snapshot_id='original',
            plan=ContextDispositionPlan(migration_id='signed-evidence-fixture',
                decision_reference='fixture without substantive Sprint context', decisions=()),
            source_builds=SOURCE, migration_builds=MIGRATION,
            runtime_directories=(tmp_path, tmp_path / 'kg'), kg_base_dir=tmp_path / 'kg',
            evidence_root=original_ledger.evidence_root)
        projection = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        seed = await candidate.prepare_retirement_candidate_seed(runtime, storage, (), run,
            projection['projection_inputs'], migration_builds=MIGRATION,
            recovery_directory=tmp_path / 'candidate-backups', seed_directory=tmp_path / 'seed')
        target = tmp_path / 'candidate'
        await candidate.restore_retirement_graph_candidate(runtime, storage, (), run, seed, target,
            migration_builds=MIGRATION, confirm_original_offline=True)
        restored = CommunityEvidenceLedger(evidence_root=target / 'evidence')
        observed = CommunityTestEvidenceWriteVerifier(ledger=restored).verify(**verification)
        assert observed.verified, observed
        assert {path.relative_to(restored.evidence_root): path.read_bytes()
            for path in restored.evidence_root.rglob('*') if path.is_file()} == original_files
        assert {path.relative_to(original_ledger.evidence_root): path.read_bytes()
            for path in original_ledger.evidence_root.rglob('*') if path.is_file()} == original_files
    finally:
        await runtime.close()
