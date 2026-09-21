"""Atomic internal retirement of policy leaves with retained before/after evidence.

The coordinator must retain both receipts outside this candidate transaction.
This operation is not a startup hook and does not authorize a policy decision.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re

from sqlalchemy import LargeBinary, cast, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from okto_pulse.core.ports.permission_retirement import (
    SOURCE_VERSION, retire_permission_document, validate_permission_retirement_registry,
)
from okto_pulse.community.adapters.permission_retirement_checkpoint import (
    PermissionRetirementCheckpoint, _capture_contexts, _digest, _id, _rows,
    read_permission_retirement_checkpoint,
)
from okto_pulse.community.adapters.permission_retirement_review_installation import (
    _AUDIT, _LAYERS, _install_reviews, _require_loaded_parity,
)

_PHASE = "permission_retirement_cleanup"
_FORMAT = "permission-retirement-cleanup/v1"
_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PermissionRetirementCleanup:
    migration_id: str
    checkpoint_sha256: str
    evidence_sha256: str
    changed_documents: int
    removed_entries: int
    review_markers: int

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 255
                or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in (self.checkpoint_sha256, self.evidence_sha256))
                or any(type(value) is not int or not 0 <= value <= 100_000
                    for value in (self.changed_documents, self.review_markers))
                or type(self.removed_entries) is not int or not 0 <= self.removed_entries <= 60_000_000):
            raise ValueError("permission_retirement_cleanup_receipt_invalid")


def _evidence(checkpoint, retired_flags, changes, marker_count):
    payload = {"format": _FORMAT, "migration_id": checkpoint.migration_id,
        "checkpoint_sha256": checkpoint.evidence_sha256, "source_sha256": checkpoint.source_sha256,
        "retired_flags": sorted(retired_flags), "review_markers": marker_count, "changes": changes}
    # Bound the actual SQLAlchemy JSON encoding, including retained source trees.
    if len(json.dumps(payload, allow_nan=False).encode("utf-8")) > _MAX_BYTES:
        raise ValueError("permission_retirement_cleanup_limit")
    receipt = PermissionRetirementCleanup(checkpoint.migration_id, checkpoint.evidence_sha256,
        _digest(payload), len(changes), sum(len(change["removed_paths"]) for change in changes), marker_count)
    row = {"id": _id(checkpoint.migration_id, "cleanup"), "manifest_version": SOURCE_VERSION,
        "phase": _PHASE, "classification": "retirement_cleanup", "subject_id": None, "base_preset_id": None,
        "before_digest": checkpoint.source_sha256, "after_digest": receipt.evidence_sha256,
        "introduced_true_count": 0, "introduced_false_count": 0, "owner_review_required": bool(marker_count),
        "mutation_count": len(changes), "details": payload}
    return receipt, row


async def _read_completion(connection, checkpoint, retired_flags):
    # Include the deterministic identity even if its phase/details were corrupted.
    selection = (_AUDIT.c.id == _id(checkpoint.migration_id, "cleanup")) | (
        (_AUDIT.c.phase == _PHASE) & (_AUDIT.c.details["migration_id"].as_string() == checkpoint.migration_id))
    count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(
        func.length(cast(_AUDIT.c.details, LargeBinary))), 0)).where(selection))).one()
    if count == 0:
        return None
    if count != 1 or size > _MAX_BYTES:
        raise ValueError("permission_retirement_cleanup_evidence_mismatch")
    stored = dict((await connection.execute(select(_AUDIT).where(selection))).mappings().one())
    stored.pop("created_at")
    try:
        changes = stored["details"]["changes"]
        if type(changes) is not list or len(changes) > 100_000:
            raise ValueError
        identities = []
        for change in changes:
            if (type(change) is not dict or set(change) != {"layer", "id", "before", "after", "removed_paths"}
                    or change["layer"] not in {layer for layer, *_ in _LAYERS}
                    or type(change["id"]) is not str or not change["id"]):
                raise ValueError
            identities.append((change["layer"], change["id"]))
            candidate = retire_permission_document(change["before"], retired_flags=retired_flags)
            if (not candidate.removed_paths or candidate.document != change["after"]
                    or list(candidate.removed_paths) != change["removed_paths"]):
                raise ValueError
        if identities != sorted(set(identities)):
            raise ValueError
        receipt, expected = _evidence(checkpoint, retired_flags, changes, stored["details"]["review_markers"])
        if stored != expected:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("permission_retirement_cleanup_evidence_mismatch") from exc
    # A completed cleanup cannot recreate missing review-installation evidence.
    if (await connection.execute(select(_AUDIT.c.id).where(
            _AUDIT.c.id == _id(checkpoint.migration_id, "review-installation")))).first() is None:
        raise ValueError("permission_retirement_cleanup_evidence_mismatch")
    if await _install_reviews(connection, checkpoint, retired_flags=retired_flags) != receipt.review_markers:
        raise ValueError("permission_retirement_cleanup_evidence_mismatch")
    return receipt


async def _load_layers(connection: AsyncConnection) -> dict:
    loaded = {}
    count_total, size_total = 0, 0
    for layer, table, flags_key, other_fields in _LAYERS:
        fields = ("id", flags_key, "permission_migration_review", *other_fields)
        count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(sum(
            func.coalesce(func.length(cast(table.c[key], LargeBinary)), 0) for key in fields)), 0)))).one()
        count_total += count
        size_total += size
        if count_total > 100_000 or size_total > _MAX_BYTES:
            raise ValueError("permission_retirement_cleanup_limit")
        if (await connection.execute(select(table.c.id).where(
                func.length(cast(table.c.permission_migration_review, LargeBinary)) > 2048).limit(1))).first():
            raise ValueError("permission_retirement_review_marker_invalid")
        rows = (await connection.execute(select(table.c.id, table.c[flags_key], table.c.permission_migration_review,
            *(table.c[key] for key in other_fields)).order_by(table.c.id))).mappings().all()
        loaded[layer] = {row["id"]: dict(row) for row in rows}
    return loaded


async def retire_permission_documents(
    engine: AsyncEngine, checkpoint: PermissionRetirementCheckpoint, *, retired_flags: tuple[str, ...],
    expected_receipt: PermissionRetirementCleanup | None = None,
    checkpoint_run=None,
    verify_dependency=None,
) -> PermissionRetirementCleanup:
    """Prune once under a SQLite write fence; replay preserves later owner edits.

    Retain expected_receipt on resume, including a zero-change completion. An
    absent journal cannot distinguish that completion from a never-started run.
    No flags, review markers or audit writes survive a failed parity check.
    When composed into offline cutover, the permissions checkpoint commits in
    this same transaction. Existing standalone evidence cannot fill a missing
    coordinated checkpoint after the fact.
    """
    if engine.dialect.name != "sqlite":
        raise ValueError("permission_retirement_backend_unsupported")
    if ((checkpoint_run is not None and not callable(verify_dependency))
            or (checkpoint_run is None and verify_dependency is not None)):
        raise ValueError("permission_retirement_dependency_verifier_required")
    validate_permission_retirement_registry(retired_flags)
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            if checkpoint_run is not None:
                from .retirement_data_journal import ensure_retirement_data_journal
                await ensure_retirement_data_journal(connection)
            contexts = await read_permission_retirement_checkpoint(connection, checkpoint)
            previous = await _read_completion(connection, checkpoint, retired_flags)
            prefix = await _checkpoint_prefix(connection, checkpoint_run, checkpoint, previous)
            if verify_dependency is not None:
                await verify_dependency(connection)
            if expected_receipt is not None and expected_receipt != previous:
                raise ValueError("permission_retirement_cleanup_replay_mismatch")
            if previous is not None:
                await _record_checkpoint(connection, checkpoint_run, checkpoint, previous, prefix, replay=True)
                if verify_dependency is not None:
                    await verify_dependency(connection)
                await connection.commit()
                return previous
            source, current = await _capture_contexts(connection, max_contexts=10_000, max_bytes=_MAX_BYTES)
            current_receipt, _ = _rows(checkpoint.migration_id, source, current)
            if current_receipt != checkpoint:
                raise ValueError("permission_retirement_cleanup_source_changed")
            markers = await _install_reviews(connection, checkpoint, retired_flags=retired_flags)
            loaded = await _load_layers(connection)
            _require_loaded_parity(loaded, contexts, retired_flags=retired_flags)
            changes = []
            for layer, table, flags_key, _ in _LAYERS:
                for row in loaded[layer].values():
                    before = row[flags_key]
                    candidate = retire_permission_document(before, retired_flags=retired_flags)
                    if not candidate.removed_paths:
                        continue
                    changes.append({"layer": layer, "id": row["id"], "before": before,
                        "after": candidate.document, "removed_paths": list(candidate.removed_paths)})
                    await connection.execute(update(table).where(table.c.id == row["id"]).values(
                        {flags_key: candidate.document}))
                    row[flags_key] = candidate.document
            # Re-read persisted values; SQL triggers must not bypass the gate.
            persisted = await _load_layers(connection)
            if _digest(persisted) != _digest(loaded):
                raise ValueError("permission_retirement_cleanup_write_mismatch")
            _require_loaded_parity(persisted, contexts, retired_flags=retired_flags)
            receipt, evidence = _evidence(checkpoint, retired_flags,
                sorted(changes, key=lambda change: (change["layer"], change["id"])), markers)
            await connection.execute(insert(_AUDIT).values(**evidence, created_at=datetime.now(timezone.utc)))
            if await _read_completion(connection, checkpoint, retired_flags) != receipt:
                raise ValueError("permission_retirement_cleanup_evidence_mismatch")
            await _record_checkpoint(connection, checkpoint_run, checkpoint, receipt, prefix, replay=False)
            if verify_dependency is not None:
                await verify_dependency(connection)
            # An audit/checkpoint trigger runs after the earlier parity check.
            # Verify the actual stored layers again before this transaction commits.
            final = await _load_layers(connection)
            if _digest(final) != _digest(loaded):
                raise ValueError("permission_retirement_cleanup_write_mismatch")
            _require_loaded_parity(final, contexts, retired_flags=retired_flags)
            if await _read_completion(connection, checkpoint, retired_flags) != receipt:
                raise ValueError("permission_retirement_cleanup_evidence_mismatch")
            await connection.commit()
            return receipt
        except BaseException:
            await connection.rollback()
            raise


async def _checkpoint_prefix(connection, run, checkpoint, previous):
    if run is None:
        return ()
    from .retirement_data_journal import read_retirement_data_journal
    if run.migration_id != checkpoint.migration_id:
        raise ValueError("permission_retirement_checkpoint_scope_mismatch")
    records = await read_retirement_data_journal(connection, run)
    if len(records) not in {6, 7, 8}:
        raise ValueError("permission_retirement_materialization_incomplete")
    if ((len(records) >= 7) != (previous is not None)
            or len(records) >= 7 and records[6]["payload"] != asdict(previous)):
        raise ValueError("permission_retirement_checkpoint_replay_mismatch")
    return records


async def _record_checkpoint(connection, run, checkpoint, receipt, prefix, *, replay):
    if run is None:
        return
    from .retirement_data_journal import read_retirement_data_journal, record_retirement_stage
    await record_retirement_stage(connection, run, "permissions", receipt, replay=replay)
    records = await read_retirement_data_journal(connection, run)
    if (len(records) not in {7, 8} or records[:6] != prefix[:6]
            or replay and records != prefix):
        raise ValueError("permission_retirement_checkpoint_prefix_changed")
    await read_permission_retirement_checkpoint(connection, checkpoint)
