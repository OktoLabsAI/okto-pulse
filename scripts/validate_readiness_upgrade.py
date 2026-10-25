"""Validate the exact v400→v401 upgrade on a consistent copy of a live database.

This command never writes to the source path. It emits only counts and hashes,
not source rows. The scratch copy and retained backup live in a temporary
directory and are removed after validation.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from hashlib import sha256
import json
import pickle
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, inspect_current_schema,
)
from okto_pulse.community.adapters.readiness_schema_upgrade import (
    PREDECESSOR_SHA256, _fingerprint, upgrade_readiness_schema,
)


def source_inventory(db: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    tables = [name for (name,) in db.execute("SELECT name FROM sqlite_schema "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        if not name.startswith('readiness_')]
    result = {}
    for table in tables:
        digest = sha256()
        count = 0
        for row in db.execute(f'SELECT * FROM "{table}"'):
            encoded = pickle.dumps(tuple(row), protocol=4)
            digest.update(len(encoded).to_bytes(8, 'big'))
            digest.update(encoded)
            count += 1
        result[table] = (count, digest.hexdigest())
    return result


def validate(source: Path) -> dict:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError('source_database_required')
    with TemporaryDirectory(prefix='readiness-upgrade-validation-') as scratch:
        copied = Path(scratch) / 'pulse.sqlite3'
        with closing(sqlite3.connect('file:' + source.as_posix() + '?mode=ro', uri=True)) as live:
            with closing(sqlite3.connect(copied)) as destination:
                live.backup(destination)
                destination.commit()
        with closing(sqlite3.connect(copied)) as predecessor:
            predecessor.execute('PRAGMA journal_mode=WAL')
            before_schema = _fingerprint(predecessor)
            if before_schema != PREDECESSOR_SHA256:
                raise ValueError('unexpected_predecessor_fingerprint')
            before = source_inventory(predecessor)
        backup = upgrade_readiness_schema(copied)
        with closing(sqlite3.connect(copied)) as upgraded, closing(sqlite3.connect(backup)) as preserved:
            if inspect_current_schema(upgraded.cursor(), current_schema_contract()) is not False:
                raise ValueError('current_schema_incompatible')
            if upgraded.execute('PRAGMA quick_check(1)').fetchone() != ('ok',):
                raise ValueError('upgraded_integrity_failed')
            if upgraded.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('upgraded_foreign_key_failed')
            if source_inventory(upgraded) != before or source_inventory(preserved) != before:
                raise ValueError('source_rows_changed')
            return {'predecessor_fingerprint': before_schema,
                'source_tables_verified': len(before),
                'source_rows_verified': sum(count for count, _ in before.values()),
                'upgraded_schema_version': upgraded.execute('PRAGMA user_version').fetchone()[0],
                'source_unchanged': True, 'backup_verified': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    print(json.dumps(validate(parser.parse_args().database), sort_keys=True))
