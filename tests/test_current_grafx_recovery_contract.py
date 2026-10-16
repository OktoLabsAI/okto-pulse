"""Recovery refuses foreign formats before conversion or destination creation."""
import pytest
import okto_grafx
from okto_grafx import Timestamp
from okto_pulse.core.kg.logical_transfer import LogicalSchemaError, schema_digest
from okto_pulse.community.adapters.grafx_schema_bootstrap import ensure_current_grafx_board_schema
from okto_pulse.community.adapters.grafx_recovery_contracts import (
    grafx_recovery_contract, make_grafx_recovery_logical_sink,
)
from okto_pulse.community.adapters.logical_transfer_factories import logical_transfer_scope


def files(path):
    # Reader leases are refreshed by read-only queries; persisted data must not change.
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*')
            if p.is_file() and p.relative_to(path).parts[:2] != ('control', 'readers')}


def test_current_catalog_is_accepted_and_foreign_version_is_inert(tmp_path):
    path=tmp_path/'board'
    db=okto_grafx.connect(path)
    try:
        ensure_current_grafx_board_schema(db, board_id='board', bootstrapped_at=Timestamp(micros=1))
        assert grafx_recovery_contract(db,scope='board') == logical_transfer_scope('board')
        tx=db.begin('write')
        tx.execute("MATCH (m:BoardMeta) SET m.schema_version = '0.5.0'")
        tx.commit()
        before=files(path)
        with pytest.raises(LogicalSchemaError,match='incompatible recovery schema version'):
            grafx_recovery_contract(db,scope='board')
        assert files(path)==before
    finally:
        db.close()


@pytest.mark.parametrize('scope',['board','global_discovery'])
def test_foreign_artifact_digest_does_not_create_a_destination(tmp_path,scope):
    destination=tmp_path/'destination'
    assert schema_digest(logical_transfer_scope(scope).schema) != '0'*64
    with pytest.raises(LogicalSchemaError,match='incompatible recovery artifact contract'):
        make_grafx_recovery_logical_sink(destination,scope=scope,expected_schema_digest='0'*64)
    assert not destination.exists()
