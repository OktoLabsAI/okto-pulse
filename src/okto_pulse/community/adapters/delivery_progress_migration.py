"""Exact predecessor upgrade of the canonical ledger, without rewriting rows."""

import re

from sqlalchemy import MetaData, inspect
from sqlalchemy.schema import CreateIndex, CreateTable

from .sqlalchemy_models import CardDeliveryEvidenceRecordRow, CARD_DELIVERY_GUARDS


async def migrate_delivery_progress(engine):
    # This is the same edition-owned contract comparison used by other SQLite
    # rebuilds; it is intentionally not a new public maintenance surface.
    from .relational_schema_steps import _sqlite_owned_table_contract

    table = CardDeliveryEvidenceRecordRow.__table__
    name = table.name
    temporary = name + "__progress_upgrade"

    def migrate(conn):
        if name not in inspect(conn).get_table_names():
            return "skipped"
        contract = _sqlite_owned_table_contract(conn, table)
        current = contract["expected"]
        prior = {
            **current,
            "checks": tuple(
                (key, expression.replace(",'progress'", ""))
                for key, expression in current["checks"]
            ),
        }
        if prior == current:
            raise RuntimeError("delivery_progress_predecessor_invalid")
        if contract["observed"] not in (current, prior):
            raise RuntimeError("delivery_progress_schema_drift")

        def normalize(sql):
            return re.sub(r"\s+", "", sql.replace("IF NOT EXISTS", "")).lower()

        triggers = dict(
            conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (name,),
            ).all()
        )
        expected_triggers = {
            f"trg_card_delivery_evidence_{key}": f"CREATE TRIGGER trg_card_delivery_evidence_{key} {body}"
            for key, body in CARD_DELIVERY_GUARDS.items()
        }
        if {key: normalize(value) for key, value in triggers.items()} != {
            key: normalize(value) for key, value in expected_triggers.items()
        }:
            raise RuntimeError("delivery_progress_trigger_drift")
        if contract["observed"] == current:
            return "skipped"
        if temporary in inspect(conn).get_table_names():
            raise RuntimeError("delivery_progress_stale_upgrade_table")
        # No table is allowed to reference this ledger by FK: dropping it must
        # not cascade into another audit surface, now or after future changes.
        if any(
            fk["referred_table"] == name
            for other in inspect(conn).get_table_names()
            for fk in inspect(conn).get_foreign_keys(other)
        ):
            raise RuntimeError("delivery_progress_inbound_foreign_key")
        before = conn.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY id').all()
        metadata = MetaData()
        for fk in table.foreign_keys:
            fk.column.table.to_metadata(metadata)
        replacement = table.to_metadata(metadata, name=temporary)
        conn.execute(CreateTable(replacement))
        columns = ", ".join(f'"{column.name}"' for column in table.columns)
        conn.exec_driver_sql(
            f'INSERT INTO "{temporary}" ({columns}) SELECT {columns} FROM "{name}"'
        )
        conn.exec_driver_sql(f'DROP TABLE "{name}"')
        conn.exec_driver_sql(f'ALTER TABLE "{temporary}" RENAME TO "{name}"')
        for index in table.indexes:
            conn.execute(CreateIndex(index))
        for sql in expected_triggers.values():
            conn.exec_driver_sql(sql)
        if conn.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY id').all() != before:
            raise RuntimeError("delivery_progress_history_mismatch")
        after = _sqlite_owned_table_contract(conn, table)
        if after["observed"] != current:
            raise RuntimeError("delivery_progress_upgrade_incomplete")
        if conn.exec_driver_sql(f'PRAGMA foreign_key_check("{name}")').all():
            raise RuntimeError("delivery_progress_foreign_key_violation")
        return "applied"

    async with engine.connect() as connection:
        if connection.dialect.name != "sqlite":
            raise RuntimeError("delivery_progress_community_sqlite_required")
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            result = await connection.run_sync(migrate)
            await connection.commit()
            return result
        except BaseException:
            await connection.rollback()
            raise
