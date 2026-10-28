"""Print compact, non-sensitive persisted readiness status from one database."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def inspect(database: Path, spec_id: str) -> dict:
    with closing(sqlite3.connect('file:' + database.resolve(strict=True).as_posix() +
            '?mode=ro', uri=True)) as connection:
        row = connection.execute(
            "SELECT s.board_id,s.edition,s.source_revision,s.active_generation_id,"
            "j.state,j.error_code,g.captured_revision,g.summary_count,g.unit_count "
            "FROM readiness_scope s JOIN readiness_job j ON j.scope_id=s.id "
            "LEFT JOIN readiness_generation g ON g.id=s.active_generation_id "
            "WHERE s.spec_id=? AND s.tombstone=0 ORDER BY s.edition DESC LIMIT 1",
            (spec_id,)).fetchone()
        if row is None:
            raise ValueError('readiness_scope_missing')
        board_id, edition, source_revision, generation_id, state, error, captured, summaries, units = row
        summary = connection.execute("SELECT payload FROM readiness_summary WHERE generation_id=? "
            "AND card_key=''", (generation_id,)).fetchone() if generation_id else None
        payload = json.loads(summary[0]) if summary else {}
        return {'board_id': board_id, 'spec_id': spec_id, 'edition': edition,
            'source_revision': source_revision, 'captured_revision': captured,
            'generation_id': generation_id, 'job_state': state, 'error_code': error,
            'summaries': summaries, 'units': units,
            'implementation': payload.get('implementation'),
            'verification': payload.get('verification')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('spec_id')
    args = parser.parse_args()
    print(json.dumps(inspect(args.database, args.spec_id), sort_keys=True))
