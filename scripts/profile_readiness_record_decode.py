"""Compare JSON decoding paths for readiness's synthetic Card ledger batch."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
from types import SimpleNamespace
import json

import orjson
from sqlalchemy import String, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from benchmark_readiness_fixture import seed
from okto_pulse.community.adapters.sqlalchemy_database import install_community_sqlite_pragmas
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, CardDeliveryEvidenceRecordRow as CardRecord,
)


def percentile(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, int(proportion * len(ordered) + .999999) - 1)]


def statement(spec_id: str, *, raw_payload: bool):
    cards = select(Card.id).where(Card.board_id == 'benchmark-board',
                                  Card.spec_id == spec_id)
    columns = [type_coerce(column, String).label(column.key)
        if raw_payload and column.key == 'payload' else column
        for column in CardRecord.__table__.columns]
    return select(*columns).where(CardRecord.board_id == 'benchmark-board',
        CardRecord.spec_id == spec_id, CardRecord.spec_edition == 1,
        CardRecord.card_id.in_(cards)).order_by(
            CardRecord.created_at, CardRecord.id).limit(20001)


async def fetch(engine, spec_id: str, *, raw_payload: bool):
    wall_start, cpu_start = perf_counter(), process_time()
    async with engine.connect() as connection:
        await connection.exec_driver_sql('BEGIN')
        try:
            async with AsyncSession(bind=connection, autoflush=False) as session:
                query_started = perf_counter()
                result = await session.execute(statement(spec_id, raw_payload=raw_payload))
                execute_ms = (perf_counter() - query_started) * 1000
                fetch_started = perf_counter()
                values = result.all()
                fetch_ms = (perf_counter() - fetch_started) * 1000
                wrap_started = perf_counter()
                if raw_payload:
                    rows = tuple(SimpleNamespace(**{
                        **row._mapping, 'payload': orjson.loads(row._mapping['payload'])})
                        for row in values)
                else:
                    rows = tuple(SimpleNamespace(**row._mapping) for row in values)
                wrap_ms = (perf_counter() - wrap_started) * 1000
        finally:
            await connection.rollback()
    return rows, {'execute': execute_ms, 'fetch': fetch_ms, 'wrap': wrap_ms,
        'total': (perf_counter() - wall_start) * 1000,
        'cpu': (process_time() - cpu_start) * 1000}


async def compare(path: Path, *, specs: int, reps: int) -> dict:
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}', pool_size=2,
                                 max_overflow=0)
    install_community_sqlite_pragmas(engine)
    samples = {'orm_json': [], 'raw_orjson': []}
    parity_checked = False
    try:
        for index in range(reps + 2):
            spec_id = f'benchmark-spec-{index % specs:03d}'
            order = (False, True) if index % 2 == 0 else (True, False)
            results = {}
            for raw in order:
                rows, phases = await fetch(engine, spec_id, raw_payload=raw)
                if not 1900 <= len(rows) <= 2100:
                    raise AssertionError(f'fixture_record_count:{len(rows)}')
                results[raw] = rows
                if index >= 2:
                    samples['raw_orjson' if raw else 'orm_json'].append(
                        {name: round(value, 3) for name, value in phases.items()})
            if not parity_checked:
                baseline = [(row.id, row.payload) for row in results[False]]
                candidate = [(row.id, row.payload) for row in results[True]]
                if baseline != candidate:
                    raise AssertionError('record_payload_parity_failed')
                parity_checked = True
    finally:
        await engine.dispose()
    return {'parity': 'pass' if parity_checked else 'missing',
        'variants': {name: {'samples': values,
            'median_ms': {key: round(median(row[key] for row in values), 3)
                for key in ('execute', 'fetch', 'wrap', 'total', 'cpu')},
            'p95_ms': {key: round(percentile([row[key] for row in values], .95), 3)
                for key in ('execute', 'fetch', 'wrap', 'total', 'cpu')}}
            for name, values in samples.items()}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--specs', type=int, default=10)
    parser.add_argument('--reps', type=int, default=20)
    args = parser.parse_args()
    if args.specs < 1 or args.reps < 2:
        parser.error('specs >= 1 and reps >= 2 required')
    fixture = argparse.Namespace(specs=args.specs, cards=100, obligations=100,
        scenarios=200, facts=2000, history=50, fixture_profile='mixed',
        overload_first=False, overload_cards=500, overload_obligations=500,
        overload_scenarios=200, overload_facts=5000)
    with TemporaryDirectory(prefix='readiness-decode-probe-') as folder:
        path = Path(folder) / 'pulse.db'
        seed(path, fixture)
        result = asyncio.run(compare(path, specs=args.specs, reps=args.reps))
    result['fixture'] = vars(fixture)
    result['reps'] = args.reps
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'parity': result['parity'], 'variants': {
        name: {key: value[key] for key in ('median_ms', 'p95_ms')}
        for name, value in result['variants'].items()}}))


if __name__ == '__main__':
    main()
