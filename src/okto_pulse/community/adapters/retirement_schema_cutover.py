"""Physical schema and durable receipt commit together; startup stays closed."""

from dataclasses import dataclass
import re

from .context_disposition_retirement import _documents
from .retirement_data_journal import ensure_retirement_data_journal, read_retirement_data_journal, record_retirement_stage
from .retirement_schema_storage import cut_retired_schema, require_cut_schema
from .sprint_retirement_archive import HistoricalArchiveReference


@dataclass(frozen=True, slots=True)
class SchemaRetirementCheckpoint:
    migration_id: str
    before_schema_sha256: str
    after_schema_sha256: str
    data_sha256: str
    cards: int

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 128
                or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in (self.before_schema_sha256, self.after_schema_sha256, self.data_sha256))
                or type(self.cards) is not int or not 0 <= self.cards <= 100_000):
            raise ValueError("retirement_schema_receipt_invalid")


async def retire_schema(engine, run, storage, *, verify_dependency):
    """Caller continuously holds schema/startup/graph publication exclusion."""
    if engine.dialect.name != "sqlite" or not callable(verify_dependency):
        raise ValueError("retirement_schema_coordinator_required")
    async with engine.connect() as connection:
        settings = {name: (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar_one()
            for name in ("foreign_keys", "legacy_alter_table")}
        await connection.rollback()
        try:
            await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            records = await read_retirement_data_journal(connection, run)
            if len(records) not in {7, 8}:
                raise ValueError("retirement_schema_dependencies_incomplete")
            await verify_dependency(connection)
            references = tuple(HistoricalArchiveReference(**{**item, "counts": tuple(tuple(pair) for pair in item["counts"])})
                for item in records[0]["payload"]["archives"])
            documents = await _documents(connection, storage, references)
            if len(records) == 8:
                receipt = SchemaRetirementCheckpoint(**records[7]["payload"])
                await connection.run_sync(lambda sync: require_cut_schema(sync, receipt))
            else:
                # Expanding the journal changes the schema hash. Never do it
                # before validating an already-retained schema-cut receipt.
                await ensure_retirement_data_journal(connection)
                values = await connection.run_sync(lambda sync: cut_retired_schema(sync, documents))
                receipt = SchemaRetirementCheckpoint(run.migration_id, **values)
            await record_retirement_stage(connection, run, "schema", receipt, replay=len(records) == 8)
            await verify_dependency(connection)
            await connection.run_sync(lambda sync: require_cut_schema(sync, receipt))
            complete = await read_retirement_data_journal(connection, run)
            if complete[:len(records)] != records or len(complete) != 8:
                raise ValueError("retirement_schema_checkpoint_changed")
            await connection.commit()
            return receipt
        except BaseException:
            await connection.rollback()
            raise
        finally:
            # Restore outside the SQL transaction; invalidate instead of pooling
            # a connection whose enforcement could not be restored.
            try:
                await connection.rollback()
                for name, value in settings.items():
                    await connection.exec_driver_sql(f"PRAGMA {name}={int(value)}")
                    if (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar_one() != value:
                        raise ValueError("retirement_schema_connection_restore_failed")
                await connection.rollback()
            except BaseException:
                await connection.invalidate()
                raise
