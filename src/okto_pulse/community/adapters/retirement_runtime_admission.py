"""Refuse runtime schema initialization over an unfinished retirement cutover.

The existing data journal is durable evidence of an installer-owned run. Its
data-only checkpoints do not prove schema/graph/permission completion. In particular
`work` means data_preserved, not runtime_ready. Only a complete terminal
installation proof can admit a retained journal; mutable user data is not frozen.
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
    Retained runs require the terminal proof; corrupt records and data-only
    prefixes remain blocked.
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
        activated = False
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
                if not runtime:
                    raise _refuse("retirement_cutover_incomplete")
                from .retirement_activation import verify_retirement_activation

                try:
                    await verify_retirement_activation(connection, engine.url.database)
                except Exception as error:
                    raise _refuse("retirement_cutover_incomplete") from error
                activated = True
        # Losing the coordinator journal cannot erase the meaning of retained
        # effects. This is a negative signal only, never a replacement receipt.
        # event_type is indexed; private payloads are neither loaded nor parsed.
        if await _table_present(connection, "domain_events"):
            effects = (await connection.exec_driver_sql(
                "SELECT 1 FROM main.domain_events WHERE event_type IN (?, ?, ?, ?, ?) LIMIT 1", (
                    "migration.context_dispositions_committed", "historical_context.bound",
                    "migration.card_validation_preserved", "migration.work_superseded", "migration.work_retirement_completed",
                ))).scalar()
            if effects is not None and not activated:
                raise _refuse("retirement_cutover_incomplete")
        if not activated and engine.url.database and engine.url.database != ':memory:':
            from pathlib import Path

            if (Path(engine.url.database).resolve().parent / 'retirement-activation').exists():
                raise _refuse("retirement_cutover_incomplete")
        if runtime:
            legacy = (await connection.exec_driver_sql("SELECT 1 FROM main.sqlite_schema WHERE "
                "name COLLATE NOCASE IN ('sprints','sprint_history','sprint_qa_items','sprint_activation_baselines') LIMIT 1")).first()
            columns = (await connection.exec_driver_sql('PRAGMA main.table_info("cards")')).all()
            if legacy is not None or any(row[1].casefold() == "sprint_id" for row in columns):
                raise _refuse("retirement_legacy_schema_requires_offline_cutover")
