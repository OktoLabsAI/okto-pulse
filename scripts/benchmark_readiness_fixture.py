"""Reproducible isolated load fixture for persisted readiness.

Defaults match the Spec population: 10 Specs, 100 Cards and 100 obligations
per Spec, 200 scenarios, 2000 ledger facts and 50 historical versions. Run
with --specs 1 --facts 20 for a quick functional smoke before the full load.
No user database is opened.
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, process_time

from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / 'okto-pulse' / 'src'),
                str(WORKSPACE / 'okto-pulse-core' / 'src')]

from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, create_current_schema_objects,
    seed_current_schema_fences,
)
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import (
    CommunityDeliveryReadinessReader, CommunityReadinessClock,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier,
    CommunityTestVerificationReportIssuer,
)
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS, delivery_digest
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.core.ports.test_evidence import (
    TestVerificationReportRequest, register_test_evidence_write_verifier,
    reset_test_evidence_write_verifier_for_tests,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256


def percentile(values: list[float], percentile_value: float) -> float:
    return sorted(values)[max(0, int(len(values) * percentile_value + .999999) - 1)]


def register_fixture_verifier(path: Path) -> None:
    """Expose the real Community verifier capability to the isolated runtime."""
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(
        ledger=CommunityEvidenceLedger(evidence_root=path.parent / 'evidence')))


def _seed_synthetic_execution(connection, *, spec_id: str, index: int,
                              observed: datetime) -> None:
    """Create internally consistent source facts in the disposable fixture only.

    These rows are synthetic workload input, not an example of an authorized
    agent submission or a replacement for code-traceability integration tests.
    """
    card_id = f'{spec_id}-task-{index:03d}'
    request_id = f'{spec_id}-request-{index:03d}'
    receipt_id = f'{spec_id}-receipt-{index:03d}'
    target_id = f'{spec_id}-target-{index:03d}'
    execution_id = f'{spec_id}-execution-{index:03d}'
    source_ref = f'repo:benchmark/{spec_id}/{index:03d}'
    def digest(value: str) -> str:
        return sha256(value.encode()).hexdigest()
    created = observed - timedelta(minutes=30)
    expires = observed + timedelta(days=1)
    connection.exec_driver_sql(
        "INSERT INTO code_investigation_requests (id,board_id,subject_type,subject_id,"
        "subject_version,issued_to_actor_id,source_ref,required_capabilities,selector_scope_digest,"
        "expected_head_generation,canonicalization_profile,limits_profile,challenge_key_id,"
        "challenge_token_hash,status,expires_at,requested_by,created_at,consumed_at,"
        "request_payload_sha256,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (request_id, 'benchmark-board', 'card', card_id, 1, 'benchmark-agent',
         source_ref, '[]', digest(source_ref), 0, 'fixture-v1', 'fixture-v1', 'fixture',
         digest(request_id), 'open', expires.isoformat(), 'benchmark-agent',
         created.isoformat(), None, digest(request_id + ':payload'), request_id))
    connection.exec_driver_sql(
        "INSERT INTO code_investigation_receipts (id,request_id,board_id,subject_type,subject_id,"
        "subject_version,attestor_actor_id,generation,trust_level,acceptance_status,delivery_context,"
        "contextual_outcome,context_contract_version,capabilities,source_ref,canonicalization_profile,"
        "limits_profile,selector_scope_digest,declared_revision,workspace_state_id,declared_dirty,"
        "reproducibility_claim,fingerprint_algorithm,manifest_digest,manifest_entry_count,"
        "omission_manifest,omission_digest,omission_count,tooling,observed_at,received_at,"
        "expires_at,observation_sha256,payload_sha256,idempotency_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (receipt_id, request_id, 'benchmark-board', 'card', card_id, 1, 'benchmark-agent',
         1, 'single_attestation', 'accepted', 'brownfield', 'evidence_applicable', 2,
         '[]', source_ref, 'fixture-v1', 'fixture-v1', digest(source_ref), 'a' * 40,
         f'{spec_id}-workspace-{index:03d}', 0, 'committed', 'sha256',
         digest(receipt_id + ':manifest'), 1, '[]', digest('[]'), 0, '{}',
         observed.isoformat(), observed.isoformat(), expires.isoformat(),
         digest(receipt_id + ':observation'), digest(receipt_id + ':payload'), receipt_id))
    connection.exec_driver_sql(
        "UPDATE code_investigation_requests SET status='consumed',consumed_at=? WHERE id=?",
        (observed.isoformat(), request_id))
    connection.exec_driver_sql(
        "INSERT INTO code_investigation_heads (board_id,source_ref,generation,latest_receipt_id,"
        "current_receipt_id,state,revision,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ('benchmark-board', source_ref, 1, receipt_id, receipt_id, 'current', 1,
         observed.isoformat()))
    connection.exec_driver_sql(
        "INSERT INTO implementation_targets (id,board_id,card_id,source_ref,selector_kind,"
        "relative_path_hint,role,intent,required,source_spec_version,lifecycle_status,"
        "revision,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (target_id, 'benchmark-board', card_id, source_ref, 'file',
         f'src/benchmark_{index:03d}.py', 'modify', 'Synthetic fixture target', 1,
         1, 'active', 1, 'benchmark-agent', created.isoformat(), created.isoformat()))
    connection.exec_driver_sql(
        "INSERT INTO implementation_target_execution_records (id,board_id,card_id,target_id,"
        "target_revision,result_investigation_receipt_id,source_ref,disposition,"
        "result_declared_revision,result_workspace_state_id,actual_relative_path,"
        "justification,submitted_by,received_at,payload_sha256,idempotency_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (execution_id, 'benchmark-board', card_id, target_id, 1, receipt_id, source_ref,
         'touched', 'a' * 40, f'{spec_id}-workspace-{index:03d}',
         f'src/benchmark_{index:03d}.py', 'Synthetic fixture execution',
         'benchmark-agent', observed.isoformat(), digest(execution_id), execution_id))


def seed(path: Path, args: argparse.Namespace) -> None:
    profile = getattr(args, 'fixture_profile', 'simple')
    if profile not in {'simple', 'mixed'}:
        raise ValueError(f'fixture_profile_unknown:{profile}')
    if profile == 'mixed' and (args.cards < 2 or args.cards % 2 or args.obligations < 2
                               or args.obligations % 2 or args.scenarios < args.obligations // 2):
        raise ValueError('mixed_fixture_dimensions_invalid')
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
    engine = create_engine(f'sqlite:///{path}')
    try:
        with engine.begin() as connection:
            create_current_schema_objects(connection)
            seed_current_schema_fences(connection)
            connection.exec_driver_sql(f'PRAGMA main.application_id={APPLICATION_ID}')
            connection.exec_driver_sql(f'PRAGMA main.user_version={SCHEMA_VERSION}')
            connection.exec_driver_sql("INSERT INTO boards (id,name,owner_id,realm_id) "
                "VALUES ('benchmark-board','Benchmark','owner','local')")
            connection.exec_driver_sql("INSERT INTO agents "
                "(id,name,api_key,api_key_hash,is_active,permission_flags,created_by) "
                "VALUES ('benchmark-agent','Benchmark agent','fixture-key-not-used',"
                "'fixture-hash-not-used',1,?,'benchmark')",
                (json.dumps(registered_permission_flags(), sort_keys=True),))
            connection.exec_driver_sql("INSERT INTO agent_boards "
                "(id,agent_id,board_id,granted_by) VALUES "
                "('benchmark-agent-grant','benchmark-agent','benchmark-board','benchmark')")
            for number in range(args.specs):
                spec_id = f'benchmark-spec-{number:03d}'
                overloaded = number == 0 and getattr(args, 'overload_first', False)
                card_count = args.overload_cards if overloaded else args.cards
                obligation_count = args.overload_obligations if overloaded else args.obligations
                scenario_count = args.overload_scenarios if overloaded else args.scenarios
                fact_count = args.overload_facts if overloaded else args.facts
                task_count = card_count // 2
                if profile == 'mixed':
                    requirement_count = obligation_count // 2
                    requirements = [{
                        'id': f'fr-{index:03d}', 'text': f'Benchmark obligation {index}',
                        'linked_task_ids': [f'{spec_id}-task-{index % task_count:03d}'],
                        'verification': {'mode': 'explicit', 'required_profiles': ['functional']},
                        'implementation_plan': {'contributions': [{
                            'card_id': f'{spec_id}-task-{index % task_count:03d}',
                            'scope': 'whole_requirement',
                        }]},
                    } for index in range(requirement_count)]
                    criteria = [{
                        'id': f'ac-{index:03d}', 'text': f'Benchmark criterion {index}',
                        'verification_profile': 'functional',
                        'requirement_links': [{'requirement_type': 'functional_requirement',
                                               'requirement_id': f'fr-{index % requirement_count:03d}'}],
                        'linked_task_ids': [f'{spec_id}-task-{index % task_count:03d}'],
                    } for index in range(obligation_count - requirement_count)]
                    scenarios = [{
                        'id': f'sc-{index:03d}', 'title': f'Benchmark scenario {index}',
                        'given': 'A committed source state', 'when': 'the projection runs',
                        'then': 'the result is traceable', 'status': 'ready',
                        'verification_method': 'inspection', 'scenario_type': 'e2e',
                        'linked_task_ids': [f'{spec_id}-test-{index % task_count:03d}'],
                        'linked_criteria': [f'ac-{index % len(criteria):03d}'],
                    } for index in range(scenario_count)]
                    if scenario_count > 2:
                        scenarios[2] = {**scenarios[2], 'status': 'passed',
                                        'evidence': {'unavailable': True}}
                else:
                    requirements = [{
                        'id': f'fr-{index:03d}', 'text': f'Benchmark obligation {index}',
                        'linked_task_ids': [f'{spec_id}-task-{index % task_count:03d}'],
                    } for index in range(obligation_count)]
                    criteria = []
                    scenarios = [{
                        'id': f'sc-{index:03d}', 'title': f'Benchmark scenario {index}',
                        'given': 'A committed source state', 'when': 'the projection runs',
                        'then': 'the result is traceable', 'status': 'planned',
                    } for index in range(scenario_count)]
                connection.exec_driver_sql("INSERT INTO specs "
                    "(id,board_id,title,status,version,created_by,architecture_adoption,"
                    "functional_requirements,acceptance_criteria,test_scenarios,execution_contract) "
                    "VALUES (?,?,'Benchmark','in_progress',1,'owner','{}',?,?,?,?)",
                    (spec_id, 'benchmark-board', json.dumps(requirements), json.dumps(criteria), json.dumps(scenarios),
                     json.dumps({'contract_version': 'spec-execution-contract/v1',
                        'board_id': 'benchmark-board', 'spec_id': spec_id,
                        'adopted_in_edition': 1, 'actor_id': 'owner', 'origin': 'new_spec'})))
                if profile == 'mixed':
                    empty_collections = [field for _, field in COLLECTIONS
                        if field not in {'functional_requirements', 'acceptance_criteria'}]
                    connection.exec_driver_sql("UPDATE specs SET " + ", ".join(
                        f"{field}='[]'" for field in empty_collections) + " WHERE id=?", (spec_id,))
                cards = []
                for index in range(card_count):
                    is_test = index >= task_count
                    card_id = f'{spec_id}-' + ('test' if is_test else 'task') + f'-{index % task_count:03d}'
                    assigned = ([f'sc-{scenario:03d}' for scenario in range(scenario_count)
                        if scenario % task_count == index % task_count] if is_test else [])
                    status = ('done' if profile == 'mixed' and index % task_count < 10
                              else 'in_progress')
                    cards.append((card_id, 'benchmark-board', spec_id, card_id,
                        'test' if is_test else 'normal', status, index,
                        'owner', json.dumps(assigned)))
                connection.exec_driver_sql("INSERT INTO cards "
                    "(id,board_id,spec_id,title,card_type,status,position,created_by,test_scenario_ids) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", cards)
                if args.history:
                    history = [(f'{spec_id}-hist-{index:03d}', spec_id, 'updated',
                        'agent', 'benchmark', 'Benchmark', json.dumps([]), index + 1)
                        for index in range(args.history)]
                    connection.exec_driver_sql("INSERT INTO spec_history "
                        "(id,spec_id,action,actor_type,actor_id,actor_name,changes,version) "
                        "VALUES (?,?,?,?,?,?,?,?)", history)
                if fact_count:
                    facts = []
                    now = datetime.now(timezone.utc).isoformat()
                    revoke_count = min(fact_count // 20, 20) if profile == 'mixed' else 0
                    proof_task_count = min(5, task_count) if profile == 'mixed' else 0
                    verification_task_count = min(2, proof_task_count)
                    proof_scenario_count = (sum(index % len(criteria) < verification_task_count
                        for index in range(scenario_count)) if proof_task_count else 0)
                    implementation_revoke_count = 1 if proof_task_count >= 5 else 0
                    progress_count = (fact_count - revoke_count - proof_task_count
                                      - proof_scenario_count - implementation_revoke_count)
                    if progress_count < revoke_count or progress_count < 0:
                        raise ValueError('mixed_fixture_facts_insufficient_for_proofs')
                    for index in range(progress_count):
                        card_id = cards[index % len(cards)][0]
                        record_id = f'{spec_id}-fact-{index:05d}'
                        payload = ({'progress': {
                            'contract_version': 'delivery-progress/v2',
                            'material_change': ('unknown' if index % 10 == 0 and
                                index % task_count >= 10 else 'none'),
                            'source_state': {'workspace_state': 'unknown', 'recoverability': 'unknown'},
                            'target_ids': [], 'remaining': f'Benchmark checkpoint {index}',
                        }, 'justification': f'Benchmark progress {index}'}
                            if profile == 'mixed' else {'note': record_id})
                        facts.append((record_id, 'benchmark-board', card_id, spec_id, 1,
                            'progress', 'benchmark', 'agent', record_id,
                            sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                            json.dumps(payload), now))
                    for index in range(revoke_count):
                        revoked_id = f'{spec_id}-fact-{index:05d}'
                        record_id = f'{spec_id}-revoke-{index:03d}'
                        card_id = cards[index % len(cards)][0]
                        payload = {'record_id': revoked_id, 'justification': 'Fixture revocation'}
                        facts.append((record_id, 'benchmark-board', card_id, spec_id, 1,
                            'revoke', 'owner', 'user', record_id,
                            sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                            json.dumps(payload), now))
                    connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                        "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                        "idempotency_key,payload_sha256,payload,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", facts)
                    if profile == 'mixed':
                        observed = datetime.now(timezone.utc) - timedelta(minutes=10)
                        for index in range(proof_task_count):
                            _seed_synthetic_execution(connection, spec_id=spec_id,
                                index=index, observed=observed)
                if profile == 'mixed':
                    waivers = []
                    for index in range(min(5, requirement_count)):
                        requirement = requirements[-index - 1]
                        semantic = {key: value for key, value in requirement.items()
                                    if key not in {'linked_task_ids', 'status', 'created_at', 'updated_at'}}
                        binding = {'obligation_ref': f"fr:{requirement['id']}",
                                   'semantic_sha256': delivery_digest(semantic)}
                        record_id = f'{spec_id}-waiver-{index:03d}'
                        payload = {'bindings': [binding], 'phase': 'implementation',
                                   'justification': 'Synthetic authorized exception for load testing'}
                        waivers.append((record_id, 'benchmark-board', spec_id, 1, 'waiver',
                            'owner', 'user', record_id,
                            sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                            json.dumps(payload), datetime.now(timezone.utc).isoformat()))
                    connection.exec_driver_sql("INSERT INTO delivery_evidence_records "
                        "(id,board_id,spec_id,edition,kind,actor_id,actor_kind,idempotency_key,"
                        "payload_sha256,payload,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", waivers)
    finally:
        engine.dispose()


async def enrich_mixed_fixture(path: Path, args: argparse.Namespace) -> None:
    """Associate synthetic accepted executions through the native Card writer."""
    if getattr(args, 'fixture_profile', 'simple') != 'mixed':
        return
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    try:
        async with build_community_session_factory(engine)() as session:
            store = CommunityDeliveryEvidenceStore(session)
            issuer = CommunityTestVerificationReportIssuer(
                ledger=CommunityEvidenceLedger(evidence_root=path.parent / 'evidence'))
            task_count = args.cards // 2
            for spec_number in range(args.specs):
                spec_id = f'benchmark-spec-{spec_number:03d}'
                spec = await session.scalar(select(Spec).where(Spec.id == spec_id))
                assert spec is not None
                implementation_ids = {}
                for index in range(min(5, task_count)):
                    card_id = f'{spec_id}-task-{index:03d}'
                    refs = [f'fr:fr-{index:03d}', f'ac:ac-{index:03d}']
                    command = CardDeliveryEvidenceCommand(
                        board_id='benchmark-board', spec_id=spec_id, card_id=card_id,
                        expected_spec_edition=1, expected_card_version=1,
                        idempotency_key=f'{spec_id}-implementation-{index:03d}',
                        kind='implementation', execution_id=f'{spec_id}-execution-{index:03d}',
                        bindings=[{'obligation_ref': ref, 'contribution': 'complete'} for ref in refs],
                        justification='Synthetic accepted execution for isolated load testing',
                    )
                    result = await store.record_card(command, actor_id='benchmark-agent', actor_kind='agent')
                    implementation_ids[index] = result['id']
                if len(implementation_ids) >= 5:
                    await store.record_card(CardDeliveryEvidenceCommand(
                        board_id='benchmark-board', spec_id=spec_id,
                        card_id=f'{spec_id}-task-004',
                        expected_spec_edition=1, expected_card_version=1,
                        idempotency_key=f'{spec_id}-implementation-revocation',
                        kind='revoke', record_id=implementation_ids[4],
                        justification='Synthetic human revocation for isolated load testing',
                    ), actor_id='owner', actor_kind='user')
                await session.flush()
                criterion_count = len(spec.acceptance_criteria)
                scenarios = list(spec.test_scenarios)
                for offset, scenario in enumerate(scenarios):
                    index = offset % criterion_count
                    if index >= min(2, len(implementation_ids)):
                        continue
                    scenario_id = scenario['id']
                    criterion_id = f'ac-{index:03d}'
                    source_ref = f'repo:benchmark/{spec_id}/{index:03d}'
                    source = {'reference': source_ref, 'revision': 'a' * 40,
                              'sha256': sha256(source_ref.encode()).hexdigest()}
                    report = {'schema_version': 'verification-report/v1',
                        'method': 'inspection', 'report_id': f'{spec_id}-report-{offset:03d}',
                        'observed_at': datetime.now(timezone.utc).isoformat(),
                        'sources': [source], 'conclusion': 'Synthetic fixture inspection passed',
                        'result': 'passed', 'observations': [{
                            'observation_id': f'{spec_id}-observation-{offset:03d}',
                            'criterion_id': criterion_id,
                            'observation_ref': source_ref,
                            'expected': 'Current accepted implementation',
                            'observed': 'Current accepted implementation',
                            'outcome': 'passed'}], 'inspection_procedure': source}
                    digest = compute_test_scenario_semantic_sha256(
                        board_id='benchmark-board', spec_id=spec_id, scenario=scenario,
                        acceptance_criteria=spec.acceptance_criteria)
                    issued = await issuer.admit(TestVerificationReportRequest(
                        board_id='benchmark-board', spec_id=spec_id,
                        scenario_id=scenario_id, scenario_sha256=digest,
                        actor_id='benchmark-agent', report=report))
                    scenarios[offset] = {**scenario, 'status': 'passed',
                                         'evidence': dict(issued.evidence)}
                spec.test_scenarios = scenarios
                await session.flush()
                for offset, scenario in enumerate(scenarios):
                    index = offset % criterion_count
                    if index >= min(2, len(implementation_ids)):
                        continue
                    await store.record_card(CardDeliveryEvidenceCommand(
                        board_id='benchmark-board', spec_id=spec_id,
                        card_id=f'{spec_id}-test-{index:03d}',
                        expected_spec_edition=1, expected_card_version=1,
                        idempotency_key=f'{spec_id}-test-association-{offset:03d}',
                        kind='test', scenario_id=scenario['id'],
                        implementation_ids=[implementation_ids[index]],
                        obligation_refs=[f'fr:fr-{index:03d}', f'ac:ac-{index:03d}'],
                        justification='Synthetic observed inspection for isolated load testing',
                    ), actor_id='benchmark-agent', actor_kind='agent')
                await session.commit()
    finally:
        await engine.dispose()


def audit_fixture(path: Path, args: argparse.Namespace) -> dict:
    """Reject load results whose actual population differs from the manifest."""
    profile = getattr(args, 'fixture_profile', 'simple')
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as connection:
        states_by_spec = {}
        waiver_counts = {}
        for number in range(args.specs):
            spec_id = f'benchmark-spec-{number:03d}'
            overloaded = number == 0 and getattr(args, 'overload_first', False)
            expected_cards = args.overload_cards if overloaded else args.cards
            expected_obligations = args.overload_obligations if overloaded else args.obligations
            expected_scenarios = args.overload_scenarios if overloaded else args.scenarios
            expected_facts = args.overload_facts if overloaded else args.facts
            spec = connection.execute("SELECT functional_requirements,acceptance_criteria,"
                "test_scenarios FROM specs WHERE id=?", (spec_id,)).fetchone()
            if spec is None:
                raise AssertionError(f'fixture_spec_missing:{spec_id}')
            requirements, criteria, scenarios = (json.loads(value) for value in spec)
            if (len(requirements) + len(criteria) != expected_obligations
                    or len(scenarios) != expected_scenarios):
                raise AssertionError(f'fixture_normative_population_mismatch:{spec_id}')
            cards = connection.execute("SELECT count(*) FROM cards WHERE spec_id=?", (spec_id,)).fetchone()[0]
            facts = connection.execute("SELECT count(*) FROM card_delivery_evidence_records "
                "WHERE spec_id=?", (spec_id,)).fetchone()[0]
            history = connection.execute("SELECT count(*) FROM spec_history WHERE spec_id=?",
                (spec_id,)).fetchone()[0]
            if (cards, facts, history) != (expected_cards, expected_facts, args.history):
                raise AssertionError(f'fixture_size_mismatch:{spec_id}:{cards}/{facts}/{history}')
            waivers = connection.execute("SELECT count(*) FROM delivery_evidence_records "
                "WHERE spec_id=? AND kind='waiver'", (spec_id,)).fetchone()[0]
            waiver_counts[spec_id] = waivers
            payloads = connection.execute("SELECT u.payload FROM readiness_unit u "
                "JOIN readiness_generation g ON g.id=u.generation_id "
                "JOIN readiness_scope s ON s.id=g.scope_id "
                "WHERE s.spec_id=? AND g.id=s.active_generation_id", (spec_id,)).fetchall()
            states = Counter((value['implementation_state'], value['verification_state'])
                for (payload,) in payloads if (value := json.loads(payload)))
            states_by_spec[spec_id] = {f'{implementation}/{verification}': count
                for (implementation, verification), count in sorted(states.items())}
            if profile == 'mixed':
                expected = {'satisfied/missing', 'satisfied/satisfied', 'stale/missing',
                            'not_applicable/missing', 'not_applicable/satisfied'}
                if expected_obligations > 10:
                    expected.add('missing/missing')
                if not expected.issubset(states_by_spec[spec_id]) or waivers == 0:
                    raise AssertionError(
                        f'fixture_mixed_states_missing:{spec_id}:states={states_by_spec[spec_id]}'
                        f':waivers={waivers}')
        return {'profile': profile, 'states_by_spec': states_by_spec,
                'waivers_by_spec': waiver_counts}


async def benchmark(path: Path, args: argparse.Namespace) -> dict:
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    try:
        worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
        started, cpu_start = perf_counter(), process_time()
        publications = 0
        while publications < args.specs:
            publications += await worker.process_once()
            if perf_counter() - started > args.projection_timeout:
                raise TimeoutError(f'projection_timeout:{publications}/{args.specs}')
            await asyncio.sleep(.06)
        initial_seconds = perf_counter() - started
        reader = CommunityDeliveryReadinessReader(engine, clock=CommunityReadinessClock())
        first = 'benchmark-spec-000'
        legacy, persisted = [], []
        for _ in range(args.reads):
            began = perf_counter()
            await reader.load(board_id='benchmark-board', spec_id=first)
            legacy.append((perf_counter() - began) * 1000)
            began = perf_counter()
            result = await worker.store.read(board_id='benchmark-board', spec_id=first, edition=1)
            persisted.append((perf_counter() - began) * 1000)
            if result is None or result.freshness != 'current':
                raise AssertionError('read_result_not_current')
        audit = audit_fixture(path, args)
        return {'fixture': {'specs': args.specs, 'cards_per_spec': args.cards,
                'obligations_per_spec': args.obligations, 'scenarios_per_spec': args.scenarios,
                'facts_per_spec': args.facts, 'history_per_spec': args.history,
                'profile': getattr(args, 'fixture_profile', 'simple')},
            'unit_states': audit['states_by_spec'][first],
            'fixture_audit': {'waivers_by_spec': audit['waivers_by_spec']},
            'initial_projection_s': round(initial_seconds, 3),
            'cpu_s': round(process_time() - cpu_start, 3),
            'reads_per_mode': args.reads,
            'legacy_p50_ms': round(median(legacy), 3),
            'legacy_p95_ms': round(percentile(legacy, .95), 3),
            'persisted_p50_ms': round(median(persisted), 3),
            'persisted_p95_ms': round(percentile(persisted, .95), 3),
            'generation_id': result.generation_id,
            'summary_count': len(result.summaries),
            'pool': engine.sync_engine.pool.status(),
            'wal_bytes': path.with_name(path.name + '-wal').stat().st_size
                if path.with_name(path.name + '-wal').exists() else 0}
    finally:
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--specs', type=int, default=10)
    parser.add_argument('--cards', type=int, default=100)
    parser.add_argument('--obligations', type=int, default=100)
    parser.add_argument('--scenarios', type=int, default=200)
    parser.add_argument('--facts', type=int, default=2000)
    parser.add_argument('--history', type=int, default=50)
    parser.add_argument('--fixture-profile', choices=('simple', 'mixed'), default='mixed')
    parser.add_argument('--reads', type=int, default=20)
    parser.add_argument('--projection-timeout', type=float, default=600)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='readiness-benchmark-') as folder:
        path = Path(folder) / 'pulse.db'
        seed(path, args)
        register_fixture_verifier(path)
        try:
            asyncio.run(enrich_mixed_fixture(path, args))
            print(json.dumps(asyncio.run(benchmark(path, args)), sort_keys=True), flush=True)
        finally:
            reset_test_evidence_write_verifier_for_tests()
