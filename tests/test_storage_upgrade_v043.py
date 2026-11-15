"""A pinned real 0.4.2 schema is converted on a copy, never inferred/upgraded in place."""
from contextlib import closing
import gzip
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, SCHEMA_VERSION, RelationalSchemaContract, StorageFormatError,
    current_schema_contract, require_current_database_file,
)
from okto_pulse.community.adapters.historical_relational_schema import historical_schema_contract, inspect_historical_schema
from okto_pulse.community.adapters import storage_upgrade_v043 as upgrade
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope


@pytest.fixture(scope='session')
def baseline(tmp_path_factory):
    fixture = json.loads(gzip.decompress((Path(__file__).parent/'fixtures/v042_relational_schema.json.gz').read_bytes()))
    assert fixture['schema_sha256'] == RelationalSchemaContract(tuple(tuple(row) for row in fixture['objects'])).sha256 == upgrade.SOURCE_SCHEMA_SHA256
    path = tmp_path_factory.mktemp('real-v042-schema')/'pulse.db'
    with closing(sqlite3.connect(path)) as connection:
        connection.execute('PRAGMA foreign_keys=ON')
        for kind, _name, _table, sql in sorted(fixture['objects'], key=lambda row: (row[0] != 'table', row[1])):
            connection.execute(sql)
        connection.execute('PRAGMA application_id=' + str(APPLICATION_ID))
        connection.execute('PRAGMA user_version=405')
        connection.execute("INSERT INTO global_discovery_source_revision(scope_id,fence_version,trigger_manifest_version,incarnation_id,revision,mutation_nonce) VALUES ('_global','gdsr-fence-v2','gdsr-trigger-manifest-v9',?,17,?)", ('a'*64,'b'*64))
        connection.execute("INSERT INTO boards(id,name,owner_id,realm_id) VALUES ('b','Existing project','owner','local')")
        adoption = ArchitectureAdoptionScope(board_id='b', spec_id='s', adopted_in_edition=1,
            actor_id='owner', inherited_resource_ids=()).model_dump(mode='json')
        connection.execute("INSERT INTO specs(id,board_id,title,status,version,created_by,context,architecture_adoption) VALUES ('s','b','Delivered Spec','done',7,'owner',?,?)", ('Original context: ação ✓\nKeep every receipt.',json.dumps(adoption)))
        connection.execute("INSERT INTO cards(id,board_id,spec_id,title,status,position,created_by,card_type,details) VALUES ('c','b','s','Historical task','done',0,'owner','normal',?)", ('Existing history\u0000and exact text',))
        connection.commit()
        upgrade._require_source(connection)
    return path


@pytest.fixture
def source(baseline, tmp_path):
    path = tmp_path/'source.db'
    shutil.copyfile(baseline, path)
    return path


def test_verified_copy_preserves_all_business_rows_and_never_opens_old_database_at_startup(source, tmp_path):
    output = tmp_path/'new.db'
    before = source.read_bytes()
    contract = current_schema_contract()
    with pytest.raises(StorageFormatError):
        require_current_database_file('sqlite:///' + source.as_posix(), contract)
    result = upgrade.upgrade_copy_v043(source, output)
    assert result['source_database_unchanged'] and not result['installation_switched']
    assert source.read_bytes() == before
    assert result['source_schema_version'] == 405 and result['target_schema_version'] == 406 and SCHEMA_VERSION == 407
    assert result['freshness_fence']['revision'] == result['freshness_fence']['previous_revision'] + 1
    assert result['freshness_fence']['manifest'] == 'gdsr-trigger-manifest-v10'
    with closing(sqlite3.connect(output)) as connection:
        assert not inspect_historical_schema(connection.cursor(), historical_schema_contract())
        assert connection.execute("SELECT status,version,context FROM specs WHERE id='s'").fetchone() == ('done',7,'Original context: ação ✓\nKeep every receipt.')
        assert connection.execute("SELECT status,details FROM cards WHERE id='c'").fetchone() == ('done','Existing history\u0000and exact text')
        assert all(connection.execute('SELECT count(*) FROM '+upgrade._quoted(table)).fetchone()[0] == 0 for table in upgrade.NEW_TABLES)
    # The historical step remains 405->406; a separate explicit copy admits 407.
    with pytest.raises(StorageFormatError, match='version'):
        require_current_database_file('sqlite:///' + output.as_posix(), contract)
    assert result['preserved_tables']['specs']['rows'] == 1
    assert not list(tmp_path.glob('.pulse-v043-upgrade-*'))


@pytest.mark.parametrize('mutation,reason', [
    ('CREATE INDEX unexpected_custom_index ON cards(title)', 'source_schema_fingerprint'),
    ('PRAGMA user_version=404', 'source_version'),
    ("UPDATE global_discovery_source_revision SET trigger_manifest_version='unknown'", 'source_revision_fence'),
])
def test_unknown_or_drifted_source_is_refused_without_source_changes_or_output(source, tmp_path, mutation, reason):
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(mutation)
        connection.commit()
    before = source.read_bytes()
    with pytest.raises(upgrade.StorageUpgradeError, match=reason):
        upgrade.upgrade_copy_v043(source, tmp_path/'new.db')
    assert source.read_bytes() == before
    assert not (tmp_path/'new.db').exists() and not list(tmp_path.glob('.pulse-v043-upgrade-*'))


def test_existing_output_is_never_replaced_and_source_cannot_be_output(source, tmp_path):
    existing = tmp_path/'important.db'
    existing.write_bytes(b'Unrelated user file')
    for output in [existing, source]:
        before = output.read_bytes()
        with pytest.raises(upgrade.StorageUpgradeError, match='unused_output_file_required'):
            upgrade.upgrade_copy_v043(source, output)
        assert output.read_bytes() == before


def test_conversion_failure_rolls_back_and_never_publishes_even_a_partial_output(source, tmp_path, monkeypatch):
    before = source.read_bytes()
    def reject(*_args):
        raise upgrade.StorageUpgradeError('injected_final_verification_failure')
    monkeypatch.setattr(upgrade, 'inspect_current_schema', reject)
    with pytest.raises(upgrade.StorageUpgradeError, match='injected_final'):
        upgrade.upgrade_copy_v043(source, tmp_path/'new.db')
    assert source.read_bytes() == before
    assert not (tmp_path/'new.db').exists() and not list(tmp_path.glob('.pulse-v043-upgrade-*'))


def test_exclusive_publication_refuses_a_racing_output_creator(source, tmp_path, monkeypatch):
    output = tmp_path/'new.db'
    original = upgrade.os.link
    def race(source_path, destination):
        output.write_bytes(b'Created by someone else')
        return original(source_path, destination)
    monkeypatch.setattr(upgrade.os, 'link', race)
    with pytest.raises(FileExistsError):
        upgrade.upgrade_copy_v043(source, output)
    assert output.read_bytes() == b'Created by someone else'
    assert not list(tmp_path.glob('.pulse-v043-upgrade-*'))


def test_cli_machine_receipt_has_no_banner_and_keeps_installation_unswitched(source, tmp_path, capsys, monkeypatch):
    import sys
    from okto_pulse.community.cli import main
    monkeypatch.setattr(sys, 'argv', ['okto-pulse', 'storage', 'upgrade-copy', '--source-db', str(source),
        '--output-db', str(tmp_path/'new.db'), '--json'])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result['contract_version'] == 'storage-upgrade-copy/v1' and not result['installation_switched']


def test_pending_wal_is_read_on_a_private_copy_without_source_sidecar_changes(source, tmp_path):
    with closing(sqlite3.connect(source)) as active:
        active.execute('PRAGMA journal_mode=WAL')
        active.execute('PRAGMA wal_autocheckpoint=0')
        active.execute("UPDATE specs SET context='Committed WAL history' WHERE id='s'")
        active.commit()
        assert source.with_name(source.name+'-wal').stat().st_size > 0
        before = {p.name:p.read_bytes() for p in tmp_path.glob('source.db*')}
        result = upgrade.upgrade_copy_v043(source, tmp_path/'new.db')
        assert {p.name:p.read_bytes() for p in tmp_path.glob('source.db*')} == before
        with closing(sqlite3.connect(tmp_path/'new.db')) as converted:
            assert converted.execute("SELECT context FROM specs WHERE id='s'").fetchone() == ('Committed WAL history',)
        assert result['preserved_tables']['specs']['rows'] == 1


def test_new_task_sources_advance_the_native_global_fence_and_are_immutable(source, tmp_path):
    output=tmp_path/'new.db'
    upgrade.upgrade_copy_v043(source, output)
    with closing(sqlite3.connect(output)) as connection:
        before=connection.execute('SELECT revision FROM global_discovery_source_revision').fetchone()[0]
        connection.execute("INSERT INTO task_requirement_decision_operations(id,board_id,spec_id,spec_edition,actor_id,idempotency_key,request_sha256,response,recorded_at) VALUES ('op','b','s',1,'owner','key',?,'{}',CURRENT_TIMESTAMP)", ('a'*64,))
        middle=connection.execute('SELECT revision FROM global_discovery_source_revision').fetchone()[0]
        connection.execute("INSERT INTO task_requirement_decisions(id,operation_id,board_id,spec_id,spec_edition,card_id,revision,decision,scope_sha256,payload) VALUES ('decision','op','b','s',1,'c',1,'waived',?,'{}')", ('b'*64,))
        assert connection.execute('SELECT revision FROM global_discovery_source_revision').fetchone()[0] == middle+1 == before+2
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            connection.execute("UPDATE task_requirement_decisions SET decision='required'")
        connection.rollback()


def test_source_change_between_snapshot_checks_prevents_publication(source, tmp_path, monkeypatch):
    original = upgrade._source_state
    calls = 0
    def observed(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            # Emulate another process, not the converter, committing new work.
            with closing(sqlite3.connect(source)) as writer:
                writer.execute("UPDATE specs SET context='Concurrent work' WHERE id='s'")
                writer.commit()
        return original(path)
    monkeypatch.setattr(upgrade, '_source_state', observed)
    with pytest.raises(upgrade.StorageUpgradeError, match='source_changed_during_copy'):
        upgrade.upgrade_copy_v043(source, tmp_path/'new.db')
    assert not (tmp_path/'new.db').exists() and not list(tmp_path.glob('.pulse-v043-upgrade-*'))
