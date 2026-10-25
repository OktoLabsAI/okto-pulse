"""Read-only live-source probe: project one Spec on a disposable SQLite copy.

The source is opened read-only. All schema, job, and worker writes target the
temporary backup; no source Card, Board, or Spec is changed.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / 'okto-pulse' / 'src'),
                str(WORKSPACE / 'okto-pulse-core' / 'src')]

from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.community.adapters.readiness_schema_upgrade import upgrade_readiness_schema
from okto_pulse.community.adapters.readiness_projection_worker import ReadinessProjectionProcessor
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import (
    CommunityDeliveryReadinessReader, CommunityReadinessClock,
)
from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution


def _p95(samples: list[float]) -> float:
    return sorted(samples)[max(0, int(len(samples) * .95 + .999999) - 1)]


async def probe(database: Path, board_id: str, read_count: int) -> dict:
    with TemporaryDirectory(prefix="readiness-live-probe-") as directory:
        copied = Path(directory) / "pulse.db"
        with closing(sqlite3.connect('file:' + database.resolve(strict=True).as_posix() +
                '?mode=ro', uri=True)) as live, closing(sqlite3.connect(copied)) as destination:
            live.backup(destination)
            destination.commit()
        with closing(sqlite3.connect(copied)) as source:
            source.execute('PRAGMA journal_mode=WAL')
        upgrade_readiness_schema(copied)
        engine = create_async_engine(f"sqlite+aiosqlite:///{copied}")
        try:
            async with engine.begin() as connection:
                row = (await connection.exec_driver_sql(
                    "SELECT s.id,s.edition,coalesce(b.realm_id,'local'),count(c.id) "
                    "FROM specs s JOIN boards b ON b.id=s.board_id "
                    "LEFT JOIN cards c ON c.spec_id=s.id AND c.archived=0 "
                    "WHERE s.board_id=? AND s.archived=0 GROUP BY s.id "
                    "ORDER BY count(c.id) DESC,s.id LIMIT 1", (board_id,))).one_or_none()
                if row is None:
                    raise ValueError('board_has_no_active_spec')
                spec_id, edition, realm_id, card_count = row
                scope_id = f"{spec_id}:{edition}"
                await connection.exec_driver_sql(
                    "INSERT INTO readiness_scope "
                    "(id,realm_id,board_id,spec_id,edition,source_revision,tombstone,updated_at) "
                    "VALUES (?,?,?,?,?,1,0,CURRENT_TIMESTAMP)",
                    (scope_id, realm_id, board_id, spec_id, edition))
                await connection.exec_driver_sql(
                    "INSERT INTO readiness_job "
                    "(scope_id,requested_revision,state,lease_epoch,attempt,due_at,updated_at) "
                    "VALUES (?,1,'pending',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", (scope_id,))
            worker = ReadinessProjectionProcessor(engine, TrackedBlockingExecution())
            worker._last_reconcile = monotonic()
            worker._last_gc = monotonic()
            started = perf_counter()
            published = await worker.process_once()
            projection_seconds = perf_counter() - started
            result = await worker.store.read(board_id=board_id, spec_id=spec_id, edition=edition)
            if published != 1 or result is None or result.freshness != 'current':
                raise RuntimeError(f'projection_failed:{published}:{result.freshness if result else None}')
            reader = CommunityDeliveryReadinessReader(engine, clock=CommunityReadinessClock())
            old, new = [], []
            for _ in range(read_count):
                start = perf_counter()
                await reader.load(board_id=board_id, spec_id=spec_id)
                old.append((perf_counter() - start) * 1000)
                start = perf_counter()
                await worker.store.read(board_id=board_id, spec_id=spec_id, edition=edition)
                new.append((perf_counter() - start) * 1000)
            return {
                'board_id': board_id, 'spec_id': spec_id, 'cards': card_count,
                'source_database_changed': False, 'projection_seconds': round(projection_seconds, 3),
                'read_count_each': read_count,
                'legacy_p50_ms': round(median(old), 3), 'legacy_p95_ms': round(_p95(old), 3),
                'persisted_p50_ms': round(median(new), 3), 'persisted_p95_ms': round(_p95(new), 3),
                'generation_id': result.generation_id,
                'summaries': len(result.summaries), 'freshness': result.freshness,
            }
        finally:
            await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('board_id')
    parser.add_argument('--reads', type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.database, args.board_id, args.reads)), sort_keys=True))
