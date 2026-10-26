"""The additive upgrade must preserve an exact predecessor and refuse drift."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, StorageFormatError, current_schema_contract,
    inspect_current_schema,
)
from okto_pulse.community.adapters.readiness_schema_upgrade import (
    PREDECESSOR_SHA256, PREDECESSOR_VERSION, READINESS_V401_SHA256,
    READINESS_V401_VERSION, READINESS_V402_SHA256, READINESS_V402_VERSION,
    READINESS_V403_SHA256, READINESS_V403_VERSION,
    READINESS_V404_SHA256, READINESS_V404_VERSION,
    _fingerprint, upgrade_readiness_schema,
)
from okto_pulse.community.adapters.readiness_invalidation_schema import (
    readiness_trigger_statements,
)
from okto_pulse.community.adapters.readiness_rollback_assessment import (
    assess_readiness_rollback, source_inventory,
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


def _v402_objects():
    # Frozen DDL from the admitted v402 manifest. The current trigger body is
    # deliberately not used to fabricate its own predecessor fixture.
    statements = json.loads((Path(__file__).parent / 'fixtures' /
        'readiness_v402_triggers.json').read_text(encoding='utf-8'))
    old = {sql.split()[2]: sql for sql in statements}
    return tuple((kind, name, table, old.get(name, sql))
        for kind, name, table, sql in current_schema_contract().objects
        if name not in {'readiness_diagnostic', 'readiness_source_delta',
                        'readiness_source_delta_event'})


def _v403_objects():
    statements = json.loads((Path(__file__).parent / 'fixtures' /
        'readiness_v403_triggers.json').read_text(encoding='utf-8'))
    old = {sql.split()[2]: sql for sql in statements}
    return tuple((kind, name, table, old.get(name, sql))
        for kind, name, table, sql in current_schema_contract().objects
        if name not in {'readiness_source_delta', 'readiness_source_delta_event'})


def _v404_objects():
    statements = json.loads((Path(__file__).parent / 'fixtures' /
        'readiness_v404_triggers.json').read_text(encoding='utf-8'))
    old = {sql.split()[2]: sql for sql in statements}
    return tuple((kind, name, table, old.get(name, sql))
        for kind, name, table, sql in current_schema_contract().objects
        if name != 'readiness_source_delta_event')


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
        assert {"readiness_scope", "readiness_job", "readiness_generation", "readiness_summary",
            "readiness_diagnostic", "readiness_unit"} <= {
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


def test_insufficient_backup_space_refuses_before_lock_backup_or_ddl(tmp_path, monkeypatch):
    path = tmp_path / "pulse.sqlite3"
    _predecessor(path)
    monkeypatch.setattr(
        "okto_pulse.community.adapters.readiness_schema_upgrade.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    with pytest.raises(StorageFormatError, match="insufficient_backup_space"):
        upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == PREDECESSOR_VERSION
        assert _fingerprint(connection) == PREDECESSOR_SHA256
    assert not list(tmp_path.glob("*.bak"))


def test_active_writer_refuses_upgrade_without_creating_backup(tmp_path):
    path = tmp_path / "pulse.sqlite3"
    _predecessor(path)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                upgrade_readiness_schema(path)
            with closing(sqlite3.connect(path)) as reader:
                assert reader.execute("PRAGMA user_version").fetchone()[0] == PREDECESSOR_VERSION
                assert _fingerprint(reader) == PREDECESSOR_SHA256
            assert not list(tmp_path.glob("*.bak"))
        finally:
            writer.rollback()


def test_ddl_failure_rolls_back_to_the_exact_predecessor(tmp_path, monkeypatch):
    path = tmp_path / "pulse.sqlite3"
    _predecessor(path)
    def fail_after_tables(_connection):
        raise RuntimeError("injected_trigger_failure")
    monkeypatch.setattr('okto_pulse.community.adapters.readiness_schema_upgrade.install_readiness_invalidation_triggers',
        fail_after_tables)
    with pytest.raises(RuntimeError, match="injected_trigger_failure"):
        upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == PREDECESSOR_VERSION
        assert _fingerprint(database) == PREDECESSOR_SHA256
        assert database.execute("SELECT value FROM app_settings WHERE key='readiness_fixture'").fetchone() == ("preserved",)
    assert len(list(tmp_path.glob("*.bak"))) == 1  # Forensic copy is retained.


def _v401_predecessor(path):
    old_audit = ("CREATE TABLE readiness_operation_audit (\n"
        "\tid VARCHAR(64) NOT NULL, \n"
        "\tscope_id VARCHAR(64) NOT NULL, \n"
        "\tboard_id VARCHAR(64) NOT NULL, \n"
        "\toperation VARCHAR(32) NOT NULL, \n"
        "\tactor_id VARCHAR(255) NOT NULL, \n"
        "\tactor_kind VARCHAR(32) NOT NULL, \n"
        "\tprevious_error_code VARCHAR(128), \n"
        "\ttarget_revision BIGINT NOT NULL, \n"
        "\tcreated_at DATETIME NOT NULL, \n"
        "\tPRIMARY KEY (id)\n)")
    with closing(sqlite3.connect(path)) as connection:
        for kind in ("table", "index", "trigger"):
            for row in _v402_objects():
                if row[0] != kind:
                    continue
                connection.execute(old_audit if row[1] == "readiness_operation_audit" else row[3])
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={READINESS_V401_VERSION}")
        connection.execute("INSERT INTO global_discovery_source_revision "
            "(scope_id, fence_version, trigger_manifest_version, incarnation_id, revision, mutation_nonce) "
            "VALUES ('_global', ?, ?, lower(hex(randomblob(32))), 0, lower(hex(randomblob(32))))",
            (GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION, GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION))
        connection.execute("INSERT INTO readiness_operation_audit "
            "(id,scope_id,board_id,operation,actor_id,actor_kind,previous_error_code,"
            "target_revision,created_at) VALUES "
            "('audit-old','spec:1','board','requeue_terminal','owner','human','disk_full',7,"
            "'2026-01-01 00:00:00')")
        connection.commit()
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        assert _fingerprint(connection) == READINESS_V401_SHA256


def test_v401_upgrade_preserves_audit_and_adds_fenced_requeue_fields(tmp_path):
    path = tmp_path / "pulse.sqlite3"
    _v401_predecessor(path)
    with closing(sqlite3.connect(path)) as previous:
        previous.execute("PRAGMA foreign_keys=ON")
        previous.execute("INSERT INTO boards (id,name,owner_id,realm_id) "
            "VALUES ('board','Board','owner','local')")
        previous.execute("INSERT INTO specs "
            "(id,board_id,title,status,version,created_by,architecture_adoption,archived) "
            "VALUES ('archived','board','Archived','in_progress',1,'owner','{}',1)")
        previous.commit()
        assert previous.execute("SELECT tombstone FROM readiness_scope "
            "WHERE spec_id='archived'").fetchone() == (1,)
        assert previous.execute("SELECT state FROM readiness_job "
            "WHERE scope_id='archived:1'").fetchone() == ('pending',)
    backup = upgrade_readiness_schema(path)
    assert backup.exists()
    with closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(backup)) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == READINESS_V401_VERSION
        assert _fingerprint(old) == READINESS_V401_SHA256
        assert current.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert inspect_current_schema(current.cursor(), current_schema_contract()) is False
        assert current.execute("SELECT id,previous_error_code,target_revision,request_key,reason "
            "FROM readiness_operation_audit").fetchone() == (
                "audit-old", "disk_full", 7, None, None)
        assert current.execute("SELECT state,error_code FROM readiness_job "
            "WHERE scope_id='archived:1'").fetchone() == ('terminal', 'scope_tombstoned')


def test_v401_ddl_failure_keeps_exact_predecessor(tmp_path, monkeypatch):
    path = tmp_path / "pulse.sqlite3"
    _v401_predecessor(path)

    def fail_ddl(_table):
        raise RuntimeError("injected_v401_ddl_failure")

    monkeypatch.setattr(
        'okto_pulse.community.adapters.readiness_schema_upgrade.CreateTable', fail_ddl)
    with pytest.raises(RuntimeError, match="injected_v401_ddl_failure"):
        upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as source:
        assert source.execute("PRAGMA user_version").fetchone()[0] == READINESS_V401_VERSION
        assert _fingerprint(source) == READINESS_V401_SHA256
        assert source.execute("SELECT previous_error_code FROM readiness_operation_audit"
            ).fetchone() == ("disk_full",)
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_v402_upgrade_replaces_only_readiness_triggers(tmp_path):
    path = tmp_path / 'pulse.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        for kind in ('table', 'index', 'trigger'):
            for row in _v402_objects():
                if row[0] == kind:
                    connection.execute(row[3])
        connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        connection.execute(f'PRAGMA user_version={READINESS_V402_VERSION}')
        connection.execute("INSERT INTO global_discovery_source_revision "
            "(scope_id,fence_version,trigger_manifest_version,incarnation_id,revision,mutation_nonce) "
            "VALUES ('_global',?,?,lower(hex(randomblob(32))),0,lower(hex(randomblob(32))))",
            (GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION, GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION))
        connection.commit()
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        assert _fingerprint(connection) == READINESS_V402_SHA256
    backup = upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(backup)) as old:
        assert _fingerprint(old) == READINESS_V402_SHA256
        assert inspect_current_schema(current.cursor(), current_schema_contract()) is False


def test_v403_upgrade_adds_empty_delta_hint_and_preserves_source(tmp_path):
    path = tmp_path / 'pulse.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        for kind in ('table', 'index', 'trigger'):
            for row in _v403_objects():
                if row[0] == kind:
                    connection.execute(row[3])
        connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        connection.execute(f'PRAGMA user_version={READINESS_V403_VERSION}')
        connection.execute("INSERT INTO global_discovery_source_revision "
            "(scope_id,fence_version,trigger_manifest_version,incarnation_id,revision,mutation_nonce) "
            "VALUES ('_global',?,?,lower(hex(randomblob(32))),0,lower(hex(randomblob(32))))",
            (GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION, GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION))
        connection.execute("INSERT INTO boards (id,name,owner_id,realm_id) "
            "VALUES ('board','Board','owner','local')")
        connection.execute("INSERT INTO specs "
            "(id,board_id,title,status,version,created_by,architecture_adoption) "
            "VALUES ('spec','board','Source','in_progress',1,'owner','{}')")
        connection.commit()
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        assert _fingerprint(connection) == READINESS_V403_SHA256
    backup = upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(backup)) as old:
        assert _fingerprint(old) == READINESS_V403_SHA256
        assert current.execute("SELECT title FROM specs WHERE id='spec'").fetchone() == ('Source',)
        assert current.execute("SELECT COUNT(*) FROM readiness_source_delta").fetchone() == (0,)
        assert inspect_current_schema(current.cursor(), current_schema_contract()) is False
        current.execute("UPDATE specs SET title='Changed' WHERE id='spec'")
        assert current.execute("SELECT kind,card_id FROM readiness_source_delta "
            "WHERE scope_id='spec:1'").fetchone() == ('full', None)


def test_v404_upgrade_adds_empty_delta_chain_and_preserves_source(tmp_path):
    path = tmp_path / 'pulse.sqlite3'
    with closing(sqlite3.connect(path)) as connection:
        for kind in ('table', 'index', 'trigger'):
            for row in _v404_objects():
                if row[0] == kind:
                    connection.execute(row[3])
        connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        connection.execute(f'PRAGMA user_version={READINESS_V404_VERSION}')
        connection.execute("INSERT INTO global_discovery_source_revision "
            "(scope_id,fence_version,trigger_manifest_version,incarnation_id,revision,mutation_nonce) "
            "VALUES ('_global',?,?,lower(hex(randomblob(32))),0,lower(hex(randomblob(32))))",
            (GLOBAL_DISCOVERY_SOURCE_FENCE_VERSION, GLOBAL_DISCOVERY_SOURCE_TRIGGER_MANIFEST_VERSION))
        connection.execute("INSERT INTO boards (id,name,owner_id,realm_id) "
            "VALUES ('board','Board','owner','local')")
        connection.execute("INSERT INTO specs "
            "(id,board_id,title,status,version,created_by,architecture_adoption) "
            "VALUES ('spec','board','Source','in_progress',1,'owner','{}')")
        connection.commit()
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone() == ('wal',)
        assert _fingerprint(connection) == READINESS_V404_SHA256
    backup = upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(backup)) as old:
        assert _fingerprint(old) == READINESS_V404_SHA256
        assert current.execute("SELECT title FROM specs WHERE id='spec'").fetchone() == ('Source',)
        assert current.execute("SELECT COUNT(*) FROM readiness_source_delta_event").fetchone() == (0,)
        assert inspect_current_schema(current.cursor(), current_schema_contract()) is False
        current.execute("UPDATE specs SET title='Changed' WHERE id='spec'")
        assert current.execute("SELECT kind,card_id FROM readiness_source_delta_event "
            "WHERE scope_id='spec:1' ORDER BY source_revision DESC LIMIT 1").fetchone() == ('full', None)


@pytest.mark.parametrize("trigger_index", [0, -1])
def test_v401_partial_trigger_ddl_failure_restores_exact_predecessor(
    tmp_path, monkeypatch, trigger_index,
):
    path = tmp_path / "pulse.sqlite3"
    _v401_predecessor(path)
    with closing(sqlite3.connect(path)) as source:
        source_before = source_inventory(source)
    statements = readiness_trigger_statements()
    stop_at = trigger_index % len(statements)

    def fail_during_trigger_install(connection):
        for index, statement in enumerate(statements):
            connection.exec_driver_sql(statement)
            if index == stop_at:
                raise RuntimeError("injected_partial_trigger_ddl")

    monkeypatch.setattr(
        "okto_pulse.community.adapters.readiness_schema_upgrade."
        "install_readiness_invalidation_triggers", fail_during_trigger_install,
    )
    with pytest.raises(RuntimeError, match="injected_partial_trigger_ddl"):
        upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as source:
        assert source.execute("PRAGMA user_version").fetchone()[0] == READINESS_V401_VERSION
        assert _fingerprint(source) == READINESS_V401_SHA256
        assert source_inventory(source) == source_before
        assert source.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
        assert source.execute("PRAGMA foreign_key_check").fetchone() is None
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as preserved:
        assert _fingerprint(preserved) == READINESS_V401_SHA256
        assert source_inventory(preserved) == source_before


def test_rollback_assessment_refuses_source_writes_and_revision_advance(tmp_path):
    path = tmp_path / "pulse.sqlite3"
    _v401_predecessor(path)
    with closing(sqlite3.connect(path)) as source:
        source.execute("INSERT INTO boards (id,name,owner_id,realm_id) "
                       "VALUES ('board','Board','owner','local')")
        source.execute("INSERT INTO specs "
                       "(id,board_id,title,status,version,created_by,architecture_adoption) "
                       "VALUES ('spec','board','Spec','in_progress',1,'owner','{}')")
        source.commit()
    backup = upgrade_readiness_schema(path)
    pristine = assess_readiness_rollback(path, backup)
    assert pristine["restore_allowed_without_preservation_plan"] is True
    assert pristine["changed_source_tables"] == []
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts" /
        "check_readiness_rollback.py"), str(path), str(backup)]
    permitted = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert permitted.returncode == 0, permitted.stderr
    assert json.loads(permitted.stdout)["restore_allowed_without_preservation_plan"] is True

    with closing(sqlite3.connect(path)) as source:
        source.execute("INSERT INTO app_settings (key,value) "
                       "VALUES ('post_upgrade_report','durable')")
        source.commit()
    changed = assess_readiness_rollback(path, backup)
    assert changed["restore_allowed_without_preservation_plan"] is False
    assert "app_settings" in changed["changed_source_tables"]
    refused = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert refused.returncode == 2, refused.stderr
    assert json.loads(refused.stdout)["restore_allowed_without_preservation_plan"] is False

    with closing(sqlite3.connect(path)) as source:
        source.execute("DELETE FROM app_settings WHERE key='post_upgrade_report'")
        source.execute("UPDATE readiness_scope SET source_revision=source_revision+1 "
                       "WHERE spec_id='spec'")
        source.commit()
    reverted = assess_readiness_rollback(path, backup)
    assert reverted["restore_allowed_without_preservation_plan"] is False
    assert "global_discovery_source_revision" in reverted["changed_source_tables"]
    assert reverted["advanced_existing_scopes"] == ["spec:1"]


def test_rollback_assessment_refuses_revision_advance_with_identical_source(tmp_path):
    path = tmp_path / "pulse.sqlite3"
    _v401_predecessor(path)
    with closing(sqlite3.connect(path)) as source:
        source.execute("INSERT INTO boards (id,name,owner_id,realm_id) "
                       "VALUES ('board','Board','owner','local')")
        source.execute("INSERT INTO specs "
                       "(id,board_id,title,status,version,created_by,architecture_adoption) "
                       "VALUES ('spec','board','Spec','in_progress',1,'owner','{}')")
        source.commit()
    backup = upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as source:
        source.execute("UPDATE readiness_scope SET source_revision=source_revision+1 "
                       "WHERE spec_id='spec'")
        source.commit()
    assessed = assess_readiness_rollback(path, backup)
    assert assessed["restore_allowed_without_preservation_plan"] is False
    assert assessed["changed_source_tables"] == []
    assert assessed["advanced_existing_scopes"] == ["spec:1"]


def test_rollback_assessment_preserves_new_operation_audit(tmp_path):
    path = tmp_path / "pulse.sqlite3"
    _v401_predecessor(path)
    backup = upgrade_readiness_schema(path)
    with closing(sqlite3.connect(path)) as source:
        source.execute("INSERT INTO readiness_operation_audit "
                       "(id,scope_id,board_id,operation,actor_id,actor_kind,"
                       "target_revision,created_at) VALUES "
                       "('audit-new','spec:1','board','requeue_terminal','owner',"
                       "'human',8,'2026-01-02 00:00:00')")
        source.commit()
    assessed = assess_readiness_rollback(path, backup)
    assert assessed["restore_allowed_without_preservation_plan"] is False
    assert assessed["changed_source_tables"] == []
    assert assessed["advanced_existing_scopes"] == []
    assert assessed["operation_audit_changed"] is True
