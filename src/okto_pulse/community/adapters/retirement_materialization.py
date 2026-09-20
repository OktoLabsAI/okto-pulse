"""Durable graph intent and atomic SQL completion of offline retirement.

Graph commits precede their SQL acknowledgement. Replay always uses the sealed
original selection. No row here certifies schema/permission cutover or startup.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
import re

from .global_outbox_retirement import _apply_outbox_in_transaction, _sha, _snapshot
from .grafx_global_retirement import apply_global_graph_retirement
from .grafx_sprint_retirement import apply_sprint_graph_retirement
from .permission_retirement_checkpoint import read_permission_retirement_checkpoint
from .retirement_data_journal import ensure_retirement_data_journal, read_retirement_data_journal, record_retirement_stage
from .retirement_materialization_plan import (
    decode_materialization_plan, graph_handles, require_materialization_bindings, require_materialization_states,
)
from .sprint_retirement_archive import _encode


@dataclass(frozen=True, slots=True)
class MaterializationCheckpoint:
    migration_id: str
    plan_sha256: str
    backup_sha256: str
    outbox_sha256: str

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 128
                or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in (self.plan_sha256, self.backup_sha256, self.outbox_sha256))):
            raise ValueError("retirement_materialization_checkpoint_invalid")


async def resume_retirement_materialization(runtime, run, permission, payload, backup, manifest, graphs, kg_root: Path):
    """Caller holds schema, startup and binding-publication exclusion throughout."""
    plan = decode_materialization_plan(payload)
    receipt = MaterializationCheckpoint(run.migration_id, _sha(_encode(payload)), backup.manifest_sha256, plan.after_sha256)
    boards, global_db = graph_handles(graphs)
    source = runtime.local_database_path()

    async def verify(connection, *, original=False, retired=False):
        await read_permission_retirement_checkpoint(connection, permission)
        require_materialization_bindings(source, kg_root, graphs, manifest)
        actual_boards = (await connection.exec_driver_sql("SELECT id FROM boards ORDER BY id")).scalars().all()
        if actual_boards != manifest["routing_inventory"]["board_ids"]:
            raise ValueError("retirement_materialization_board_population_mismatch")
        all_retired = require_materialization_states(plan, graphs, original=original, retired=retired)
        raw = await connection.run_sync(_snapshot)
        if original and raw != plan.original:
            raise ValueError("retirement_materialization_original_outbox_mismatch")
        if retired and _sha(raw) != plan.after_sha256:
            raise ValueError("retirement_materialization_outbox_mismatch")
        if raw != plan.original and (_sha(raw) != plan.after_sha256 or not all_retired):
            raise ValueError("retirement_materialization_stage_order_invalid")

    async with runtime.engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            await ensure_retirement_data_journal(connection)
            records = await read_retirement_data_journal(connection, run)
            if len(records) < 4:
                raise ValueError("retirement_materialization_data_incomplete")
            replay = len(records) >= 5
            # Missing intent cannot be reconstructed from already changed graphs.
            await verify(connection, original=not replay, retired=len(records) == 6)
            await record_retirement_stage(connection, run, "graph_intent", receipt, replay=replay)
            if len(records) == 6 and records[5]["payload"] != asdict(receipt):
                raise ValueError("retirement_materialization_completion_mismatch")
            await verify(connection, original=not replay, retired=len(records) == 6)
            await connection.commit()
        finally:
            await connection.rollback()

    for item in plan.board_plans:
        apply_sprint_graph_retirement(boards[item.board_id], item)
    if plan.graph_plan is not None:
        apply_global_graph_retirement(global_db, plan.graph_plan, board_databases=boards)

    async with runtime.engine.connect() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            records = await read_retirement_data_journal(connection, run)
            if len(records) not in {5, 6} or records[4]["payload"] != asdict(receipt):
                raise ValueError("retirement_materialization_intent_mismatch")
            await verify(connection)
            require_materialization_states(plan, graphs, retired=True)
            await _apply_outbox_in_transaction(connection, plan)
            await record_retirement_stage(connection, run, "graphs", receipt, replay=len(records) == 6)
            # Checkpoint triggers must not silently damage earlier authority,
            # outbox evidence or the just-verified graph state.
            await verify(connection, retired=True)
            complete = await read_retirement_data_journal(connection, run)
            if len(complete) != 6 or complete[5]["payload"] != asdict(receipt):
                raise ValueError("retirement_materialization_completion_mismatch")
            await connection.commit()
        finally:
            await connection.rollback()
    return receipt
