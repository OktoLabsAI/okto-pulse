"""Source and projection intent commit or roll back together."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import create_engine
from sqlalchemy import text

import test_delivery_evidence_integration as delivery

ledger = delivery.ledger

from okto_pulse.community.adapters.current_relational_schema import (
    create_current_schema_objects, seed_current_schema_fences,
)


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pulse.sqlite3'}")
    with engine.begin() as connection:
        create_current_schema_objects(connection)
        seed_current_schema_fences(connection)
        connection.exec_driver_sql("INSERT INTO boards (id,name,owner_id,realm_id) "
            "VALUES ('board','Test','owner','local')")
        for spec_id in ("spec-a", "spec-b"):
            connection.exec_driver_sql("INSERT INTO specs "
                "(id,board_id,title,status,version,created_by,architecture_adoption) "
                "VALUES (?,?,?,'in_progress',1,'owner','{}')",
                (spec_id, "board", spec_id))
    return engine


def test_spec_and_card_mutations_enqueue_in_the_same_transaction(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.connect() as connection:
            first = connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one()
            assert first == 1
            assert connection.exec_driver_sql("SELECT requested_revision FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == first
            connection.exec_driver_sql("INSERT INTO cards "
                "(id,board_id,spec_id,title,status,position,created_by) "
                "VALUES ('card','board','spec-a','Card','not_started',0,'owner')")
            changed = connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one()
            assert changed == first + 1
            assert connection.exec_driver_sql("SELECT requested_revision FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == changed
            connection.rollback()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one() == first
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM cards WHERE id='card'").scalar_one() == 0
    finally:
        engine.dispose()


def test_reparenting_invalidates_both_scopes(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO cards "
                "(id,board_id,spec_id,title,status,position,created_by) "
                "VALUES ('card','board','spec-a','Card','not_started',0,'owner')")
        with engine.begin() as connection:
            before = dict(connection.exec_driver_sql("SELECT spec_id,source_revision FROM readiness_scope").all())
            connection.exec_driver_sql("UPDATE cards SET spec_id='spec-b' WHERE id='card'")
            after = dict(connection.exec_driver_sql("SELECT spec_id,source_revision FROM readiness_scope").all())
            assert after['spec-a'] == before['spec-a'] + 1
            assert after['spec-b'] == before['spec-b'] + 1
    finally:
        engine.dispose()


def test_intent_write_failure_rolls_back_source_update(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO cards "
                "(id,board_id,spec_id,title,status,position,created_by) "
                "VALUES ('card','board','spec-a','Original','not_started',0,'owner')")
        with engine.begin() as connection:
            revision = connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one()
            other_revision = connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-b'").scalar_one()
            connection.exec_driver_sql("CREATE TRIGGER fail_readiness_intent BEFORE UPDATE ON readiness_scope "
                "WHEN OLD.spec_id='spec-a' "
                "BEGIN SELECT RAISE(ABORT, 'injected_intent_failure'); END")
        with pytest.raises(IntegrityError, match='injected_intent_failure'):
            with engine.begin() as connection:
                connection.exec_driver_sql("UPDATE specs SET title='Uncommitted spec' WHERE id='spec-b'")
                connection.exec_driver_sql("UPDATE cards SET title='Uncommitted' WHERE id='card'")
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT title FROM cards WHERE id='card'").scalar_one() == 'Original'
            assert connection.exec_driver_sql("SELECT title FROM specs WHERE id='spec-b'").scalar_one() == 'spec-b'
            assert connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one() == revision
            assert connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-b'").scalar_one() == other_revision
            assert connection.exec_driver_sql("SELECT requested_revision FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == revision
    finally:
        engine.dispose()


def test_failed_scenario_result_invalidates_before_evidence_binding(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            before = connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one()
            connection.exec_driver_sql("UPDATE specs SET test_scenarios=? WHERE id='spec-a'",
                ('[{"id":"sc-a","status":"failed"}]',))
            after = connection.exec_driver_sql("SELECT source_revision FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one()
            assert after == before + 1
            assert connection.exec_driver_sql("SELECT requested_revision FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == after
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM card_delivery_evidence_records").scalar_one() == 0
    finally:
        engine.dispose()


def test_board_policy_fans_out_and_spec_history_invalidates_only_its_spec(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            before = dict(connection.exec_driver_sql(
                "SELECT spec_id,source_revision FROM readiness_scope").all())
            connection.exec_driver_sql("UPDATE boards SET settings=? WHERE id='board'",
                ('{"delivery_gate_mode":"strict"}',))
            after_policy = dict(connection.exec_driver_sql(
                "SELECT spec_id,source_revision FROM readiness_scope").all())
            assert all(after_policy[spec_id] == revision + 1
                for spec_id, revision in before.items())
            connection.exec_driver_sql("INSERT INTO spec_history "
                "(id,spec_id,action,actor_type,actor_id,actor_name,version) "
                "VALUES ('history','spec-a','updated','agent','agent-1','Agent',2)")
            after_history = dict(connection.exec_driver_sql(
                "SELECT spec_id,source_revision FROM readiness_scope").all())
            assert after_history['spec-a'] == after_policy['spec-a'] + 1
            assert after_history['spec-b'] == after_policy['spec-b']
            assert dict(connection.exec_driver_sql("SELECT s.spec_id,j.requested_revision "
                "FROM readiness_scope s JOIN readiness_job j ON j.scope_id=s.id").all()) == after_history
    finally:
        engine.dispose()


def test_new_edition_tombstones_old_generation_scope(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE specs SET edition=2,version=2 WHERE id='spec-a'")
            states = dict(connection.exec_driver_sql("SELECT edition,tombstone FROM readiness_scope "
                "WHERE spec_id='spec-a'").all())
            assert states == {1: 1, 2: 0}
            assert connection.exec_driver_sql("SELECT state FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == "terminal"
            assert connection.exec_driver_sql("SELECT requested_revision FROM readiness_job "
                "WHERE scope_id='spec-a:2'").scalar_one() == 1
    finally:
        engine.dispose()


def test_archive_terminates_debt_and_restore_requeues_current_scope(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE specs SET archived=1 WHERE id='spec-a'")
            assert connection.exec_driver_sql("SELECT tombstone FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT state FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == 'terminal'
            connection.exec_driver_sql("UPDATE specs SET archived=0 WHERE id='spec-a'")
            assert connection.exec_driver_sql("SELECT tombstone FROM readiness_scope "
                "WHERE spec_id='spec-a'").scalar_one() == 0
            assert connection.exec_driver_sql("SELECT state FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == 'pending'
    finally:
        engine.dispose()


def test_scope_job_constraints_and_paged_unit_index_survive_reopen(tmp_path):
    engine = _database(tmp_path)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            scope = connection.exec_driver_sql(
                "SELECT id,realm_id,board_id,spec_id,edition FROM readiness_scope "
                "WHERE spec_id='spec-a'").one()
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("INSERT INTO readiness_scope "
                    "(id,realm_id,board_id,spec_id,edition,source_revision,tombstone,updated_at) "
                    "VALUES (?,?,?,?,?,0,0,CURRENT_TIMESTAMP)",
                    ('duplicate-scope', *scope[1:]))
            connection.rollback()
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("INSERT INTO readiness_job "
                    "(scope_id,requested_revision,state,lease_epoch,attempt,due_at,updated_at) "
                    "VALUES (?,1,'pending',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", (scope[0],))
            connection.rollback()
            plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN SELECT payload "
                "FROM readiness_unit WHERE generation_id=? AND card_key=? "
                "ORDER BY kind,ref,semantic_sha256 LIMIT 20", ('g', '')).all()
            assert any('INDEX' in row[3].upper() for row in plan)
    finally:
        engine.dispose()
    reopened = create_engine(f"sqlite:///{tmp_path / 'pulse.sqlite3'}")
    try:
        with reopened.connect() as connection:
            assert connection.exec_driver_sql("SELECT requested_revision FROM readiness_job "
                "WHERE scope_id='spec-a:1'").scalar_one() == 1
    finally:
        reopened.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
async def test_authenticated_report_and_exact_replay_share_source_transaction(ledger):
    session, store, _ = ledger

    async def state():
        return (await session.execute(text(
            "SELECT s.source_revision,j.requested_revision,j.state "
            "FROM readiness_scope s JOIN readiness_job j ON j.scope_id=s.id "
            "WHERE s.spec_id=:spec_id"), {'spec_id': delivery.SPEC_ID})).one()

    before = await state()
    implementation = await delivery.record(store, delivery.command())
    await session.commit()
    committed = await state()
    assert committed.source_revision == before.source_revision + 1
    assert committed.requested_revision == committed.source_revision
    assert committed.state == 'pending'

    replay = await delivery.record(store, delivery.command())
    await session.commit()
    assert replay == {'id': implementation['id'], 'replayed': True}
    assert await state() == committed

    await delivery.record(store, delivery.command('test',
        implementation_ids=[implementation['id']]))
    await session.commit()
    verified = await state()
    assert verified.source_revision == committed.source_revision + 1
    assert verified.requested_revision == verified.source_revision
    assert verified.state == 'pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
async def test_target_execution_receipt_and_revocation_invalidate_consumer(ledger):
    session, _, _ = ledger

    async def revision():
        return (await session.execute(text(
            "SELECT source_revision FROM readiness_scope WHERE spec_id=:spec_id"),
            {'spec_id': delivery.SPEC_ID})).scalar_one()

    receipt_id = (await session.execute(text(
        "SELECT result_investigation_receipt_id FROM implementation_target_execution_records "
        "WHERE id='execution'"))).scalar_one()
    before = await revision()
    await session.execute(text("INSERT INTO implementation_target_resolutions "
        "(id,board_id,target_id,investigation_receipt_id,source_ref,receipt_generation,"
        "subject_version,target_revision,declared_revision,workspace_state_id,declared_dirty,"
        "state,selector_fingerprint,candidate_count,declared_tool_id,declared_tool_version,"
        "submitted_by,agent_observed_at,received_at,payload_sha256,idempotency_key) "
        "SELECT 'resolution',t.board_id,t.id,r.id,t.source_ref,r.generation,"
        "r.subject_version,t.revision,r.declared_revision,r.workspace_state_id,r.declared_dirty,"
        "'resolved',:selector,0,'fixture','1','agent-1',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,"
        ":digest,'resolution' FROM implementation_targets t "
        "JOIN code_investigation_receipts r ON r.id=:receipt_id WHERE t.id='target'"),
        {'receipt_id': receipt_id, 'selector': 'f' * 64, 'digest': 'a' * 64})
    assert await revision() == before + 1
    await session.execute(text("UPDATE implementation_targets "
        "SET intent='Changed intent',revision=revision+1,"
        "last_change_reason_sha256=:reason,updated_at=CURRENT_TIMESTAMP WHERE id='target'"),
        {'reason': 'd' * 64})
    assert await revision() == before + 2
    await session.execute(text("INSERT INTO implementation_target_execution_records "
        "(id,board_id,card_id,target_id,target_revision,result_investigation_receipt_id,"
        "source_ref,disposition,result_declared_revision,result_workspace_state_id,"
        "actual_relative_path,justification,submitted_by,received_at,payload_sha256,idempotency_key) "
        "SELECT 'execution-2',board_id,card_id,target_id,2,result_investigation_receipt_id,"
        "source_ref,disposition,result_declared_revision,result_workspace_state_id,"
        "actual_relative_path,'Changed result',submitted_by,CURRENT_TIMESTAMP,:digest,'execution-2' "
        "FROM implementation_target_execution_records WHERE id='execution'"),
        {'digest': 'e' * 64})
    assert await revision() == before + 3
    await session.execute(text("INSERT INTO code_investigation_receipt_revocations "
        "(id,receipt_id,board_id,reason_code,justification,revoked_by,revoked_at) "
        "VALUES ('revocation',:receipt_id,:board_id,'invalid','Test','owner',CURRENT_TIMESTAMP)"),
        {'receipt_id': receipt_id, 'board_id': delivery.BOARD_ID})
    assert await revision() == before + 4
    await session.commit()
    assert (await session.execute(text("SELECT requested_revision FROM readiness_job "
        "WHERE scope_id=:scope_id"),
        {'scope_id': f'{delivery.SPEC_ID}:1'})).scalar_one() == before + 4
