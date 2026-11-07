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
from okto_grafx import Database, VectorValue, connect
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
COUNT_READ = ContextVar('r8_native_count_read', default=None)
VERIFYING = ContextVar('r8_native_verification', default=False)


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
        self.sql_observations = []
        self.durability_operations = []
        self.count_reads = defaultdict(list)
        self.event_windows = []
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
        for operation in ('flush', 'checkpoint'):
            original = getattr(Database, operation)
            def observed(database, *args, _operation=operation, _original=original, **kwargs):
                if (database is not self.databases.get('global') or not self.phases
                        or self.phases[-1]['phase'] != _operation):
                    return _original(database, *args, **kwargs)
                observation = {'operation': _operation, 'started': time.perf_counter(), 'completed': False}
                self.durability_operations.append(observation)
                try:
                    if self.fault == _operation and not self.fault_fired:
                        self.fault_fired = True
                        raise OSError('r8_injected_native_' + _operation + '_failure')
                    result = _original(database, *args, **kwargs)
                    observation['completed'] = True
                    return result
                finally:
                    observation['finished'] = time.perf_counter()
            monkeypatch.setattr(Database, operation, observed)
        real_execute = self.runtime.execute
        def execute(*args, **kwargs):
            observation = COUNT_READ.get()
            started = time.perf_counter()
            result = real_execute(*args, **kwargs)
            if observation is not None:
                observation['queries'].append({'started': started, 'finished': time.perf_counter(),
                                               'rows': list(result.rows)})
            return result
        monkeypatch.setattr(self.runtime, 'execute', execute)

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
            await self.observe_sql('before_persist')
            return await real_persist(context, events, attempts)
        self.monkeypatch.setattr(self.processor, '_persist_batch_outcomes', persist)
        real_save = self.store.save_events
        async def save(context, events):
            lose_owner_at('owner_loss_at_ack')
            await self.observe_sql('before_save')
            self.ack_observations.extend({'event_id': event.event_id, 'delivered_claim': event.processed_at is not None,
                                          'board': event.board_id, 'at': time.perf_counter(),
                                          'post_flush_verified_boards': sorted({v['board'] for v in self.verifications['post_flush']}),
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
            await self.observe_sql('before_commit')
            result = await real_commit(context)
            await self.observe_sql('after_commit')
            return result
        self.monkeypatch.setattr(self.store, 'commit', commit)
        real_apply = self.processor._apply_event
        real_inventory = self.processor._read_board_digestable_node_types
        real_verify = self.processor._verify_reconciled_digest_layers
        real_counts = self.processor._read_digest_inbound_edge_counts
        def counts(runtime, board, identities):
            observation = {'board': board, 'identities': sorted(identities),
                           'stage': 'verification' if VERIFYING.get() else 'reconciliation',
                           'started': time.perf_counter(), 'queries': []}
            self.count_reads[EVENT.get() or 'post_flush'].append(observation)
            token = COUNT_READ.set(observation)
            try:
                result = real_counts(runtime, board, identities)
                observation['result'] = dict(result)
                return result
            finally:
                observation['finished'] = time.perf_counter()
                COUNT_READ.reset(token)
        self.monkeypatch.setattr(self.processor, '_read_digest_inbound_edge_counts', counts)
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
            token = VERIFYING.set(True)
            try:
                real_verify(runtime, board, expected)
            finally:
                VERIFYING.reset(token)
            self.verifications[event or 'post_flush'].append({'board': board, 'identities': sorted(expected),
                                                             'finished': time.perf_counter()})
        async def apply(event, session):
            token = EVENT.set(event.event_id)
            window = {'event_id': event.event_id, 'started': time.perf_counter()}
            self.event_windows.append(window)
            try:
                index = self.envelopes.index(next(e for e in self.envelopes if e.attempt_event_key == event.event_id))
                source_changed = len(self.envelopes) == 50 and index % 5 == 0
                if source_changed:
                    await asyncio.to_thread(self.source_change, event.board_id, index)
                expected = {source: (layer, 'Decision') for source, (layer, revoked) in self.oracle[event.board_id].items()
                            if not revoked}
                self.event_expectations[event.event_id] = expected
                result = await real_apply(event, session)
                assert result == expected
                digest = await asyncio.to_thread(self.assert_effects, event.board_id)
                self.event_effects.append({'event_id': event.event_id, 'board': event.board_id, 'effects_sha256': digest,
                                           'source_changed': source_changed, 'expected_sources': sorted(result)})
                await self.observe_sql('event_applied')
                if len(self.event_effects) % 5 == 0:
                    print(json.dumps({'r8_stage': 'events_applied', 'count': len(self.event_effects)}), flush=True)
                return result
            finally:
                window['finished'] = time.perf_counter()
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

    async def observe_sql(self, stage):
        # A separate connection observes committed authority, not staged ORM values.
        rows = await self.outcomes()
        self.sql_observations.append({'stage': stage, 'event_id': EVENT.get(),
                                      'at': time.perf_counter(), 'outcomes': rows})

    def assert_ack_order(self):
        expected_ids = [envelope.attempt_event_key for envelope in self.envelopes]
        assert self.sql_observations
        for observation in self.sql_observations:
            assert [row['event_id'] for row in observation['outcomes']] == expected_ids, observation
            if observation['stage'] != 'after_commit':
                assert all(not row['acked'] and row['ledger'] == 'outbox_persisted'
                           for row in observation['outcomes']), observation
        for acknowledgement in self.ack_observations:
            if not acknowledgement['delivered_claim']:
                continue
            required_boards = {effect['board'] for effect in self.event_effects}
            assert set(acknowledgement['post_flush_verified_boards']) == required_boards
            assert acknowledgement['board'] in required_boards
            flushes = [operation for operation in self.durability_operations
                       if operation['operation'] == 'flush' and operation['completed']]
            checkpoints = [operation for operation in self.durability_operations
                           if operation['operation'] == 'checkpoint' and operation['completed']]
            assert flushes and checkpoints
            flush, checkpoint = flushes[-1], checkpoints[-1]
            assert flush['finished'] <= checkpoint['started'] < checkpoint['finished']
            probes = [phase['at'] for phase in self.phases if phase['phase'] == 'reopen_probe'
                      and checkpoint['finished'] <= phase['at'] < acknowledgement['at']]
            assert probes
            for board in required_boards:
                proofs = [proof for proof in self.verifications['post_flush'] if proof['board'] == board]
                assert proofs and probes[-1] < proofs[-1]['finished'] < acknowledgement['at']

    def assert_fresh_event_counts(self):
        expected_ids = [envelope.attempt_event_key for envelope in self.envelopes]
        assert [window['event_id'] for window in self.event_windows] == expected_ids
        assert set(self.count_reads) == {*expected_ids, 'post_flush'}
        for envelope, window in zip(self.envelopes, self.event_windows, strict=True):
            reads = self.count_reads[envelope.attempt_event_key]
            assert {read['stage'] for read in reads} == {'reconciliation', 'verification'}
            identities = {_digest(envelope.board_id, source) for source in self.oracle[envelope.board_id]}
            expected = {_digest(envelope.board_id, source): 1
                        for source in self.event_expectations[envelope.attempt_event_key]}
            for read in reads:
                assert window['started'] <= read['started'] <= read['finished'] <= window['finished']
                assert read['board'] == envelope.board_id and set(read['identities']) == identities
                # Each call must execute a native query. Returning a memoized map
                # (even an equal one for a no-op event) cannot satisfy this check.
                assert read['queries']
                for query in read['queries']:
                    assert read['started'] <= query['started'] <= query['finished'] <= read['finished']
                    rows = query['rows']
                    assert len(rows) == len(identities) and {row[0] for row in rows} == identities
                if read['stage'] == 'verification':
                    assert read['result'] == expected
            assert all(window['started'] <= proof['finished'] <= window['finished']
                       for proof in self.verifications[envelope.attempt_event_key])
        # The native batch contract verifies each affected Board after reopening,
        # using its final state rather than repeating every intermediate event.
        post_flush = self.count_reads['post_flush']
        assert {read['board'] for read in post_flush} == {envelope.board_id for envelope in self.envelopes}
        last_event = max(window['finished'] for window in self.event_windows)
        first_ack = min(ack['at'] for ack in self.ack_observations)
        for read in post_flush:
            identities = {_digest(read['board'], source) for source in self.oracle[read['board']]}
            expected = {_digest(read['board'], source): 1
                        for source, (_layer, revoked) in self.oracle[read['board']].items() if not revoked}
            assert read['stage'] == 'verification' and set(read['identities']) == identities
            assert last_event < read['started'] <= read['finished'] < first_ack
            assert read['queries'] and read['result'] == expected
            for query in read['queries']:
                assert read['started'] <= query['started'] <= query['finished'] <= read['finished']
                assert len(query['rows']) == len(identities) and {row[0] for row in query['rows']} == identities
                assert {identity: count for identity, count in query['rows'] if count} == expected

    def observations(self):
        return dict(event_effects=self.event_effects, source_reads=dict(self.source_reads),
                    verifications=dict(self.verifications), ack_observations=self.ack_observations,
                    sql_observations=self.sql_observations, count_reads=dict(self.count_reads),
                    event_windows=self.event_windows, durability_operations=self.durability_operations,
                    phases=self.phases, fsync_observations=self.fsync_observations)


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
        report['completed'] = True
    finally:
        h.close_all()
        _save_report('r8-benchmark', report, tmp_path)


def test_native_count_populations_match_oracle_without_timing_gate(native_template, tmp_path, monkeypatch):
    h = NativeHarness(native_template, tmp_path, monkeypatch)
    report = {'identity': _identity(), 'completed': False, 'populations': [], 'read_windows': []}
    try:
        db = h.database('global')
        def logical_snapshot():
            # Preserve multiplicity and ownership, including deliberately invalid
            # links, using the same factual fields as the per-event effect oracle.
            return [sorted(db.execute(query).rows, key=repr) for query in (
                'MATCH (b:Board) RETURN b.board_id',
                'MATCH (d:DecisionDigest) RETURN d.id,d.board_id,d.original_node_id,d.node_type,d.graph_layer,d.source_revoked',
                'MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) RETURN b.board_id,d.id',
            )]
        def read_counts(board, identities):
            before = logical_snapshot()
            before_lsn = db.transactions.published_lsn()
            baseline = None
            actual = None
            refused = None
            try:
                baseline_rows = h.runtime.execute(BASELINE, {'bid': board}).rows
                baseline = dict(baseline_rows)
                assert len(baseline) == len(baseline_rows), 'duplicate baseline identity'
                actual = GlobalOutboxProcessor._read_digest_inbound_edge_counts(h.runtime, board, identities)
                # One correctness comparison per population, with no timing gate.
                # The caller also compares against the independently seeded oracle.
                assert actual == baseline
                return actual
            except RuntimeError as exc:
                refused = str(exc)
                raise
            finally:
                after = logical_snapshot()
                after_lsn = db.transactions.published_lsn()
                report['read_windows'].append({'board': board, 'identities': list(identities),
                    'baseline_counts': baseline, 'candidate_counts': actual, 'refusal': refused,
                    'published_lsn_before': before_lsn, 'published_lsn_after': after_lsn,
                    'logical_sha256_before': hashlib.sha256(json.dumps(before).encode()).hexdigest(),
                    'logical_sha256_after': hashlib.sha256(json.dumps(after).encode()).hexdigest()})
                assert after == before and after_lsn == before_lsn
        seeded = _seed_rows()
        assert len(seeded) == 2500 and len({row['board'] for row in seeded}) == 3
        for board, size in zip(BOARDS, SIZES, strict=True):
            rows = [row for row in seeded if row['board'] == board]
            identities = tuple(row['id'] for row in rows)
            oracle = {row['id']: 1 for row in rows if not row['revoked']}
            if board == BOARDS[0]:
                oracle[rows[0]['id']] = 3  # One proper, one duplicate and one foreign owner.
            actual = read_counts(board, identities)
            assert len(identities) == size and actual == oracle
            report['populations'].append({'board': board, 'identities': list(identities), 'counts': actual})
        rows = [row for row in seeded if row['board'] == BOARDS[0]]
        identities = tuple(row['id'] for row in rows)
        oracle = {row['id']: 1 for row in rows if not row['revoked']}
        oracle[rows[0]['id']] = 3
        assert len(identities) >= 1025
        assert read_counts('absent', ()) == {}
        with pytest.raises(RuntimeError, match='duplicate_identity'):
            read_counts(BOARDS[0], (*identities, identities[0]))
        with pytest.raises(RuntimeError, match='population_changed'):
            read_counts(BOARDS[0], (*identities[:-1], _digest(BOARDS[1], 'source-0000')))
        # Actual native populations, independently executable without timing gates.
        for chunk in _chunks(rows[1024:]):
            with db.begin('write') as tx:
                tx.executemany('MATCH (d:DecisionDigest {id:$id}) DETACH DELETE d', chunk)
        expected = {key: value for key, value in oracle.items() if key in identities[:1024]}
        assert read_counts(BOARDS[0], identities[:1024]) == expected
        report['populations'].append({'board': BOARDS[0], 'identities': list(identities[:1024]), 'counts': dict(expected)})
        with db.begin('write') as tx:
            tx.execute("CREATE (:DecisionDigest {id:$id,board_id:$board,original_node_id:$source,node_type:'Decision',graph_layer:'canonical'})", rows[1024])
            tx.execute('MATCH (b:Board {board_id:$board}),(d:DecisionDigest {id:$id}) CREATE (b)-[:CONTAINS_DECISION]->(d)', rows[1024])
        expected[identities[1024]] = 1
        assert read_counts(BOARDS[0], identities[:1025]) == expected
        report['populations'].append({'board': BOARDS[0], 'identities': list(identities[:1025]), 'counts': expected})
        with pytest.raises(RuntimeError, match='population_changed'):
            read_counts(BOARDS[0], identities[:1024])
        report['boundary_populations'] = [0, 1024, 1025, 1200]
        report['completed'] = True
    finally:
        h.close_all()
        _save_report('r8-native-populations', report, tmp_path)


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
        assert Counter(effect['source_changed'] for effect in h.event_effects) == {False: 40, True: 10}
        previous_effects = {}
        for effect in h.event_effects:
            previous = previous_effects.get(effect['board'])
            if previous is not None:
                assert (effect['effects_sha256'] != previous) == effect['source_changed']
            previous_effects[effect['board']] = effect['effects_sha256']
        assert all(r['acked'] and r['retries'] == 0 and r['ledger'] == 'delivered' for r in report['outcomes'])
        assert set(h.source_reads) == set(expected_ids)
        assert all(len(h.source_reads[event]) >= 2 and h.verifications[event] for event in expected_ids)
        assert all(all(read == {source: value[1] for source, value in h.event_expectations[event].items()}
                       for read in h.source_reads[event]) for event in expected_ids)
        h.assert_fresh_event_counts()
        h.assert_ack_order()
        assert [ack['event_id'] for ack in h.ack_observations] == expected_ids
        assert all(ack['delivered_claim'] for ack in h.ack_observations)
        committed = [observation for observation in h.sql_observations if observation['stage'] == 'after_commit']
        assert len(committed) == 1 and all(row['acked'] and row['ledger'] == 'delivered'
                                           for row in committed[0]['outcomes'])
        assert max(ack['at'] for ack in h.ack_observations) < committed[0]['at']
        assert {v['board'] for v in h.verifications['post_flush']} == set(BOARDS)
        assert any(item['phase'] in ('flush', 'checkpoint') for item in h.fsync_observations)
        assert {'flush', 'checkpoint', 'post_write_verification', 'reopen_probe'} <= {p['phase'] for p in h.phases}
        for board in BOARDS:
            h.assert_effects(board)
        assert KGSingleWriterLock().inspect(board_id=GLOBAL_DISCOVERY_WRITER_SCOPE) is None
        report['completed'] = True
    finally:
        report.update(h.observations())
        h.close_all()
        if hasattr(h, 'engine'):
            await h.engine.dispose()
        _save_report('r8-fifty-events', report, tmp_path)


@pytest.mark.parametrize('fault', [
    'duplicate', 'foreign_owner', 'revoked', 'source_change', 'owner_loss',
    'owner_loss_before_ack', 'owner_loss_at_ack', 'owner_loss_after_save',
    'owner_loss_after_ledger', 'owner_loss_at_commit', 'ledger_failure', 'ledger_cancel',
    'flush', 'checkpoint', 'fsync', 'terminal_fsync',
])
@pytest.mark.asyncio
async def test_native_faults_never_ack_matching_counts_without_full_proof(native_template, tmp_path, monkeypatch, fault):
    h = NativeHarness(native_template, tmp_path, monkeypatch)
    report = {'identity': _identity(), 'fault': fault, 'completed': False}
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
        h.assert_ack_order()
        if fault in ('flush', 'checkpoint'):
            refused = [operation for operation in h.durability_operations
                       if operation['operation'] == fault and not operation['completed']]
            assert len(refused) == 1 and not h.verifications['post_flush']
            assert all(not ack['delivered_claim'] for ack in h.ack_observations)
            if fault == 'checkpoint':
                assert any(operation['operation'] == 'flush' and operation['completed']
                           and operation['finished'] <= refused[0]['started']
                           for operation in h.durability_operations)
        if fault == 'terminal_fsync':
            assert row['retries'] == -1 and row['ledger'] == 'delivery_debt'
        else:
            assert row['ledger'] == 'outbox_persisted'
            unchanged = fault.startswith('owner_loss') or fault in ('source_change', 'ledger_failure', 'ledger_cancel')
            assert row['retries'] == (0 if unchanged else 1)
        report.update(completed=True, phases=h.phases, fsync_calls=h.fsync_calls)
    finally:
        report.update(h.observations())
        h.close_all()
        if hasattr(h, 'engine'):
            await h.engine.dispose()
        _save_report(f'r8-fault-{fault}', report, tmp_path)


class R8PopulationAcceptance(unittest.TestCase):
    """Independent native-runner entry for exact populations and read invariance."""

    __test__ = False  # Native unittest entry only; avoid duplicate pytest runs.

    def test_real_population_boundaries_and_invariance(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='pulse-r8-populations-') as temp:
            reports = Path(os.environ.get('OKTO_PULSE_R8_REPORT_DIR', temp))
            reports.mkdir(parents=True, exist_ok=True)
            junit = reports / 'results.xml'
            environment = os.environ.copy()
            environment['OKTO_PULSE_R8_REPORT_DIR'] = str(reports)
            result = subprocess.run(
                ['rtk', 'proxy', sys.executable, '-B', '-m', 'pytest',
                 str(Path(__file__)) + '::test_native_count_populations_match_oracle_without_timing_gate',
                 '-q', '-s', '--tb=long', '-p', 'no:cacheprovider',
                 '--junitxml=' + str(junit)],
                cwd=root, env=environment, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=900,
            )
            (reports / 'pytest-output.log').write_text(result.stdout + result.stderr, encoding='utf-8')
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-12000:])
            cases = ET.parse(junit).getroot().findall('.//testcase')
            self.assertEqual(len(cases), 1)
            self.assertTrue(all(not any(case.find(tag) is not None for tag in
                                       ('failure', 'error', 'skipped')) for case in cases))
            observations = list(reports.glob('r8-*.json'))
            self.assertEqual([path.name for path in observations], ['r8-native-populations.json'])
            self.assertTrue(json.loads(observations[0].read_text(encoding='utf-8'))['completed'])


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
            self.assertEqual(len(cases), 25)
            self.assertTrue(all(not any(c.find(tag) is not None for tag in
                                        ('failure', 'error', 'skipped')) for c in cases))
            observations = [json.loads(p.read_text(encoding='utf-8'))
                            for p in reports.glob('r8-*.json')]
            self.assertEqual(len(observations), 17)
            self.assertTrue(all(item['completed'] for item in observations))


class R8ReproducibilityAcceptance(unittest.TestCase):
    """Combine native correctness with the existing paired Core log contract."""

    __test__ = False

    def test_complete_results_and_bounded_operational_logs(self):
        suite = unittest.TestSuite([
            R8PopulationAcceptance('test_real_population_boundaries_and_invariance'),
            R9AcknowledgementAcceptance('test_real_provider_acknowledgement_boundaries'),
        ])
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, 2)
        self.assertFalse(result.skipped)
        self.assertFalse(result.expectedFailures)
        self.assertFalse(result.unexpectedSuccesses)
        self.assertTrue(result.wasSuccessful(), repr(result.errors + result.failures))

        # The registered companion is an immutable checkout, not the installed
        # package or an arbitrary workspace supplied by the scenario caller.
        core = Path(os.environ['OKTO_PULSE_CORE_REPO']).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='pulse-r8-log-contract-') as temp:
            junit = Path(temp) / 'results.xml'
            observed = subprocess.run(
                ['rtk', 'proxy', sys.executable, '-B', '-m', 'pytest',
                 str(core / 'tests/test_e2e043_outbox_observability.py'),
                 '-q', '--tb=short', '-p', 'no:cacheprovider',
                 '--junitxml=' + str(junit)],
                cwd=core, env=os.environ.copy(), capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=120,
            )
            self.assertEqual(observed.returncode, 0, (observed.stdout + observed.stderr)[-12000:])
            cases = ET.parse(junit).getroot().findall('.//testcase')
            names = {case.attrib['name'].split('[')[0] for case in cases}
            self.assertTrue({
                'test_observations_correlate_phases_only_ack_after_commit',
                'test_failed_or_cancelled_ack_never_logs_delivered',
                'test_graph_failure_records_pending_not_delivery_and_redacts',
            } <= names)
            self.assertTrue(all(not any(case.find(tag) is not None for tag in
                                       ('failure', 'error', 'skipped')) for case in cases))
