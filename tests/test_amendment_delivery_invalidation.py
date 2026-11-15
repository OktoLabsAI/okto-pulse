"""Revision mutations invalidate their exact original consumer atomically."""
import json
from hashlib import sha256
import pytest
from test_readiness_invalidation import _database


def revision(connection, spec='spec-a'):
    return connection.exec_driver_sql('SELECT source_revision FROM readiness_scope WHERE spec_id=?', (spec,)).scalar_one()


def amend(connection):
    connection.exec_driver_sql("INSERT INTO amendment_hotfix_revisions(id,board_id,original_spec_id,origin_bug_id,revision_spec_id,status,lineage_state,created_by) VALUES('amendment','board','spec-a','bug','spec-b','done','complete','owner')")


@pytest.mark.parametrize('mutation', [
    "UPDATE specs SET title='Changed source' WHERE id='spec-b'",
    "UPDATE specs SET edition=2 WHERE id='spec-b'",
    "UPDATE amendment_hotfix_revisions SET validation_metadata='{}' WHERE id='amendment'",
    "UPDATE amendment_hotfix_revisions SET status='cancelled' WHERE id='amendment'",
    "DELETE FROM amendment_hotfix_revisions WHERE id='amendment'",
    "DELETE FROM specs WHERE id='spec-b'",
])
def test_dependent_scope_invalidation_and_rollback(tmp_path, mutation):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            amend(connection)
        with engine.connect() as connection:
            before = revision(connection)
            connection.exec_driver_sql(mutation)
            assert revision(connection) > before
            assert connection.exec_driver_sql("SELECT kind,card_id FROM readiness_source_delta WHERE scope_id='spec-a:1'").one() == ('full', None)
            assert connection.exec_driver_sql("SELECT requested_revision FROM readiness_job WHERE scope_id='spec-a:1'").scalar_one() == revision(connection)
            connection.rollback()
        with engine.connect() as connection:
            assert revision(connection) == before
    finally:
        engine.dispose()


def test_foreign_progress_never_uses_local_card_delta_optimization(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            amend(connection)
            connection.exec_driver_sql("INSERT INTO cards(id,board_id,spec_id,title,status,position,created_by) VALUES('source-card','board','spec-b','Revision task','in_progress',0,'owner')")
            payload = json.dumps({'justification': 'Current work', 'progress': {'contract_version': 'delivery-progress/v2',
                'material_change': 'unknown', 'source_state': {'workspace_state': 'unknown', 'recoverability': 'unknown'},
                'target_ids': [], 'remaining': 'Checkpoint'}})
            connection.exec_driver_sql("INSERT INTO card_delivery_evidence_records(id,board_id,card_id,spec_id,spec_edition,kind,actor_id,actor_kind,idempotency_key,payload_sha256,payload,created_at) VALUES('p','board','source-card','spec-b',1,'progress','agent','agent','p',?,?,CURRENT_TIMESTAMP)", (sha256(payload.encode()).hexdigest(), payload))
            assert connection.exec_driver_sql("SELECT kind,card_id FROM readiness_source_delta WHERE scope_id='spec-a:1'").one() == ('full', None)
            assert connection.exec_driver_sql("SELECT kind,card_id FROM readiness_source_delta WHERE scope_id='spec-b:1'").one() == ('card_progress_insert', 'source-card')
    finally:
        engine.dispose()


def test_unrelated_scope_is_not_invalidated(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            amend(connection)
            connection.exec_driver_sql("INSERT INTO specs(id,board_id,title,status,version,created_by,architecture_adoption) VALUES('unrelated','board','Unrelated','in_progress',1,'owner','{}')")
            before = revision(connection)
            connection.exec_driver_sql("UPDATE specs SET title='Other' WHERE id='unrelated'")
            assert revision(connection) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize('mutation', [
    "UPDATE cards SET title='Changed implementation' WHERE id='source-card'",
    "UPDATE cards SET spec_id='unrelated' WHERE id='source-card'",
    "DELETE FROM cards WHERE id='source-card'",
])
def test_revision_card_mutation_delete_and_reparent_invalidate_original(tmp_path, mutation):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            amend(connection)
            connection.exec_driver_sql("INSERT INTO specs(id,board_id,title,status,version,created_by,architecture_adoption) VALUES('unrelated','board','Other','in_progress',1,'owner','{}')")
            connection.exec_driver_sql("INSERT INTO cards(id,board_id,spec_id,title,status,position,created_by) VALUES('source-card','board','spec-b','Revision task','in_progress',0,'owner')")
            before = revision(connection)
            connection.exec_driver_sql(mutation)
            assert revision(connection) > before
            assert connection.exec_driver_sql("SELECT kind,card_id FROM readiness_source_delta WHERE scope_id='spec-a:1'").one() == ('full', None)
    finally:
        engine.dispose()
