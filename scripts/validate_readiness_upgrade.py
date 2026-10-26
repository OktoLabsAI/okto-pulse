"""Validate an admitted v400–v403 to v404 upgrade on a live database copy.

This command never writes to the source path. It emits only counts and hashes,
not source rows. The scratch copy and retained backup live in a temporary
directory and are removed after validation.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / 'okto-pulse' / 'src'),
                str(WORKSPACE / 'okto-pulse-core' / 'src')]

from okto_pulse.community.adapters.current_relational_schema import (  # noqa: E402
    current_schema_contract, inspect_current_schema,
)
from okto_pulse.community.adapters.readiness_schema_upgrade import (  # noqa: E402
    PREDECESSOR_SHA256, PREDECESSOR_VERSION, READINESS_V401_SHA256,
    READINESS_V401_VERSION, READINESS_V402_SHA256, READINESS_V402_VERSION,
    READINESS_V403_SHA256, READINESS_V403_VERSION,
    _fingerprint, upgrade_readiness_schema,
)
from okto_pulse.community.adapters.readiness_rollback_assessment import (  # noqa: E402
    source_inventory,
)


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
            before_version = predecessor.execute('PRAGMA user_version').fetchone()[0]
            before_schema = _fingerprint(predecessor)
            expected = {
                PREDECESSOR_VERSION: PREDECESSOR_SHA256,
                READINESS_V401_VERSION: READINESS_V401_SHA256,
                READINESS_V402_VERSION: READINESS_V402_SHA256,
                READINESS_V403_VERSION: READINESS_V403_SHA256,
            }.get(before_version)
            if before_schema != expected:
                raise ValueError('unexpected_predecessor_fingerprint')
            before = source_inventory(predecessor)
        backup = upgrade_readiness_schema(copied)
        with closing(sqlite3.connect(copied)) as upgraded, closing(sqlite3.connect(backup)) as preserved:
            if inspect_current_schema(upgraded.cursor(), current_schema_contract()) is not False:
                raise ValueError('current_schema_incompatible')
            diagnostic_present = upgraded.execute("SELECT count(*) FROM sqlite_schema "
                "WHERE type='table' AND name='readiness_diagnostic'").fetchone()[0] == 1
            if not diagnostic_present:
                raise ValueError('readiness_diagnostic_missing')
            delta_present = upgraded.execute("SELECT count(*) FROM sqlite_schema "
                "WHERE type='table' AND name='readiness_source_delta'").fetchone()[0] == 1
            if not delta_present:
                raise ValueError('readiness_source_delta_missing')
            if upgraded.execute('PRAGMA quick_check(1)').fetchone() != ('ok',):
                raise ValueError('upgraded_integrity_failed')
            if upgraded.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('upgraded_foreign_key_failed')
            if source_inventory(upgraded) != before or source_inventory(preserved) != before:
                raise ValueError('source_rows_changed')
            return {'predecessor_version': before_version,
                'predecessor_fingerprint': before_schema,
                'source_tables_verified': len(before),
                'source_rows_verified': sum(count for count, _ in before.values()),
                'upgraded_schema_version': upgraded.execute('PRAGMA user_version').fetchone()[0],
                'readiness_diagnostic_present': diagnostic_present,
                'readiness_source_delta_present': delta_present,
                'quick_check': 'ok', 'foreign_key_check': 'empty',
                'source_unchanged': True, 'backup_verified': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    print(json.dumps(validate(parser.parse_args().database), sort_keys=True))
