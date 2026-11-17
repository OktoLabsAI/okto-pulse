"""Exact published407 conversion; no live database and no history rewriting."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import pytest

from okto_pulse.community.adapters import amendment_storage_upgrade as upgrade
from okto_pulse.community.adapters.current_relational_schema import (
    APPLICATION_ID, StorageFormatError, current_schema_contract, inspect_current_schema,
)
from okto_pulse.community.adapters.historical_relational_schema import historical_schema_contract, inspect_historical_schema
from okto_pulse.community.adapters.sqlite_verification_currentness import install_verification_currentness_functions
from okto_pulse.community.adapters.storage_upgrade_v043 import StorageUpgradeError, _table_fingerprint
import test_verification_storage_upgrade as historical

format406 = historical.format406
legacy_source = historical.source


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'published407.db'
    with closing(sqlite3.connect(path)) as db:
        install_verification_currentness_functions(db)
        for row in sorted(historical_schema_contract(407).objects, key=lambda r: (r[0] != 'table', r[1])):
            db.execute(row[3])
        db.execute(f'PRAGMA application_id={APPLICATION_ID}')
        db.execute('PRAGMA user_version=407')
        db.execute("INSERT INTO global_discovery_source_revision(scope_id,fence_version,trigger_manifest_version,incarnation_id,revision,mutation_nonce) VALUES('_global','gdsr-fence-v2','gdsr-trigger-manifest-v10',?,5,?)", ('a' * 64, 'b' * 64))
        db.execute("INSERT INTO boards(id,name,owner_id,realm_id) VALUES('b','Preserved','owner','local')")
        db.execute("INSERT INTO specs(id,board_id,title,status,edition,version,created_by,architecture_adoption,context) VALUES('s','b','Preserved','in_progress',1,1,'owner','{}','Preserve exact text: ação ✓')")
        db.execute("INSERT INTO app_settings(key,value) VALUES('amendment-upgrade','unchanged')")
        db.commit()
        inspect_historical_schema(db.cursor(), version=407)
    return path


def test_verified_copy_preserves_all_authoritative_rows_and_invalidates_readiness(source, tmp_path):
    original = source.read_bytes()
    output = tmp_path / 'new408.db'
    result = upgrade.upgrade_amendment_copy(source, output)
    assert source.read_bytes() == original and not result['installation_switched']
    assert result['source_schema_version'] == 407 and result['target_schema_version'] == 408
    with closing(sqlite3.connect(source)) as old, closing(sqlite3.connect(output)) as new:
        inspect_historical_schema(old.cursor(), version=407)
        inspect_current_schema(new.cursor(), current_schema_contract())
        # Only DELETE gains the existing scoped erasure exception. UPDATE
        # history stays immutable, and the frozen source DDL is unchanged.
        for table in ('verification_reservations', 'verification_acceptances'):
            name = 'trg_' + table + '_immutable_delete'
            old_sql = old.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
            new_sql = new.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
            assert 'kg_board_erasure_permits' not in old_sql
            assert 'WHERE board_id=OLD.board_id' in new_sql
            name = 'trg_' + table + '_immutable_update'
            assert old.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone() == new.execute(
                'SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()
        for table in result['preserved_tables']:
            assert _table_fingerprint(old, table) == _table_fingerprint(new, table)
        assert set(old.execute('SELECT * FROM readiness_source_delta_event')) <= set(new.execute('SELECT * FROM readiness_source_delta_event'))
        prior = old.execute("SELECT source_revision FROM readiness_scope WHERE id='s:1'").fetchone()[0]
        assert new.execute("SELECT source_revision FROM readiness_scope WHERE id='s:1'").fetchone()[0] == prior + 1
        assert new.execute("SELECT requested_revision,state FROM readiness_job WHERE scope_id='s:1'").fetchone() == (prior + 1, 'pending')
        assert new.execute("SELECT kind,card_id FROM readiness_source_delta WHERE scope_id='s:1'").fetchone() == ('full', None)


@pytest.mark.parametrize('drift', ['CREATE TABLE foreign_object(id TEXT)', 'PRAGMA user_version=406'])
def test_refuses_unknown_source_without_touching_it(source, tmp_path, drift):
    with closing(sqlite3.connect(source)) as db:
        db.execute(drift)
        db.commit()
    before = source.read_bytes()
    output = tmp_path / 'refused.db'
    with pytest.raises(StorageUpgradeError):
        upgrade.upgrade_amendment_copy(source, output)
    assert source.read_bytes() == before and not output.exists()


def test_failed_verification_never_publishes_partial_copy(source, tmp_path, monkeypatch):
    before = source.read_bytes()
    output = tmp_path / 'refused.db'
    def refuse(*args):
        raise StorageFormatError('controlled_target_verification_failure')
    monkeypatch.setattr(upgrade, 'inspect_current_schema', refuse)
    with pytest.raises(StorageUpgradeError):
        upgrade.upgrade_amendment_copy(source, output)
    assert source.read_bytes() == before and not output.exists()
    assert not tuple(tmp_path.glob('.pulse-amendment-upgrade-*'))


def test_existing406_conversion_keeps_frozen407_destination():
    from okto_pulse.community.adapters import verification_storage_upgrade as historical
    assert historical.SCHEMA_VERSION == 407
    assert historical.current_schema_contract().objects == historical_schema_contract(407).objects
    assert historical.current_schema_contract().objects != current_schema_contract().objects


def test_populated_signed_history_receipts_and_waiver_are_preserved(legacy_source, tmp_path):
    source407, _ = historical.convert(legacy_source, tmp_path)
    receipts = historical.ledger_snapshot(legacy_source.ledger.evidence_root)
    # A historical append-only human waiver fixture: not a forged current
    # completion authority. Its exact stored bytes must survive the conversion.
    payload = json.dumps({'justification': 'Accepted historical risk', 'phase': 'test',
        'disposition': 'risk_accepted', 'bindings': [{'obligation_ref': 'ac:ac-1', 'semantic_sha256': 'a' * 64}]}, sort_keys=True)
    with closing(sqlite3.connect(source407)) as db:
        install_verification_currentness_functions(db)
        db.execute("INSERT INTO delivery_evidence_records(id,board_id,spec_id,edition,kind,actor_id,actor_kind,idempotency_key,payload_sha256,payload,created_at) VALUES('historic-waiver','board','spec',2,'waiver','actor','user','historic-waiver',?,?,CURRENT_TIMESTAMP)", (sha256(payload.encode()).hexdigest(), payload))
        db.execute("INSERT INTO readiness_operation_audit(id,scope_id,board_id,operation,actor_id,actor_kind,target_revision,reason,created_at) VALUES('historic-audit','spec:2','board','requeue','actor','user',1,'Historical user action',CURRENT_TIMESTAMP)")
        db.commit()
    before = source407.read_bytes()
    output = tmp_path / 'populated408.db'
    result = upgrade.upgrade_amendment_copy(source407, output)
    assert source407.read_bytes() == before
    assert historical.ledger_snapshot(legacy_source.ledger.evidence_root) == receipts
    with closing(sqlite3.connect(source407)) as old, closing(sqlite3.connect(output)) as new:
        assert old.execute('SELECT count(*) FROM verification_acceptances').fetchone()[0] == 2
        assert old.execute('SELECT count(*) FROM delivery_evidence_records').fetchone()[0] == 1
        assert old.execute('SELECT count(*) FROM readiness_operation_audit').fetchone()[0] == 1
        for table in result['preserved_tables']:
            assert _table_fingerprint(old, table) == _table_fingerprint(new, table)
        assert new.execute('SELECT count(*) FROM verification_acceptances').fetchone()[0] == 2
