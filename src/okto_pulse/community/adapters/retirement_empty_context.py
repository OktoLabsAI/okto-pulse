"""Checkpoint-bound proof for a complete empty Sprint archive population.

No archive, Board, context decision or historical origin is fabricated. The
normal archive census proves emptiness before the atomic context checkpoint.
Later replay uses that retained proof, not mutable post-cutover source tables.
"""

from dataclasses import asdict, replace

from .retirement_data_journal import (
    RetirementDataRun, _digest, read_retirement_data_journal, record_retirement_stage,
)


def _receipt(run):
    from .context_disposition_retirement import ContextDispositionReceipt
    return ContextDispositionReceipt(run.migration_id, _digest({
        'format': 'retirement-empty-context/v1', 'migration_id': run.migration_id,
        'input_sha256': run.input_sha256}), 0, 0)


async def verify_empty_context(connection, *, expected_receipt, expected_plan=None, candidates=None):
    from .context_disposition_retirement import ContextDispositionReceipt, _journal
    if type(expected_receipt) is not ContextDispositionReceipt or candidates not in (None, ()):
        raise ValueError('context_disposition_empty_checkpoint_required')
    first = (await connection.exec_driver_sql(
        'SELECT sha256 FROM retirement_data_checkpoints WHERE migration_id=? AND ordinal=0',
        (expected_receipt.migration_id,))).scalar_one_or_none()
    if first is None:
        raise ValueError('context_disposition_empty_checkpoint_required')
    run = RetirementDataRun(expected_receipt.migration_id, first)
    records = await read_retirement_data_journal(connection, run)
    receipt = _receipt(run)
    if (records[0]['payload']['archives'] != [] or len(records) < 2
            or records[1]['payload'] != asdict(receipt) or receipt != expected_receipt
            or await _journal(connection, run.migration_id)):
        raise ValueError('context_disposition_empty_checkpoint_mismatch')
    if expected_plan is not None and (expected_plan.migration_id != run.migration_id or expected_plan.decisions
            or _digest(expected_plan.model_dump(mode='json')) != records[0]['payload']['plan_sha256']):
        raise ValueError('context_disposition_replay_mismatch')
    return receipt, {}


async def install_empty_context(connection, *, plan, expected_receipt, checkpoint_run):
    from .context_disposition_retirement import _journal, _require_original_archive
    from .sprint_retirement_preflight import inspect_sprint_pretransform
    if type(checkpoint_run) is not RetirementDataRun or plan.decisions:
        raise ValueError('context_disposition_empty_checkpoint_required')
    records = await read_retirement_data_journal(connection, checkpoint_run)
    if records[0]['payload']['archives'] != []:
        raise ValueError('context_disposition_archive_scope_invalid')
    receipt = _receipt(checkpoint_run)
    if len(records) > 1:
        observed, _ = await verify_empty_context(connection, expected_receipt=receipt,
            expected_plan=plan, candidates=())
        if expected_receipt is not None and expected_receipt != observed:
            raise ValueError('context_disposition_replay_mismatch')
        await record_retirement_stage(connection, checkpoint_run, 'context', receipt, replay=True)
        return receipt
    if expected_receipt is not None or await _journal(connection, plan.migration_id):
        raise ValueError('context_disposition_replay_mismatch')
    before = await connection.run_sync(inspect_sprint_pretransform)
    before.require_resolved_pretransform()
    await _require_original_archive(connection, (), plan.migration_id)
    await record_retirement_stage(connection, checkpoint_run, 'context', receipt, replay=False)
    after = await connection.run_sync(inspect_sprint_pretransform)
    after.require_resolved_pretransform()
    # The census also counts the journal JSON itself. Admit exactly the one
    # checkpoint just verified by record_retirement_stage; every other census
    # fact must remain identical, including the resolved embedded references.
    embedded = before.relational.embedded_references
    key = 'retirement_data_checkpoints.record_json'
    if sum(name == key for name, _ in embedded.scanned_counts) != 1:
        raise ValueError('context_disposition_source_changed')
    expected_embedded = replace(embedded, scanned_counts=tuple(
        (name, count + 1 if name == key else count) for name, count in embedded.scanned_counts))
    expected = replace(before, relational=replace(before.relational, embedded_references=expected_embedded))
    if after != expected:
        raise ValueError('context_disposition_source_changed')
    await _require_original_archive(connection, (), plan.migration_id)
    await verify_empty_context(connection, expected_receipt=receipt, expected_plan=plan, candidates=())
    return receipt
