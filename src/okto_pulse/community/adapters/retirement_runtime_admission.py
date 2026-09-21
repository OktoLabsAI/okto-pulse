"""Refuse runtime schema initialization over an unfinished retirement cutover.

The existing data journal is durable evidence of an installer-owned run. None
of its checkpoints proves schema/graph/permission completion. In particular
`work` means data_preserved, not runtime_ready. A future full cutover must provide
and verify its terminal contract before this gate can admit that schema.
"""

from okto_pulse.core.ports import SchemaMigrationError


def _refuse(reason):
    return SchemaMigrationError(reason, step_id="retirement_runtime_admission",
        phase="pre_create_all", remediation=(
            "Resume the authorized offline cutover using its original backup and retained receipts, "
            "or restore that backup with its matching build pair. Do not delete migration evidence."))


async def _table_present(connection, name):
    objects = (await connection.exec_driver_sql(
        "SELECT type FROM main.sqlite_schema WHERE name = ? COLLATE NOCASE", (name,)
    )).fetchmany(2)
    if not objects:
        return False
    if objects != [("table",)]:
        raise _refuse("retirement_runtime_journal_schema_invalid")
    return True


async def require_retirement_runtime_admission(engine):
    """Read only bounded structure/presence, before any migration or seed.

    Empty/absent journals admit only when no transformation receipts remain.
    Any retained run blocks, including corrupt records and a completed data-only prefix.
    This is startup admission, not exclusion of concurrent raw database writers.
    The installer must still own its continuous runtime/schema writer window.
    """
    await _require_admission(engine, runtime=True)


async def require_retirement_not_started(engine):
    """Installer preflight permits its original schema, never retained effects."""
    await _require_admission(engine, runtime=False)


async def _require_admission(engine, *, runtime):
    if engine.dialect.name != "sqlite":
        raise _refuse("retirement_runtime_backend_unsupported")
    async with engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN")
        if await _table_present(connection, "retirement_data_checkpoints"):
            columns = (await connection.exec_driver_sql(
                'PRAGMA main.table_info("retirement_data_checkpoints")'
            )).fetchmany(5)
            expected = (
                ("migration_id", "VARCHAR(128)", 1, 1),
                ("ordinal", "INTEGER", 1, 2),
                ("record_json", "JSON", 1, 0),
                ("sha256", "VARCHAR(64)", 1, 0),
            )
            if tuple((row[1], row[2].upper(), row[3], row[5]) for row in columns) != expected:
                raise _refuse("retirement_runtime_journal_schema_invalid")
            present = (await connection.exec_driver_sql(
                "SELECT 1 FROM main.retirement_data_checkpoints LIMIT 1")).scalar()
            if present is not None:
                raise _refuse("retirement_cutover_incomplete")
        # Losing the coordinator journal cannot erase the meaning of retained
        # effects. This is a negative signal only, never a replacement receipt.
        # event_type is indexed; private payloads are neither loaded nor parsed.
        if await _table_present(connection, "domain_events"):
            effects = (await connection.exec_driver_sql(
                "SELECT 1 FROM main.domain_events WHERE event_type IN (?, ?, ?, ?, ?) LIMIT 1", (
                    "migration.context_dispositions_committed", "historical_context.bound",
                    "migration.card_validation_preserved", "migration.work_superseded", "migration.work_retirement_completed",
                ))).scalar()
            if effects is not None:
                raise _refuse("retirement_cutover_incomplete")
        if runtime:
            legacy = (await connection.exec_driver_sql("SELECT 1 FROM main.sqlite_schema WHERE "
                "name COLLATE NOCASE IN ('sprints','sprint_history','sprint_qa_items','sprint_activation_baselines') LIMIT 1")).first()
            columns = (await connection.exec_driver_sql('PRAGMA main.table_info("cards")')).all()
            if legacy is not None or any(row[1].casefold() == "sprint_id" for row in columns):
                raise _refuse("retirement_legacy_schema_requires_offline_cutover")
