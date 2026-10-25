"""Reproducible isolated load fixture for persisted readiness.

Defaults match the Spec population: 10 Specs, 100 Cards and 100 obligations
per Spec, 200 scenarios, 2000 ledger facts and 50 historical versions. Run
with --specs 1 --facts 20 for a quick functional smoke before the full load.
No user database is opened.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter, process_time

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / 'okto-pulse' / 'src'),
                str(WORKSPACE / 'okto-pulse-core' / 'src')]

from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, create_current_schema_objects,
    seed_current_schema_fences,
)
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import (
    CommunityDeliveryReadinessReader, CommunityReadinessClock,
)
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution
from okto_pulse.core.ports.permission_policy import registered_permission_flags


def percentile(values: list[float], percentile_value: float) -> float:
    return sorted(values)[max(0, int(len(values) * percentile_value + .999999) - 1)]


def seed(path: Path, args: argparse.Namespace) -> None:
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
                requirements = [{
                    'id': f'fr-{index:03d}', 'text': f'Benchmark obligation {index}',
                    'linked_task_ids': [f'{spec_id}-task-{index % task_count:03d}'],
                } for index in range(obligation_count)]
                scenarios = [{
                    'id': f'sc-{index:03d}', 'title': f'Benchmark scenario {index}',
                    'given': 'A committed source state', 'when': 'the projection runs',
                    'then': 'the result is traceable', 'status': 'planned',
                } for index in range(scenario_count)]
                connection.exec_driver_sql("INSERT INTO specs "
                    "(id,board_id,title,status,version,created_by,architecture_adoption,"
                    "functional_requirements,test_scenarios,execution_contract) "
                    "VALUES (?,?,'Benchmark','in_progress',1,'owner','{}',?,?,?)",
                    (spec_id, 'benchmark-board', json.dumps(requirements), json.dumps(scenarios),
                     json.dumps({'contract_version': 'spec-execution-contract/v1',
                        'board_id': 'benchmark-board', 'spec_id': spec_id,
                        'adopted_in_edition': 1, 'actor_id': 'owner', 'origin': 'new_spec'})))
                cards = []
                for index in range(card_count):
                    is_test = index >= task_count
                    card_id = f'{spec_id}-' + ('test' if is_test else 'task') + f'-{index % task_count:03d}'
                    assigned = ([f'sc-{scenario:03d}' for scenario in range(scenario_count)
                        if scenario % task_count == index % task_count] if is_test else [])
                    cards.append((card_id, 'benchmark-board', spec_id, card_id,
                        'test' if is_test else 'normal', 'in_progress', index,
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
                    for index in range(fact_count):
                        card_id = cards[index % len(cards)][0]
                        record_id = f'{spec_id}-fact-{index:05d}'
                        facts.append((record_id, 'benchmark-board', card_id, spec_id, 1,
                            'progress', 'benchmark', 'agent', record_id,
                            sha256(record_id.encode()).hexdigest(), json.dumps({'note': record_id}), now))
                    connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records "
                        "(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,"
                        "idempotency_key,payload_sha256,payload,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", facts)
    finally:
        engine.dispose()


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
        return {'fixture': {'specs': args.specs, 'cards_per_spec': args.cards,
                'obligations_per_spec': args.obligations, 'scenarios_per_spec': args.scenarios,
                'facts_per_spec': args.facts, 'history_per_spec': args.history},
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
    parser.add_argument('--reads', type=int, default=20)
    parser.add_argument('--projection-timeout', type=float, default=600)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='readiness-benchmark-') as folder:
        path = Path(folder) / 'pulse.db'
        seed(path, args)
        print(json.dumps(asyncio.run(benchmark(path, args)), sort_keys=True), flush=True)
