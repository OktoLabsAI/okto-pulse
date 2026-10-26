"""Prove an older Pulse wheel refuses a disposable upgraded SQLite copy.

The predecessor source is opened read-only. A temporary SQLite backup is
upgraded with the current branch; the supplied older wheel runs in a separate
interpreter and must reject v404 before any database mutation.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / "okto-pulse" / "src"),
                str(WORKSPACE / "okto-pulse-core" / "src")]

from okto_pulse.community.adapters.readiness_schema_upgrade import (  # noqa: E402
    upgrade_readiness_schema,
)


_OLD_ADMISSION = """
import json
from pathlib import Path
import sys
wheel_root, database = sys.argv[1:]
sys.path.insert(0, wheel_root)
import okto_pulse.community as old_community
from okto_pulse.community.adapters.current_relational_schema import (
    SCHEMA_VERSION, StorageFormatError, current_schema_contract,
    require_current_database_file,
)
if not str(old_community.__file__).startswith(wheel_root) or SCHEMA_VERSION >= 404:
    raise AssertionError('previous_wheel_not_loaded')
try:
    require_current_database_file(
        'sqlite+aiosqlite:///' + Path(database).as_posix(), current_schema_contract())
except StorageFormatError as error:
    if error.reason != 'version':
        raise AssertionError('unexpected_previous_binary_refusal:' + str(error)) from error
else:
    raise AssertionError('previous_binary_admitted_v404')
print(json.dumps({'old_refused': True, 'old_schema_version': SCHEMA_VERSION,
                  'old_origin': str(old_community.__file__)}))
"""


def _database_file_hashes(path: Path) -> dict[str, str]:
    paths = (path, path.with_name(path.name + "-wal"),
             path.with_name(path.name + "-shm"))
    return {item.name: sha256(item.read_bytes()).hexdigest()
            for item in paths if item.is_file()}


def verify(predecessor: Path, previous_wheel: Path, expected_sha256: str) -> dict:
    predecessor = predecessor.resolve(strict=True)
    previous_wheel = previous_wheel.resolve(strict=True)
    wheel_hash = sha256(previous_wheel.read_bytes()).hexdigest()
    if wheel_hash != expected_sha256.lower():
        raise ValueError("previous_wheel_hash_mismatch")
    with TemporaryDirectory(prefix="readiness-old-binary-") as directory:
        wheel_root = Path(directory) / "previous-wheel"
        with ZipFile(previous_wheel) as archive:
            archive.extractall(wheel_root)
        upgraded = Path(directory) / "pulse.sqlite3"
        with closing(sqlite3.connect(
            "file:" + predecessor.as_posix() + "?mode=ro", uri=True,
        )) as source, closing(sqlite3.connect(upgraded)) as destination:
            source.backup(destination)
            destination.commit()
        backup = upgrade_readiness_schema(upgraded)
        before = _database_file_hashes(upgraded)
        result = subprocess.run(
            [sys.executable, "-c", _OLD_ADMISSION, str(wheel_root), str(upgraded)],
            cwd=directory, capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("previous_binary_refusal_unproven:" +
                               result.stderr[-2000:] + result.stdout[-1000:])
        old = json.loads(result.stdout.strip())
        if _database_file_hashes(upgraded) != before:
            raise RuntimeError("old_binary_modified_upgraded_database")
        return {"old_refused": old["old_refused"],
                "old_schema_version": old["old_schema_version"],
                "old_wheel_sha256": wheel_hash,
                "upgraded_copy_unchanged": True,
                "predecessor_backup_verified": backup.is_file()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("predecessor", type=Path)
    parser.add_argument("previous_wheel", type=Path)
    parser.add_argument("expected_sha256")
    print(json.dumps(verify(**vars(parser.parse_args())), sort_keys=True))
