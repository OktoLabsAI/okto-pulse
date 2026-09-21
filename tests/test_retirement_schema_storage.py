"""Physical cut preserves raw cells, incoming references and extension DDL."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from okto_pulse.community.adapters.retirement_schema_storage import (
    RETIRED_TABLES, cut_retired_schema, surviving_data,
)
from okto_pulse.community.adapters.sprint_retirement_archive import _cell


@pytest.fixture
def candidate(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'physical.db'}")
    with engine.connect() as connection:
        for statement in (
            "CREATE TABLE sprints(id TEXT PRIMARY KEY, opaque BLOB)",
            "CREATE TABLE sprint_history(id TEXT PRIMARY KEY, sprint_id TEXT REFERENCES sprints(id))",
            "CREATE TABLE sprint_qa_items(id TEXT PRIMARY KEY, sprint_id TEXT REFERENCES sprints(id))",
            "CREATE TABLE sprint_activation_baselines(id TEXT PRIMARY KEY, sprint_id TEXT REFERENCES sprints(id))",
            "CREATE TABLE cards(id VARCHAR(36) PRIMARY KEY, sprint_id VARCHAR(36), payload TEXT COLLATE NOCASE DEFAULT '(' CHECK(length(payload)>0), "
                "size INTEGER GENERATED ALWAYS AS (length(payload)) STORED, FOREIGN KEY(sprint_id) REFERENCES sprints(id) ON DELETE SET NULL)",
            "CREATE TABLE children(id TEXT PRIMARY KEY, card_id TEXT REFERENCES cards(id) ON DELETE CASCADE)",
            "CREATE UNIQUE INDEX ix_cards_payload ON cards(payload)",
            "CREATE INDEX ix_cards_sprint_id ON cards(sprint_id)",
            "CREATE VIEW card_view AS SELECT id,payload FROM cards",
            "CREATE TRIGGER protect_card BEFORE DELETE ON cards BEGIN SELECT RAISE(ABORT,'immutable'); END",
            "INSERT INTO sprints VALUES ('origin',X'0102FF')",
            "INSERT INTO sprint_history VALUES ('h','origin')",
            "INSERT INTO sprint_qa_items VALUES ('q','origin')",
            "INSERT INTO sprint_activation_baselines VALUES ('b','origin')",
            "INSERT INTO cards(rowid,id,payload) VALUES (77,'card','{ \"raw\" : \"Ω\" }')",
            "INSERT INTO children VALUES ('child','card')",
        ):
            connection.exec_driver_sql(statement)
        connection.commit()
        documents = {"board": {"tables": {}}}
        for table in RETIRED_TABLES:
            names = [row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')]
            documents["board"]["tables"][table] = {"columns": [{"name": name} for name in names],
                "rows": [[_cell(cell) for cell in row] for row in connection.exec_driver_sql(f'SELECT * FROM "{table}"')]}
        connection.rollback()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        yield connection, documents
        connection.rollback()
    engine.dispose()


def snapshot(connection):
    schema = connection.exec_driver_sql("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name").all()
    return schema, {name: connection.exec_driver_sql(f'SELECT * FROM "{name}"').all()
        for kind, name, _, _ in schema if kind == "table"}


def test_cut_preserves_children_raw_data_views_checks_collation_and_trigger(candidate):
    connection, documents = candidate
    before = surviving_data(connection)
    receipt = cut_retired_schema(connection, documents)
    assert receipt["cards"] == 1 and surviving_data(connection) == before
    assert connection.exec_driver_sql("SELECT rowid FROM cards").scalar_one() == 77
    assert connection.exec_driver_sql("SELECT count(*) FROM children").scalar_one() == 1
    assert connection.exec_driver_sql("SELECT payload FROM card_view").scalar_one() == '{ "raw" : "Ω" }'
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert not set(RETIRED_TABLES) & set(connection.exec_driver_sql("SELECT name FROM sqlite_schema").scalars())
    with pytest.raises(Exception, match="immutable"):
        connection.exec_driver_sql("DELETE FROM cards")
    with pytest.raises(Exception, match="CHECK constraint failed"):
        connection.exec_driver_sql("INSERT INTO cards(id,payload) VALUES ('bad','')")
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        connection.exec_driver_sql("INSERT INTO cards(id,payload) SELECT 'duplicate',upper(payload) FROM cards")


@pytest.mark.parametrize("damage", [
    "UPDATE sprints SET opaque=X'00'",
    "UPDATE cards SET sprint_id='origin'",
    "CREATE TABLE extension(id TEXT, origin TEXT REFERENCES sprints(id))",
    "CREATE VIEW extension AS SELECT id FROM 'sprints'",
    "CREATE INDEX extension ON cards(sprint_id,payload)",
    "CREATE TRIGGER extension AFTER UPDATE ON cards BEGIN UPDATE sprints SET opaque=X'00'; END",
])
def test_unclassified_dependencies_or_unpreserved_sources_refuse_before_cut(candidate, damage):
    connection, documents = candidate
    connection.exec_driver_sql(damage)
    before = snapshot(connection)
    with pytest.raises(ValueError, match="retirement_schema_"):
        cut_retired_schema(connection, documents)
    assert snapshot(connection) == before


def test_failure_after_drop_rolls_back_ddl_and_all_rows(candidate, monkeypatch):
    connection, documents = candidate
    before = snapshot(connection)
    connection.commit()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    execute = Connection.exec_driver_sql
    def fail(self, statement, *args, **kwargs):
        if statement.startswith('ALTER TABLE "cards_retirement_candidate"'):
            raise RuntimeError("lost after cards drop")
        return execute(self, statement, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Connection, "exec_driver_sql", fail)
        with pytest.raises(RuntimeError, match="lost after cards drop"):
            cut_retired_schema(connection, documents)
        connection.rollback()
    assert snapshot(connection) == before


def test_trigger_sharing_cards_name_keeps_its_guard(candidate):
    connection, documents = candidate
    connection.exec_driver_sql("CREATE TRIGGER cards BEFORE UPDATE ON cards BEGIN SELECT RAISE(ABORT,'guard retained'); END")
    cut_retired_schema(connection, documents)
    with pytest.raises(Exception, match="guard retained"):
        connection.exec_driver_sql("UPDATE cards SET payload='changed'")


def test_trigger_sharing_cards_name_cannot_hide_retired_dependency(candidate):
    connection, documents = candidate
    connection.exec_driver_sql("CREATE TRIGGER cards AFTER UPDATE ON children BEGIN UPDATE sprints SET opaque=X'00'; END")
    before = snapshot(connection)
    with pytest.raises(ValueError, match="dependency_unclassified:cards"):
        cut_retired_schema(connection, documents)
    assert snapshot(connection) == before
