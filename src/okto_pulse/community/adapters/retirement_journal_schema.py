"""Transactional expansion of the private checkpoint ordinal contract.

SQLite cannot alter a CHECK in place. Copy raw cells inside the caller's write
reservation, preserve exact rows/triggers, and refuse unclassified dependents.
"""

from sqlalchemy import MetaData, inspect

from .sqlalchemy_models import RetirementDataCheckpoint

_NAME = "retirement_data_checkpoints"
_STAGE = "retirement_data_checkpoints_expanded"
_EXPECTED_COLUMNS = (("migration_id", "VARCHAR(128)", 1, 1), ("ordinal", "INTEGER", 1, 2),
    ("record_json", "JSON", 1, 0), ("sha256", "VARCHAR(64)", 1, 0))


async def expand_retirement_checkpoint_schema(connection):
    if connection.dialect.name != "sqlite" or not connection.in_transaction():
        raise ValueError("retirement_checkpoint_sqlite_transaction_required")
    columns = (await connection.exec_driver_sql(f'PRAGMA table_info("{_NAME}")')).all()
    if tuple((row[1], row[2].upper(), row[3], row[5]) for row in columns) != _EXPECTED_COLUMNS:
        raise ValueError("retirement_checkpoint_schema_mismatch")
    checks = await connection.run_sync(lambda sync: inspect(sync).get_check_constraints(_NAME))
    if len(checks) != 1 or checks[0]["name"] != "ck_retirement_checkpoint_ordinal":
        raise ValueError("retirement_checkpoint_constraint_mismatch")
    expression = " ".join(checks[0]["sqltext"].split())
    if expression == "ordinal >= 0 AND ordinal <= 8":
        return
    if expression not in {"ordinal >= 0 AND ordinal <= 3", "ordinal >= 0 AND ordinal <= 5", "ordinal >= 0 AND ordinal <= 6", "ordinal >= 0 AND ordinal <= 7"}:
        raise ValueError("retirement_checkpoint_constraint_mismatch")
    objects = (await connection.exec_driver_sql("SELECT type,name,sql FROM sqlite_schema WHERE "
        "name=? OR (name<>? AND tbl_name<>? AND lower(sql) LIKE ?) OR (tbl_name=? AND type IN ('trigger','index'))",
        (_STAGE, _NAME, _NAME, "%retirement_data_checkpoints%", _NAME))).all()
    triggers = []
    for kind, name, sql in objects:
        if kind == "index" and sql is None and name.startswith("sqlite_autoindex_"):
            continue
        if kind != "trigger" or name not in {"retirement_data_no_update", "retirement_data_no_delete"}:
            raise ValueError("retirement_checkpoint_unclassified_dependency")
        triggers.append(sql)
    count, size = (await connection.exec_driver_sql(f'SELECT count(*),coalesce(sum(length(CAST(record_json AS BLOB))),0) FROM {_NAME}')).one()
    if count > 100_000 or size > 64 * 1024 * 1024:
        raise ValueError("retirement_checkpoint_upgrade_limit")
    async def rows(table):
        return (await connection.exec_driver_sql(f'SELECT migration_id,ordinal,CAST(record_json AS BLOB),sha256 '
            f'FROM {table} ORDER BY migration_id,ordinal')).all()
    before = await rows(_NAME)
    stage = RetirementDataCheckpoint.__table__.to_metadata(MetaData(), name=_STAGE)
    await connection.run_sync(stage.create)
    await connection.exec_driver_sql(f'INSERT INTO {_STAGE} SELECT migration_id,ordinal,record_json,sha256 FROM {_NAME}')
    if await rows(_STAGE) != before:
        raise ValueError("retirement_checkpoint_upgrade_copy_mismatch")
    await connection.exec_driver_sql(f'DROP TABLE {_NAME}')
    await connection.exec_driver_sql(f'ALTER TABLE {_STAGE} RENAME TO {_NAME}')
    for statement in triggers:
        await connection.exec_driver_sql(statement)
    if await rows(_NAME) != before:
        raise ValueError("retirement_checkpoint_upgrade_copy_mismatch")
