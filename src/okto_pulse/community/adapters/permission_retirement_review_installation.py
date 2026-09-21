"""Install captured review classifications without removing policy flags yet.

Internal cutover operation only. All surviving permissions must match the frozen
source before commit, including latent policy on inactive/unbound identities.
"""

from datetime import datetime, timezone

from sqlalchemy import LargeBinary, cast, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from okto_pulse.core.ports.permission_policy import PermissionPresetLineageNode, resolve_agent_permission_facts
from okto_pulse.core.ports.permission_retirement import (
    SOURCE_VERSION, capture_permission_migration_review,
    parse_permission_retirement_authority, require_permission_retirement_parity,
    validate_permission_retirement_registry,
)
from okto_pulse.community.adapters.permission_retirement_checkpoint import (
    PermissionRetirementCheckpoint, _capture_contexts, _digest, _id, _rows,
    read_permission_retirement_checkpoint,
)
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, PermissionIntroductionAudit, PermissionPreset

_AUDIT = PermissionIntroductionAudit.__table__
_PHASE = "permission_retirement_review_install"


async def install_permission_retirement_reviews(
    engine: AsyncEngine, checkpoint: PermissionRetirementCheckpoint, *, retired_flags: tuple[str, ...],
) -> int:
    """Install once; replay never re-applies review cleared by an owner edit.

    Keep the original checkpoint receipt in the migration coordinator. This
    operation does not infer permission from a hash mismatch or repair a broken
    checkpoint. Neither a flag tree nor source entity is deleted here.
    """
    if engine.dialect.name != "sqlite":
        raise ValueError("permission_retirement_backend_unsupported")
    validate_permission_retirement_registry(retired_flags)
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            result = await _install_reviews(connection, checkpoint, retired_flags=retired_flags)
            await connection.commit()
            return result
        except BaseException:
            await connection.rollback()
            raise


_LAYERS = (
    ("agent", Agent.__table__, "permission_flags", ("preset_id", "permissions", "created_by", "is_active")),
    ("preset", PermissionPreset.__table__, "flags", ("base_preset_id",)),
    ("board", AgentBoard.__table__, "permission_overrides", ("agent_id", "board_id")),
)


async def _write_migration_field(connection, table, identity, field, value):
    # This is a mechanical migration, not a new author edit. Preserve raw
    # onupdate cells (notably preset.updated_at); the migration audit has its
    # own timestamp. Column self-assignment avoids datetime re-serialization.
    values = {column.name: column for column in table.c if column.onupdate is not None}
    values[field] = value
    await connection.execute(update(table).where(table.c.id == identity).values(values))


async def _install_reviews(
    connection: AsyncConnection, checkpoint: PermissionRetirementCheckpoint, *, retired_flags: tuple[str, ...],
) -> int:
    """Caller owns the write fence and commits review with the whole cutover."""
    if not connection.in_transaction():
        raise ValueError("permission_retirement_transaction_required")
    from okto_pulse.community.adapters.relational_schema_steps import _ensure_permission_migration_reviews
    contexts = await read_permission_retirement_checkpoint(connection, checkpoint)
    identity = _id(checkpoint.migration_id, "review-installation")
    payload = {"format": "permission-retirement-review-installation/v1",
        "checkpoint_sha256": checkpoint.evidence_sha256, "source_sha256": checkpoint.source_sha256,
        "migration_id": checkpoint.migration_id, "retired_flags": sorted(retired_flags)}
    previous = (await connection.execute(select(_AUDIT).where(_AUDIT.c.id == identity))).mappings().one_or_none()
    missing = await _ensure_permission_migration_reviews(connection, create=False, require_tables=True)
    if missing and (previous is not None or (await connection.execute(select(_AUDIT.c.id).where(
            _AUDIT.c.phase.in_((_PHASE, "permission_retirement_cleanup"))).limit(1))).first() is not None):
        # Shared destinations cannot be reconstructed from a new migration's
        # checkpoint after any earlier review/cleanup evidence has survived.
        raise ValueError("permission_retirement_review_storage_missing")
    if previous is not None:
        payload["marker_count"] = previous["mutation_count"]
        if (previous["manifest_version"] != SOURCE_VERSION or previous["phase"] != _PHASE
                or previous["classification"] != "review_installation" or previous["details"] != payload
                or previous["before_digest"] != checkpoint.source_sha256
                or previous["after_digest"] != _digest(payload)
                or previous["subject_id"] is not None or previous["base_preset_id"] is not None
                or previous["introduced_true_count"] != 0 or previous["introduced_false_count"] != 0
                or previous["owner_review_required"] is not bool(previous["mutation_count"])
                or not 0 <= previous["mutation_count"] <= 100_000):
            raise ValueError("permission_retirement_review_installation_mismatch")
        return previous["mutation_count"]
    source, current_contexts = await _capture_contexts(connection, max_contexts=10_000, max_bytes=64 * 1024 * 1024)
    current_receipt, _ = _rows(checkpoint.migration_id, source, current_contexts)
    if current_receipt != checkpoint:
        raise ValueError("permission_retirement_review_source_changed")
    if missing:
        await _ensure_permission_migration_reviews(connection, require_tables=True)
    loaded, mutations = {}, 0
    for layer, table, flags_key, other_fields in _LAYERS:
        oversized = (await connection.execute(select(table.c.id).where(
            func.length(cast(table.c.permission_migration_review, LargeBinary)) > 2048).limit(1))).first()
        if oversized is not None:
            raise ValueError("permission_retirement_review_marker_invalid")
        rows = (await connection.execute(select(table.c.id, table.c[flags_key], table.c.permission_migration_review,
            *(table.c[key] for key in other_fields)).order_by(table.c.id))).mappings().all()
        loaded[layer] = {}
        for raw in rows:
            row = dict(raw)
            if row["permission_migration_review"] is not None:
                raise ValueError("permission_retirement_review_already_present")
            marker = capture_permission_migration_review(layer=layer, flags=row[flags_key],
                preset_id=row.get("preset_id"), source_sha256=checkpoint.source_sha256,
                checkpoint_sha256=checkpoint.evidence_sha256)
            if marker is not None:
                row["permission_migration_review"] = marker.document()
                await _write_migration_field(connection, table, row["id"], "permission_migration_review", marker.document())
                mutations += 1
            loaded[layer][row["id"]] = row
    _require_loaded_parity(loaded, contexts, retired_flags=retired_flags)
    payload["marker_count"] = mutations
    await connection.execute(insert(_AUDIT).values(id=identity, manifest_version=SOURCE_VERSION, phase=_PHASE,
        classification="review_installation", subject_id=None, base_preset_id=None,
        before_digest=checkpoint.source_sha256, after_digest=_digest(payload), details=payload,
        introduced_true_count=0, introduced_false_count=0, owner_review_required=bool(mutations),
        mutation_count=mutations, created_at=datetime.now(timezone.utc)))
    return mutations


def _require_loaded_parity(loaded: dict, contexts: tuple[dict, ...], *, retired_flags: tuple[str, ...]) -> None:
    presets = tuple(PermissionPresetLineageNode(row["id"], row["flags"], row["base_preset_id"],
        migration_review=row["permission_migration_review"]) for row in loaded["preset"].values())
    for context in contexts:
        agent = loaded["agent"][context["agent_id"]]
        binding = loaded["board"].get(context["binding_id"]) if context["binding_id"] is not None else None
        if (agent["created_by"] != context["created_by"] or agent["is_active"] is not context["is_active"]
                or agent["preset_id"] != context["preset_id"]
                or (context["binding_id"] is not None and (binding is None
                    or binding["agent_id"] != agent["id"] or binding["board_id"] != context["board_id"]))):
            raise ValueError("permission_retirement_review_identity_changed")
        candidate = resolve_agent_permission_facts(agent_flags=agent["permission_flags"],
            legacy_permissions=agent["permissions"], preset_id=agent["preset_id"], presets=presets,
            board_overrides=binding["permission_overrides"] if binding is not None else None,
            agent_migration_review=agent["permission_migration_review"],
            board_migration_review=binding["permission_migration_review"] if binding is not None else None)
        require_permission_retirement_parity(parse_permission_retirement_authority(context["authority"]),
            candidate, retired_flags=retired_flags)
