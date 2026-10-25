"""Compare one committed Spec update with and without readiness hooks.

Both databases are disposable, WAL/FULL and seeded from the same fixture.
The reported difference is the isolated transactional invalidation overhead.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from benchmark_readiness_fixture import seed, percentile


def measure(path: Path, *, writes: int, hooks: bool) -> dict:
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        if not hooks:
            names = [name for (name,) in db.execute("SELECT name FROM sqlite_schema "
                "WHERE type='trigger' AND name LIKE 'trg_readiness_%'")]
            for name in names:
                db.execute(f'DROP TRIGGER "{name}"')
        times = []
        for index in range(writes + 10):
            spec_id = f'benchmark-spec-{index % 10:03d}'
            started = perf_counter()
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE specs SET description=? WHERE id=?', (f'write-{index}', spec_id))
            db.execute('COMMIT')
            if index >= 10:
                times.append((perf_counter() - started) * 1000)
        return {'writes': writes, 'p50_ms': round(median(times), 3),
            'p95_ms': round(percentile(times, .95), 3),
            'max_ms': round(max(times), 3)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--writes', type=int, default=1000)
    args = parser.parse_args()
    args.specs = 10
    args.cards = 100
    args.obligations = 100
    args.scenarios = 200
    args.facts = 2000
    args.history = 50
    with TemporaryDirectory(prefix='readiness-write-overhead-') as folder:
        old, new = Path(folder) / 'old.db', Path(folder) / 'new.db'
        seed(old, args)
        seed(new, args)
        baseline = measure(old, writes=args.writes, hooks=False)
        candidate = measure(new, writes=args.writes, hooks=True)
        print(json.dumps({'fixture': {'specs': 10, 'cards_per_spec': 100,
            'obligations_per_spec': 100, 'scenarios_per_spec': 200,
            'facts_per_spec': 2000, 'history_per_spec': 50},
            'baseline': baseline, 'candidate': candidate,
            'p95_increment_ms': round(candidate['p95_ms'] - baseline['p95_ms'], 3)},
            sort_keys=True))
