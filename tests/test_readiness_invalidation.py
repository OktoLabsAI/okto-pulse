"""Source and projection intent commit or roll back together."""

from sqlalchemy import create_engine

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
