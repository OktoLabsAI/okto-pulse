"""The additive upgrade must preserve an exact predecessor and refuse drift."""

from contextlib import closing
import sqlite3

import pytest

from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, StorageFormatError, current_schema_contract,
    inspect_current_schema,
)
from okto_pulse.community.adapters.readiness_schema_upgrade import (
    PREDECESSOR_SHA256, PREDECESSOR_VERSION, _fingerprint, upgrade_readiness_schema,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION, GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION,
)


def _predecessor(path):
    contract = current_schema_contract()
    objects = [row for row in contract.objects
        if not (row[1].startswith(("readiness_", "trg_readiness_"))
            or row[2].startswith("readiness_"))]
    with closing(sqlite3.connect(path)) as connection:
        for kind in ("table", "index", "trigger"):
            for row in objects:
                if row[0] == kind:
                    connection.execute(row[3])
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={PREDECESSOR_VERSION}")
        connection.execute("INSERT INTO global_discovery_source_revision "
            "(scope_id, fence_version, trigger_manifest_version, incarnation_id, revision, mutation_nonce) "
            "VALUES ('_global', ?, ?, lower(hex(randomblob(32))), 0, lower(hex(randomblob(32))))",
            (GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION, GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION))
        connection.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)",
            ("readiness_fixture", "preserved"))
        connection.commit()
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        assert _fingerprint(connection) == PREDECESSOR_SHA256


def test_upgrade_preserves_source_rows_and_leaves_verified_backup(tmp_path):
    path = tmp_path / "pulse.sqlite3"
    _predecessor(path)
    backup = upgrade_readiness_schema(path)
    assert backup.exists()
    with closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(backup)) as old:
        assert current.execute("SELECT value FROM app_settings WHERE key='readiness_fixture'").fetchone() == ("preserved",)
        assert old.execute("SELECT value FROM app_settings WHERE key='readiness_fixture'").fetchone() == ("preserved",)
        assert old.execute("PRAGMA user_version").fetchone()[0] == PREDECESSOR_VERSION
        assert current.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert inspect_current_schema(current.cursor(), current_schema_contract()) is False
        assert {"readiness_scope", "readiness_job", "readiness_generation", "readiness_summary", "readiness_unit"} <= {
            row[0] for row in current.execute("SELECT name FROM sqlite_schema WHERE type='table'")}


def test_unknown_predecessor_is_refused_without_backup_or_mutation(tmp_path):
    path = tmp_path / "pulse.sqlite3"
    _predecessor(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unexpected_extension (id TEXT PRIMARY KEY)")
        connection.commit()
    with pytest.raises(StorageFormatError, match="predecessor_fingerprint"):
        upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PREDECESSOR_VERSION
        assert connection.execute("SELECT name FROM sqlite_schema WHERE name='readiness_scope'").fetchone() is None
    assert not list(tmp_path.glob("*.bak"))
