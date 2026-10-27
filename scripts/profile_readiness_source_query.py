"""Profile the readiness Card ledger SELECT on a disposable fixture.

The probe compares the current schema with a candidate index and drops the
candidate before a final baseline pass. It never opens the user's data home.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from statistics import median
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from benchmark_readiness_fixture import seed
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import (
    MAX_FACTS, CommunityDeliveryReadinessReader, CommunityReadinessClock,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, CardDeliveryEvidenceRecordRow as CardRecord,
)


INDEX = "ix_probe_readiness_scope_order"
INDEX_SQL = (f"CREATE INDEX {INDEX} ON card_delivery_evidence_records "
             "(board_id,spec_id,spec_edition,created_at,id)")
PLAN_SQL = ("EXPLAIN QUERY PLAN SELECT * FROM card_delivery_evidence_records "
            "WHERE board_id=? AND spec_id=? AND spec_edition=? AND card_id IN "
            "(SELECT id FROM cards WHERE board_id=? AND spec_id=?) "
            "ORDER BY created_at,id LIMIT ?")


def query_plan(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [str(row[3]) for row in connection.execute(PLAN_SQL,
            ('benchmark-board', 'benchmark-spec-000', 1,
             'benchmark-board', 'benchmark-spec-000', MAX_FACTS + 1))]


async def measure(path: Path, *, reps: int, specs: int) -> dict:
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}', pool_size=2,
                                 max_overflow=0)
    # A candidate index intentionally changes the schema fingerprint, so the
    # production admission hook must not be used for this disposable A/B probe.
    # This SELECT does not use delivery write functions or writer PRAGMAs.
    reader = CommunityDeliveryReadinessReader(engine, clock=CommunityReadinessClock())
    samples = []
    row_counts = []
    try:
        for index in range(reps + 2):
            spec_id = f'benchmark-spec-{index % specs:03d}'
            cards = select(Card.id).where(Card.board_id == 'benchmark-board',
                                          Card.spec_id == spec_id)
            statement = select(*CardRecord.__table__.columns).where(
                CardRecord.board_id == 'benchmark-board',
                CardRecord.spec_id == spec_id,
                CardRecord.spec_edition == 1,
                CardRecord.card_id.in_(cards)).order_by(
                    CardRecord.created_at, CardRecord.id)
            counts = {'facts': 0, 'query_ms': {}}
            async with engine.connect() as connection:
                await connection.exec_driver_sql('BEGIN')
                try:
                    async with AsyncSession(bind=connection, autoflush=False) as session:
                        rows = await reader._projection_record_rows(
                            session, statement, MAX_FACTS, counts)
                finally:
                    await connection.rollback()
            if index >= 2:
                samples.append(round(counts['query_ms'][CardRecord.__tablename__], 3))
                row_counts.append(len(rows))
            if not 1900 <= len(rows) <= 2100:
                raise AssertionError(f'fixture_record_count:{len(rows)}')
    finally:
        await engine.dispose()
    ordered = sorted(samples)
    return {'samples_ms': samples, 'row_counts': row_counts,
            'median_ms': round(median(samples), 3),
            'p95_ms': ordered[max(0, int(.95 * len(ordered) + .999999) - 1)]}


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
    with TemporaryDirectory(prefix='readiness-query-probe-') as folder:
        path = Path(folder) / 'pulse.db'
        seed(path, fixture)
        before_plan = query_plan(path)
        before = asyncio.run(measure(path, reps=args.reps, specs=args.specs))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(INDEX_SQL)
            connection.commit()
        candidate_plan = query_plan(path)
        candidate = asyncio.run(measure(path, reps=args.reps, specs=args.specs))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(f'DROP INDEX {INDEX}')
            connection.commit()
        after_plan = query_plan(path)
        after = asyncio.run(measure(path, reps=args.reps, specs=args.specs))
    result = {'fixture': vars(fixture), 'reps': args.reps,
              'before': {'plan': before_plan, **before},
              'candidate': {'plan': candidate_plan, **candidate},
              'after': {'plan': after_plan, **after}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
