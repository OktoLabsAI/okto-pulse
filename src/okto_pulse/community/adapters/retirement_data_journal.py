"""Atomic receipts for the context -> Card -> work portion of offline cutover.

This is not the schema/bootstrap coordinator or a public migration API. Archive,
grant and backup preparation precedes it; schema/graph/permission cutover follows.
The enclosing installer retains the returned run identity outside candidate SQL.
No checkpoint grants authority, supplies a disposition or certifies full cutover.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import re

from sqlalchemy import LargeBinary, cast, func, insert, select

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.community.adapters.sprint_retirement_archive import HistoricalArchiveReference, _encode
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow, RetirementDataCheckpoint

_TABLE = RetirementDataCheckpoint.__table__
_FORMAT = "retirement-data-journal/v1"
_STAGES = ("prepared", "context", "cards", "work", "graph_intent", "graphs", "permissions", "schema", "bootstrap", "activation")
_MAX_BYTES = 64 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _references(references):
    if (type(references) is not tuple or not references or len(references) > 100_000
            or any(not isinstance(item, HistoricalArchiveReference) for item in references)
            or len({item.board_id for item in references}) != len(references)):
        raise ValueError("retirement_data_archives_invalid")
    documents, total, source_bytes = [], 0, 0
    for item in sorted(references, key=lambda item: item.board_id):
        if type(item.size) is not int or item.size <= 0:
            raise ValueError("retirement_data_archives_invalid")
        encoded = _encode(asdict(item))
        total += len(encoded) + 2
        source_bytes += item.size
        if total > _MAX_BYTES or source_bytes > _MAX_BYTES:
            raise ValueError("retirement_data_journal_limit")
        documents.append(json.loads(encoded))
    return documents


@dataclass(frozen=True, slots=True)
class RetirementDataRun:
    migration_id: str
    input_sha256: str

    def __post_init__(self):
        if (type(self.migration_id) is not str or not self.migration_id.strip() or len(self.migration_id) > 128
                or type(self.input_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.input_sha256) is None):
            raise ValueError("retirement_data_run_invalid")


async def ensure_retirement_data_journal(connection):
    """Create only the internal journal, including on an existing pre-cutover DB."""
    if connection.dialect.name != "sqlite" or not connection.in_transaction():
        raise ValueError("retirement_data_sqlite_transaction_required")
    await connection.run_sync(lambda sync: _TABLE.create(sync, checkfirst=True))
    from .retirement_journal_schema import expand_retirement_checkpoint_schema
    await expand_retirement_checkpoint_schema(connection)
    for operation in ("UPDATE", "DELETE"):
        await connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS retirement_data_no_{operation.lower()}
            BEFORE {operation} ON retirement_data_checkpoints
            BEGIN SELECT RAISE(ABORT,'retirement_data_checkpoint_immutable'); END""")


async def _rows(connection, migration_id):
    count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(
        func.length(cast(_TABLE.c.record_json, LargeBinary)) + func.length(cast(_TABLE.c.sha256, LargeBinary))), 0))
        .where(_TABLE.c.migration_id == migration_id))).one()
    if count > len(_STAGES) or size > _MAX_BYTES:
        raise ValueError("retirement_data_journal_limit")
    return (await connection.execute(select(_TABLE).where(_TABLE.c.migration_id == migration_id)
        .order_by(_TABLE.c.ordinal))).mappings().all()


def _receipt(stage, payload):
    # Local imports avoid cycles with the transaction owners. These are the
    # existing typed evidence contracts, not an arbitrary JSON success claim.
    from okto_pulse.community.adapters.context_disposition_retirement import ContextDispositionReceipt
    from okto_pulse.community.adapters.card_validation_retirement import CardValidationRetirementReceipt
    from okto_pulse.community.adapters.sprint_work_retirement import WorkRetirementReceipt
    from okto_pulse.community.adapters.retirement_materialization import MaterializationCheckpoint
    from okto_pulse.community.adapters.permission_retirement_cleanup import PermissionRetirementCleanup
    from okto_pulse.community.adapters.retirement_schema_cutover import SchemaRetirementCheckpoint
    from okto_pulse.community.adapters.retirement_bootstrap import BootstrapRetirementCheckpoint
    from okto_pulse.community.adapters.retirement_activation import RetirementActivationCheckpoint
    contract = {"context": ContextDispositionReceipt, "cards": CardValidationRetirementReceipt, "work": WorkRetirementReceipt,
        "graph_intent": MaterializationCheckpoint, "graphs": MaterializationCheckpoint,
        "permissions": PermissionRetirementCleanup, "schema": SchemaRetirementCheckpoint,
        "bootstrap": BootstrapRetirementCheckpoint, "activation": RetirementActivationCheckpoint}[stage]
    try:
        result = contract(**payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("retirement_data_receipt_invalid") from exc
    if asdict(result) != payload:
        raise ValueError("retirement_data_receipt_invalid")
    return result


async def read_retirement_data_journal(connection, run: RetirementDataRun):
    if not isinstance(run, RetirementDataRun) or not connection.in_transaction():
        raise ValueError("retirement_data_transaction_required")
    rows = await _rows(connection, run.migration_id)
    if not rows:
        raise ValueError("retirement_data_journal_missing")
    previous, records = None, []
    for ordinal, row in enumerate(rows):
        record = row["record_json"]
        if (row["ordinal"] != ordinal or type(record) is not dict
                or set(record) != {"format", "migration_id", "stage", "previous_sha256", "payload"}
                or record["format"] != _FORMAT or record["migration_id"] != run.migration_id
                or record["stage"] != _STAGES[ordinal] or record["previous_sha256"] != previous
                or row["sha256"] != _digest(record)):
            raise ValueError("retirement_data_journal_mismatch")
        payload = record["payload"]
        if ordinal == 0:
            if (type(payload) is not dict or set(payload) != {"archives", "plan_sha256"}
                    or row["sha256"] != run.input_sha256):
                raise ValueError("retirement_data_input_mismatch")
        elif _receipt(_STAGES[ordinal], payload).migration_id != run.migration_id:
            raise ValueError("retirement_data_receipt_scope_mismatch")
        previous = row["sha256"]
        records.append(record)
    return tuple(records)


async def prepare_retirement_data_run(engine, storage, references, *, plan: ContextDispositionPlan):
    """Create an immutable input anchor before any of the three data steps.

    Repeating preparation never adopts pre-existing uncoordinated effects.
    Once returned, retain this run identity and use it on every resume. Losing
    the journal cannot be repaired from mutable candidate state.
    """
    from okto_pulse.community.adapters.historical_archive_grant_installation import _install_historical_archive_grants
    from okto_pulse.community.adapters.context_disposition_retirement import _require_original_archive
    if engine.dialect.name != "sqlite" or not isinstance(plan, ContextDispositionPlan):
        raise ValueError("retirement_data_input_invalid")
    archives = _references(references)
    if any(item["migration_id"] != plan.migration_id for item in archives):
        raise ValueError("retirement_data_input_mismatch")
    record = {"format": _FORMAT, "migration_id": plan.migration_id, "stage": "prepared", "previous_sha256": None,
        "payload": {"archives": archives, "plan_sha256": _digest(plan.model_dump(mode="json"))}}
    if len(json.dumps(record, allow_nan=False).encode("utf-8")) > _MAX_BYTES:
        raise ValueError("retirement_data_journal_limit")
    run = RetirementDataRun(plan.migration_id, _digest(record))
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await ensure_retirement_data_journal(connection)
            if await _rows(connection, run.migration_id):
                await read_retirement_data_journal(connection, run)
            else:
                event = DomainEventRow.__table__
                prior = await connection.scalar(select(event.c.id).where(
                    event.c.payload_json["migration_id"].as_string() == run.migration_id,
                    event.c.event_type.in_(("migration.context_dispositions_committed", "historical_context.bound",
                        "migration.card_validation_preserved", "migration.work_superseded", "migration.work_retirement_completed"))).limit(1))
                if prior is not None:
                    raise ValueError("retirement_data_uncoordinated_effects")
                for reference in references:
                    await _install_historical_archive_grants(connection, storage, reference, require_existing=True)
                await _require_original_archive(connection, references, run.migration_id)
                await connection.execute(insert(_TABLE).values(migration_id=run.migration_id, ordinal=0,
                    record_json=record, sha256=run.input_sha256))
                await read_retirement_data_journal(connection, run)
                await _require_original_archive(connection, references, run.migration_id)
            await connection.commit()
            return run
        except BaseException:
            await connection.rollback()
            raise


async def require_retirement_stage(connection, run, stage, references, *, plan=None, dependency=None):
    """Check the exact input and prefix inside the step's BEGIN IMMEDIATE."""
    if run is None:
        return
    records = await read_retirement_data_journal(connection, run)
    if records[0]["payload"]["archives"] != _references(references):
        raise ValueError("retirement_data_input_mismatch")
    ordinal = _STAGES.index(stage)
    if len(records) < ordinal:
        raise ValueError("retirement_data_stage_order_invalid")
    if stage == "context":
        if not isinstance(plan, ContextDispositionPlan) or _digest(plan.model_dump(mode="json")) != records[0]["payload"]["plan_sha256"]:
            raise ValueError("retirement_data_input_mismatch")
    elif dependency is None or asdict(dependency) != records[ordinal - 1]["payload"]:
        raise ValueError("retirement_data_dependency_mismatch")


async def record_retirement_stage(connection, run, stage, receipt, *, replay):
    """Called before final step verification; data and checkpoint commit together."""
    if run is None:
        return
    records = await read_retirement_data_journal(connection, run)
    ordinal = _STAGES.index(stage)
    payload = asdict(receipt)
    if _receipt(stage, payload).migration_id != run.migration_id or len(records) < ordinal:
        raise ValueError("retirement_data_stage_order_invalid")
    previous = _digest(records[ordinal - 1])
    record = {"format": _FORMAT, "migration_id": run.migration_id, "stage": stage,
        "previous_sha256": previous, "payload": payload}
    if replay:
        if len(records) <= ordinal or records[ordinal] != record:
            raise ValueError("retirement_data_replay_mismatch")
        return
    if len(records) != ordinal:
        raise ValueError("retirement_data_unexpected_checkpoint")
    await connection.execute(insert(_TABLE).values(migration_id=run.migration_id, ordinal=ordinal,
        record_json=record, sha256=_digest(record)))
    if (await read_retirement_data_journal(connection, run)) != (*records, record):
        raise ValueError("retirement_data_journal_mismatch")


async def resume_retirement_data_run(engine, storage, run: RetirementDataRun, *, plan: ContextDispositionPlan):
    """Resume verified data steps; never recapture transformed original sources.

    The enclosing cutover owns runtime/graph writer exclusion and joint backup.
    This result means data_preserved only, not schema/permission/graph completion
    or permission to serve the new runtime. No startup registration is made here.
    """
    from okto_pulse.community.adapters.context_disposition_retirement import install_context_dispositions
    from okto_pulse.community.adapters.card_validation_retirement import materialize_archived_card_policies
    from okto_pulse.community.adapters.sprint_work_retirement import supersede_archived_sprint_work
    async with engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN")
        records = await read_retirement_data_journal(connection, run)
        archives = records[0]["payload"]["archives"]
        references = tuple(HistoricalArchiveReference(**{**item, "counts": tuple(tuple(pair) for pair in item["counts"])}) for item in archives)
        await require_retirement_stage(connection, run, "context", references, plan=plan)
        await connection.rollback()
    # Each owner rechecks its prefix inside its write reservation. A competing
    # resume can only commit the same evidence or return the verified replay.
    prior = {record["stage"]: _receipt(record["stage"], record["payload"]) for record in records[1:]}
    context = await install_context_dispositions(engine, storage, references, plan=plan,
        expected_receipt=prior.get("context"), checkpoint_run=run)
    cards = await materialize_archived_card_policies(engine, storage, references, migration_id=run.migration_id,
        context_receipt=context, expected_receipt=prior.get("cards"), checkpoint_run=run)
    work = await supersede_archived_sprint_work(engine, storage, references, card_receipt=cards,
        expected_receipt=prior.get("work"), checkpoint_run=run)
    return {"state": "data_preserved", "run": run, "context": context, "cards": cards, "work": work}
