"""R8 acceptance on real Grafx, SQL outbox, delivery ledger and writer leases.

The fixture oracle is authored from seed identities, never from either query.
Hooks delegate production operations and only observe or inject named faults.
No production database, queue, graph or lease is used by this module.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

import pytest
from okto_grafx import VectorValue, connect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.coordination import (
    CommunityLocalWriteLockPort, CommunitySqlAlchemyClaimRepository,
)
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_global_discovery import ensure_current_grafx_global_schema
from okto_pulse.community.adapters.grafx_global_discovery_runtime import CommunityGrafxGlobalDiscoveryRuntime
from okto_pulse.community.adapters.grafx_schema_manifest import GrafxSpaceManifest
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory, install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_ledger import CommunitySqlAlchemyDeliveryLedger
from okto_pulse.community.adapters.sqlalchemy_global_outbox import CommunitySqlAlchemyGlobalOutboxStore
from okto_pulse.community.adapters.sqlalchemy_models import Board, GlobalDiscoveryDeliveryLedger, GlobalUpdateOutbox
from okto_pulse.core.application.processors import global_outbox as worker_module
from okto_pulse.core.application.processors.global_outbox import DIGESTED_NODE_TYPES, GlobalOutboxProcessor
from okto_pulse.core.kg.global_discovery_writer import (
    GLOBAL_DISCOVERY_WRITER_SCOPE, assert_global_discovery_writer_fence,
    global_discovery_writer_scope,
)
from okto_pulse.core.kg.interfaces import KGProviderRegistry
from okto_pulse.core.kg.interfaces import registry as registry_module
from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock
from okto_pulse.core.kg.write_barrier import require_global_write_token
from okto_pulse.core.ports.coordination import register_coordination_providers
from okto_pulse.core.ports.delivery_ledger import DeliveryAttemptEnvelope, DeliveryState
from okto_pulse.core.ports.global_outbox import register_global_outbox_store


BOARDS = (
    'a1111111-1111-4111-8111-111111111111',
    'b2222222-2222-4222-8222-222222222222',
    'c3333333-3333-4333-8333-333333333333',
)
SIZES = (1200, 650, 650)
VECTOR = [1.0, *([0.0] * 383)]
BASELINE = ('MATCH (:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) '
            'WHERE d.board_id = $bid RETURN d.id, count(r)')
CANDIDATE = ('MATCH (d:DecisionDigest) WHERE d.board_id = $bid '
             'OPTIONAL MATCH (d)<-[r:CONTAINS_DECISION]-(:Board) RETURN d.id, count(r)')
EVENT = ContextVar('r8_acceptance_event', default=None)


def _digest(board, source):
    return f'dd_{board[:8]}_{source}'


def _seed_rows():
    return [dict(board=board, source=f'source-{index:04d}',
                 id=_digest(board, f'source-{index:04d}'),
                 revoked=(board == BOARDS[0] and index == 1199))
            for board, size in zip(BOARDS, SIZES) for index in range(size)]


def _chunks(rows, size=100):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _save_report(name, result, root):
    destination = Path(os.environ.get('OKTO_PULSE_R8_REPORT_DIR', str(root)))
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f'{name}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')


def _identity():
    community = Path(__file__).resolve().parents[1]
    core = Path(worker_module.__file__).resolve().parents[5]
    def revision(root):
        # Pulse's trusted runner exports exact Git blobs without .git metadata.
        # Its signed execution basis owns revision identity for that snapshot.
        if not (root / '.git').exists():
            return None
        return subprocess.check_output(['rtk', 'proxy', 'git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    return {
        'python': platform.python_version(), 'platform': platform.platform(),
        'machine': platform.machine(), 'processor': platform.processor(),
        'cpu_count': os.cpu_count(), 'grafx': importlib.metadata.version('okto-grafx'),
        'core_revision': revision(core), 'community_revision': revision(community),
        'core_module_sha256': hashlib.sha256(Path(worker_module.__file__).read_bytes()).hexdigest(),
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'seed': 'r8-fixed-identities-v1', 'digests': sum(SIZES), 'boards': len(BOARDS),
        'target_identities': SIZES[0],
    }


@pytest.fixture(scope='module')
def native_template(tmp_path_factory):
    root = tmp_path_factory.mktemp('r8-native-template')
    rows = _seed_rows()
    with connect(root / 'global') as db:
        ensure_current_grafx_global_schema(db)
        with db.begin('write') as tx:
            tx.executemany('CREATE (:Board {board_id:$board})', [{'board': b} for b in BOARDS])
        for chunk in _chunks(rows):
            with db.begin('write') as tx:
                tx.executemany("CREATE (:DecisionDigest {id:$id,board_id:$board,original_node_id:$source,"
                               "node_type:'Decision',graph_layer:'canonical',source_revoked:$revoked})", chunk)
                tx.executemany('MATCH (b:Board {board_id:$board}),(d:DecisionDigest {id:$id}) '
                               'CREATE (b)-[:CONTAINS_DECISION]->(d)', [r for r in chunk if not r['revoked']])
            if rows.index(chunk[-1]) % 500 == 499:
                print(json.dumps({'r8_stage': 'global_seed', 'count': rows.index(chunk[-1]) + 1}), flush=True)
        for owner in BOARDS[:2]:
            with db.begin('write') as tx:
                tx.execute('MATCH (b:Board {board_id:$board}),(d:DecisionDigest {id:$id}) '
                           'CREATE (b)-[:CONTAINS_DECISION]->(d)', {'board': owner, 'id': rows[0]['id']})
        db.checkpoint()
        assert db.verify('all').findings == ()
    for board in BOARDS:
        with connect(root / board) as db:
            with db.begin('write') as tx:
                tx.execute(GrafxSpaceManifest(node_type='Decision', name='r8_source').ddl())
                for kind in DIGESTED_NODE_TYPES:
                    tx.execute(f'CREATE NODE TABLE {kind}(id STRING,title STRING,embedding VECTOR(r8_source),graph_layer STRING,'
                               'revocation_reason STRING,superseded_by STRING,PRIMARY KEY(id))')
            vector = VectorValue(values=tuple(VECTOR), space_ref=db.catalog.catalog.space('r8_source').space_id, dtype='float64')
            for chunk in _chunks([r for r in rows if r['board'] == board]):
                with db.begin('write') as tx:
                    tx.executemany("CREATE (:Decision {id:$source,title:$source,embedding:$vector,"
                                   "graph_layer:'canonical',revocation_reason:$reason})",
                                   [dict(r, vector=vector, reason='user_revoked' if r['revoked'] else None) for r in chunk])
            db.checkpoint()
    print(json.dumps({'r8_stage': 'native_fixture_seeded', 'digests': len(rows)}), flush=True)
    return root


class NativeHarness:
    def __init__(self, template, root, monkeypatch):
        self.root, self.monkeypatch = root, monkeypatch
        shutil.copytree(template, root / 'graphs')
        self.databases = {}
        self.phases = []
        self.fsync_calls = 0
        self.fsync_observations = []
        self.fault = None
        self.fault_fired = False
        self.source_reads = defaultdict(list)
        self.verifications = defaultdict(list)
        self.event_effects = []
        self.event_expectations = {}
        self.ack_observations = []
        self.oracle = {board: {r['source']: ('canonical', r['revoked']) for r in _seed_rows() if r['board'] == board}
                       for board in BOARDS}
        self.runtime = CommunityGrafxGlobalDiscoveryRuntime(
            lambda: self.database('global'), lambda: self.root / 'graphs' / 'global',
            lambda: self.close('global'), self.fence,
        )
        self.cypher = CommunityGrafxCypherExecutor(self.database)
        registry = KGProviderRegistry(cypher_executor=self.cypher, global_discovery_runtime=self.runtime,
                                      embedding_provider=SimpleNamespace(encode=lambda _: list(VECTOR)))
        monkeypatch.setattr(worker_module, 'get_kg_registry', lambda: registry)
        monkeypatch.setattr(registry_module, 'get_kg_registry', lambda: registry)
        register_coordination_providers(write_lock_port=CommunityLocalWriteLockPort(kg_base_dir=root / 'leases'))
        self.store = CommunitySqlAlchemyGlobalOutboxStore()
        register_global_outbox_store(self.store)
        real_fsync = os.fsync
        def fsync(fd):
            self.fsync_calls += 1
            self.fsync_observations.append({'phase': self.phases[-1]['phase'] if self.phases else 'setup',
                                            'event_id': EVENT.get(), 'at': time.perf_counter()})
            if self.fault == 'fsync' and not self.fault_fired and self.phases and self.phases[-1]['phase'] in ('flush', 'checkpoint'):
                self.fault_fired = True
                raise OSError('r8_injected_native_fsync_failure')
            return real_fsync(fd)
        monkeypatch.setattr(os, 'fsync', fsync)

    def database(self, scope):
        if scope not in self.databases:
            self.databases[scope] = connect(self.root / 'graphs' / scope)
        return self.databases[scope]

    def close(self, scope):
        db = self.databases.pop(scope, None)
        if db is not None:
            db.close()

    def close_all(self):
        for scope in tuple(self.databases):
            self.close(scope)

    def fence(self, phase):
        self.phases.append({'phase': phase, 'event_id': EVENT.get(), 'at': time.perf_counter()})
        if self.fault == 'owner_loss' and phase == 'post_write_verification' and not self.fault_fired:
            self.fault_fired = True
            token = require_global_write_token().owner_token
            assert KGSingleWriterLock().release(board_id=GLOBAL_DISCOVERY_WRITER_SCOPE, owner_token=token)
        assert_global_discovery_writer_fence()

    def clean_initial_links(self):
        with global_discovery_writer_scope(operation='r8_fixture_normalize'):
            assert self.runtime.normalize_board_digest_link(board_id=BOARDS[0], digest_id=_digest(BOARDS[0], 'source-0000')) == 3

    def source_change(self, board, index):
        source = 'source-0000'
        if index == 10:
            source = 'source-0007'
            with self.database(board).begin('write') as tx:
                tx.execute('MATCH (n:Decision {id:$id}) SET n.revocation_reason=$reason', {'id': source, 'reason': 'user_revoked'})
            self.oracle[board][source] = ('canonical', True)
        else:
            layer = 'working' if self.oracle[board][source][0] == 'canonical' else 'canonical'
            with self.database(board).begin('write') as tx:
                tx.execute('MATCH (n:Decision {id:$id}) SET n.graph_layer=$layer', {'id': source, 'layer': layer})
            self.oracle[board][source] = (layer, False)

    def assert_effects(self, board):
        rows = self.database('global').execute(
            'MATCH (d:DecisionDigest) WHERE d.board_id=$board '
            'RETURN d.id,d.original_node_id,d.node_type,d.graph_layer,d.source_revoked', {'board': board}).rows
        expected = {_digest(board, source): (source, 'Decision', layer, revoked)
                    for source, (layer, revoked) in self.oracle[board].items()}
        actual = {row[0]: tuple(row[1:]) for row in rows}
        assert len(rows) == len(actual) and actual == expected
        edges = self.database('global').execute(
            'MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) WHERE d.board_id=$board '
            'RETURN b.board_id,d.id', {'board': board}).rows
        assert Counter(edges) == Counter((board, identity) for identity, row in expected.items() if not row[-1])
        return hashlib.sha256(json.dumps(sorted(actual.items()), separators=(',', ':')).encode()).hexdigest()

    async def setup_sql(self, count, *, retry_count=0):
        self.engine = create_async_engine(f'sqlite+aiosqlite:///{self.root / "outbox.db"}')
        install_community_sqlite_pragmas(self.engine)
        await initialize_current_schema(self.engine, current_schema_contract())
        self.sessions = build_community_session_factory(self.engine)
        self.envelopes = [DeliveryAttemptEnvelope(board_id=BOARDS[i % 3], artifact_type='spec',
                          artifact_id=f'r8-artifact-{i:03d}', generation=1, delete_event_id=f'r8-delete-{i:03d}', attempt=0)
                          for i in range(count)]
        async with self.sessions() as session:
            session.add_all([Board(id=b, realm_id='local', name=b, owner_id='r8-test') for b in BOARDS])
            await session.flush()
            for i, env in enumerate(self.envelopes):
                session.add(GlobalDiscoveryDeliveryLedger(delivery_key=env.delivery_key, board_id=env.board_id,
                    artifact_type=env.artifact_type, artifact_id=env.artifact_id, generation=env.generation,
                    delete_event_id=env.delete_event_id, state=DeliveryState.OUTBOX_PERSISTED.value,
                    attempt=0, attempt_event_key=env.attempt_event_key))
                session.add(GlobalUpdateOutbox(event_id=env.attempt_event_key, board_id=env.board_id,
                    session_id=env.outbox_session_id, event_type=env.outbox_event_type, payload=dict(env.payload),
                    retry_count=retry_count, created_at=datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(seconds=i)))
            await session.commit()
        self.processor = GlobalOutboxProcessor(self.sessions, claim_repository=CommunitySqlAlchemyClaimRepository(),
                                               delivery_ledger=CommunitySqlAlchemyDeliveryLedger())

        def lose_owner_at(boundary):
            if self.fault == boundary and not self.fault_fired:
                self.fault_fired = True
                token = require_global_write_token().owner_token
                assert KGSingleWriterLock().release(board_id=GLOBAL_DISCOVERY_WRITER_SCOPE, owner_token=token)

        real_persist = self.processor._persist_batch_outcomes
        async def persist(context, events, attempts):
            lose_owner_at('owner_loss_before_ack')
            return await real_persist(context, events, attempts)
        self.monkeypatch.setattr(self.processor, '_persist_batch_outcomes', persist)
        real_save = self.store.save_events
        async def save(context, events):
            lose_owner_at('owner_loss_at_ack')
            self.ack_observations.extend({'event_id': event.event_id, 'delivered_claim': event.processed_at is not None,
                                          'post_flush_verified': bool(self.verifications['post_flush']),
                                          'fault_fired': self.fault_fired} for event in events)
            result = await real_save(context, events)
            lose_owner_at('owner_loss_after_save')
            return result
        self.monkeypatch.setattr(self.store, 'save_events', save)
        real_ledger = self.processor._delivery_ledger.apply_attempt_outcomes
        async def ledger(context, outcomes):
            result = await real_ledger(context, outcomes)
            lose_owner_at('owner_loss_after_ledger')
            if self.fault in ('ledger_failure', 'ledger_cancel'):
                self.fault_fired = True
                if self.fault == 'ledger_cancel':
                    raise asyncio.CancelledError()
                raise RuntimeError('r9_injected_ledger_failure')
            return result
        self.monkeypatch.setattr(self.processor._delivery_ledger, 'apply_attempt_outcomes', ledger)
        real_commit = self.store.commit
        async def commit(context):
            lose_owner_at('owner_loss_at_commit')
            return await real_commit(context)
        self.monkeypatch.setattr(self.store, 'commit', commit)
        real_apply = self.processor._apply_event
        real_inventory = self.processor._read_board_digestable_node_types
        real_verify = self.processor._verify_reconciled_digest_layers
        def inventory(board):
            value = real_inventory(board)
            if EVENT.get():
                self.source_reads[EVENT.get()].append(dict(value) if value is not None else None)
            return value
        def verify(runtime, board, expected):
            event = EVENT.get()
            if event and self.fault in ('duplicate', 'foreign_owner', 'revoked', 'source_change') and not self.fault_fired:
                self.fault_fired = True
                if self.fault == 'source_change':
                    with self.database(board).begin('write') as tx:
                        vector = VectorValue(values=tuple(VECTOR), space_ref=self.database(board).catalog.catalog.space('r8_source').space_id, dtype='float64')
                        tx.execute("CREATE (:Decision {id:'late-source',title:'Late',embedding:$vector,graph_layer:'canonical'})", {'vector': vector})
                elif self.fault == 'revoked':
                    with self.database('global').begin('write') as tx:
                        tx.execute('MATCH (d:DecisionDigest {id:$id}) SET d.source_revoked=true', {'id': _digest(board, 'source-0000')})
                else:
                    owner = board if self.fault == 'duplicate' else BOARDS[1]
                    with self.database('global').begin('write') as tx:
                        tx.execute('MATCH (b:Board {board_id:$owner}),(d:DecisionDigest {id:$id}) '
                                   'CREATE (b)-[:CONTAINS_DECISION]->(d)', {'owner': owner, 'id': _digest(board, 'source-0000')})
            real_verify(runtime, board, expected)
            self.verifications[event or 'post_flush'].append({'board': board, 'identities': sorted(expected)})
        async def apply(event, session):
            token = EVENT.set(event.event_id)
            try:
                index = self.envelopes.index(next(e for e in self.envelopes if e.attempt_event_key == event.event_id))
                if len(self.envelopes) == 50 and index % 5 == 0:
                    await asyncio.to_thread(self.source_change, event.board_id, index)
                expected = {source: (layer, 'Decision') for source, (layer, revoked) in self.oracle[event.board_id].items()
                            if not revoked}
                self.event_expectations[event.event_id] = expected
                result = await real_apply(event, session)
                assert result == expected
                digest = await asyncio.to_thread(self.assert_effects, event.board_id)
                self.event_effects.append({'event_id': event.event_id, 'board': event.board_id, 'effects_sha256': digest,
                                           'expected_sources': sorted(result)})
                if len(self.event_effects) % 5 == 0:
                    print(json.dumps({'r8_stage': 'events_applied', 'count': len(self.event_effects)}), flush=True)
                return result
            finally:
                EVENT.reset(token)
        # The source fence calls the class staticmethod, while initial reads use
        # the instance. Observe both paths without replacing the actual query.
        self.monkeypatch.setattr(GlobalOutboxProcessor, '_read_board_digestable_node_types', staticmethod(inventory))
        self.monkeypatch.setattr(self.processor, '_verify_reconciled_digest_layers', verify)
        self.monkeypatch.setattr(self.processor, '_apply_event', apply)

    async def outcomes(self):
        async with self.sessions() as session:
            rows = (await session.execute(select(GlobalUpdateOutbox).order_by(GlobalUpdateOutbox.created_at))).scalars().all()
            ledger = (await session.execute(select(GlobalDiscoveryDeliveryLedger))).scalars().all()
            by_key = {row.attempt_event_key: row.state for row in ledger}
            return [{'event_id': r.event_id, 'acked': r.processed_at is not None, 'retries': r.retry_count,
                     'error': r.last_error, 'ledger': by_key[r.event_id]} for r in rows]


def test_native_counts_and_fifty_alternating_benchmark_pairs(native_template, tmp_path, monkeypatch):
    h = NativeHarness(native_template, tmp_path, monkeypatch)
    report = {'identity': _identity(), 'completed': False, 'times_ms': {'baseline': [], 'candidate': []}}
    try:
        db = h.database('global')
        rows = [r for r in _seed_rows() if r['board'] == BOARDS[0]]
        identities = tuple(r['id'] for r in rows)
        oracle = {r['id']: 1 for r in rows if not r['revoked']}
        oracle[rows[0]['id']] = 3
        report['oracle'] = sorted(oracle.items())
        report['plans'] = {name: repr(db.explain(query)) for name, query in [('baseline', BASELINE), ('candidate', CANDIDATE)]}
        before = db.transactions.published_lsn()
        def read(name):
            started = time.perf_counter()
            if name == 'baseline':
                actual = dict(h.runtime.execute(BASELINE, {'bid': BOARDS[0]}).rows)
            else:
                actual = GlobalOutboxProcessor._read_digest_inbound_edge_counts(h.runtime, BOARDS[0], identities)
            elapsed = (time.perf_counter() - started) * 1000
            assert actual == oracle
            return elapsed
        report['warmup_ms'] = {name: [read(name) for _ in range(3)] for name in report['times_ms']}
        for pair in range(50):
            for name in (('baseline', 'candidate') if pair % 2 == 0 else ('candidate', 'baseline')):
                report['times_ms'][name].append(read(name))
            if (pair + 1) % 10 == 0:
                print(json.dumps({'r8_stage': 'benchmark_pairs', 'count': pair + 1}), flush=True)
                _save_report('r8-benchmark', report, tmp_path)
        report['published_lsn_before'] = before
        report['published_lsn_after'] = db.transactions.published_lsn()
        assert report['published_lsn_after'] == before
        report['summary'] = {name: {'median_ms': statistics.median(values), 'p95_ms': sorted(values)[47]}
                             for name, values in report['times_ms'].items()}
        report['median_ratio'] = report['summary']['candidate']['median_ms'] / report['summary']['baseline']['median_ms']
        assert report['median_ratio'] <= 0.8
        assert GlobalOutboxProcessor._read_digest_inbound_edge_counts(h.runtime, 'absent', ()) == {}
        # Actual population boundaries on the same disposable database after
        # the read-only benchmark; never send an oversized native collection.
        for chunk in _chunks(rows[1024:]):
            with db.begin('write') as tx:
                tx.executemany('MATCH (d:DecisionDigest {id:$id}) DETACH DELETE d', chunk)
        expected = {key: value for key, value in oracle.items() if key in identities[:1024]}
        assert GlobalOutboxProcessor._read_digest_inbound_edge_counts(h.runtime, BOARDS[0], identities[:1024]) == expected
        with db.begin('write') as tx:
            tx.execute("CREATE (:DecisionDigest {id:$id,board_id:$board,original_node_id:$source,node_type:'Decision',graph_layer:'canonical'})", rows[1024])
            tx.execute('MATCH (b:Board {board_id:$board}),(d:DecisionDigest {id:$id}) CREATE (b)-[:CONTAINS_DECISION]->(d)', rows[1024])
        expected[identities[1024]] = 1
        assert GlobalOutboxProcessor._read_digest_inbound_edge_counts(h.runtime, BOARDS[0], identities[:1025]) == expected
        with pytest.raises(RuntimeError, match='population_changed'):
            GlobalOutboxProcessor._read_digest_inbound_edge_counts(h.runtime, BOARDS[0], identities[:1024])
        report['boundary_populations'] = [0, 1024, 1025, 1200]
        report['completed'] = True
    finally:
        h.close_all()
        _save_report('r8-benchmark', report, tmp_path)


@pytest.mark.asyncio
async def test_fifty_real_events_preserve_each_inventory_effect_and_durable_ack(native_template, tmp_path, monkeypatch):
    h = NativeHarness(native_template, tmp_path, monkeypatch)
    report = {'identity': _identity(), 'completed': False}
    try:
        h.clean_initial_links()
        await h.setup_sql(50)
        started = time.perf_counter()
        assert await h.processor.process_once() == 50
        report['elapsed_ms'] = (time.perf_counter() - started) * 1000
        report['outcomes'] = await h.outcomes()
        expected_ids = [e.attempt_event_key for e in h.envelopes]
        assert [r['event_id'] for r in report['outcomes']] == expected_ids
        assert [r['event_id'] for r in h.event_effects] == expected_ids
        assert all(r['acked'] and r['retries'] == 0 and r['ledger'] == 'delivered' for r in report['outcomes'])
        assert set(h.source_reads) == set(expected_ids)
        assert all(len(h.source_reads[event]) >= 2 and h.verifications[event] for event in expected_ids)
        assert all(all(read == {source: value[1] for source, value in h.event_expectations[event].items()}
                       for read in h.source_reads[event]) for event in expected_ids)
        assert {v['board'] for v in h.verifications['post_flush']} == set(BOARDS)
        assert any(item['phase'] in ('flush', 'checkpoint') for item in h.fsync_observations)
        assert {'flush', 'checkpoint', 'post_write_verification', 'reopen_probe'} <= {p['phase'] for p in h.phases}
        for board in BOARDS:
            h.assert_effects(board)
        assert KGSingleWriterLock().inspect(board_id=GLOBAL_DISCOVERY_WRITER_SCOPE) is None
        report.update(completed=True, event_effects=h.event_effects, source_reads=dict(h.source_reads),
                      verifications=dict(h.verifications), phases=h.phases, fsync_observations=h.fsync_observations)
    finally:
        report.update(event_effects=h.event_effects, source_reads=dict(h.source_reads),
                      verifications=dict(h.verifications), ack_observations=h.ack_observations,
                      phases=h.phases, fsync_observations=h.fsync_observations)
        h.close_all()
        if hasattr(h, 'engine'):
            await h.engine.dispose()
        _save_report('r8-fifty-events', report, tmp_path)


@pytest.mark.parametrize('fault', [
    'duplicate', 'foreign_owner', 'revoked', 'source_change', 'owner_loss',
    'owner_loss_before_ack', 'owner_loss_at_ack', 'owner_loss_after_save',
    'owner_loss_after_ledger', 'owner_loss_at_commit', 'ledger_failure', 'ledger_cancel',
    'fsync', 'terminal_fsync',
])
@pytest.mark.asyncio
async def test_native_faults_never_ack_matching_counts_without_full_proof(native_template, tmp_path, monkeypatch, fault):
    h = NativeHarness(native_template, tmp_path, monkeypatch)
    report = {'fault': fault, 'completed': False}
    try:
        h.clean_initial_links()
        await h.setup_sql(1, retry_count=4 if fault == 'terminal_fsync' else 0)
        h.fault = 'fsync' if fault == 'terminal_fsync' else fault
        try:
            processed = await h.processor.process_once()
        except (Exception, asyncio.CancelledError) as exc:
            assert fault.startswith('owner_loss') or fault in ('ledger_failure', 'ledger_cancel'), repr(exc)
            report['raised'] = type(exc).__name__
            processed = 0
        assert processed == 0 and h.fault_fired
        report['outcomes'] = await h.outcomes()
        row, = report['outcomes']
        assert row['event_id'] == h.envelopes[0].attempt_event_key and not row['acked']
        if fault == 'terminal_fsync':
            assert row['retries'] == -1 and row['ledger'] == 'delivery_debt'
        else:
            assert row['ledger'] == 'outbox_persisted'
            unchanged = fault.startswith('owner_loss') or fault in ('source_change', 'ledger_failure', 'ledger_cancel')
            assert row['retries'] == (0 if unchanged else 1)
        report.update(completed=True, phases=h.phases, fsync_calls=h.fsync_calls)
    finally:
        report.update(event_effects=h.event_effects, source_reads=dict(h.source_reads),
                      verifications=dict(h.verifications), ack_observations=h.ack_observations,
                      phases=h.phases, fsync_observations=h.fsync_observations)
        h.close_all()
        if hasattr(h, 'engine'):
            await h.engine.dispose()
        _save_report(f'r8-fault-{fault}', report, tmp_path)


class R9AcknowledgementAcceptance(unittest.TestCase):
    """Exact native-runner entry for the already specified R9 regression.

    The server supplies immutable Core/Community snapshots. The selected pytest
    cases retain their real providers and fixtures. No benchmark is selected.
    """

    __test__ = False  # Native unittest entry only; avoid duplicate pytest runs.

    def test_real_provider_acknowledgement_boundaries(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='pulse-r9-acceptance-') as temp:
            reports = Path(temp)
            junit = reports / 'results.xml'
            environment = os.environ.copy()
            environment['OKTO_PULSE_R8_REPORT_DIR'] = str(reports)
            result = subprocess.run(
                ['rtk', 'proxy', sys.executable, '-B', '-m', 'pytest',
                 str(Path(__file__)) + '::test_fifty_real_events_preserve_each_inventory_effect_and_durable_ack',
                 str(Path(__file__)) + '::test_native_faults_never_ack_matching_counts_without_full_proof',
                 str(root / 'tests/test_global_outbox_commit_fence.py'),
                 '-q', '--tb=short', '-p', 'no:cacheprovider',
                 '--junitxml=' + str(junit)],
                cwd=root, env=environment, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=900,
            )
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-12000:])
            cases = ET.parse(junit).getroot().findall('.//testcase')
            self.assertEqual(len(cases), 23)
            self.assertTrue(all(not any(c.find(tag) is not None for tag in
                                        ('failure', 'error', 'skipped')) for c in cases))
            observations = [json.loads(p.read_text(encoding='utf-8'))
                            for p in reports.glob('r8-*.json')]
            self.assertEqual(len(observations), 15)
            self.assertTrue(all(item['completed'] for item in observations))
