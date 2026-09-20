"""Internal pre-cutover evidence in the existing permission audit journal.

This is not bootstrap or a product endpoint. Capture before preset reconciliation
or flag cleanup; the future cutover must retain and verify the returned receipt.
Neither capture nor replay changes agents, presets, bindings or authority.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import uuid

from sqlalchemy import LargeBinary, cast, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.ports.historical_archive_authority import HistoricalArchivePresetFacts
from okto_pulse.core.ports.permission_retirement import (
    SOURCE_VERSION,
    capture_permission_retirement_authority,
    parse_permission_retirement_authority,
)
from okto_pulse.community.adapters.sprint_retirement_access import _bound_authority_sources
from okto_pulse.community.adapters.sqlalchemy_models import Agent, AgentBoard, Board, PermissionIntroductionAudit, PermissionPreset

_TABLE = PermissionIntroductionAudit.__table__
_PHASE = "permission_retirement_capture"
_FORMAT = "permission-retirement-checkpoint/v1"
_NAMESPACE = uuid.UUID("86154822-a39e-5cda-8ba4-02f369e2a196")
_MAX_CONTEXTS = 10_000
_MAX_BYTES = 64 * 1024 * 1024


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _id(migration_id: str, context: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, _encoded([migration_id, context]).decode("utf-8")))


@dataclass(frozen=True, slots=True)
class PermissionRetirementCheckpoint:
    migration_id: str
    source_sha256: str
    evidence_sha256: str
    context_count: int

    def __post_init__(self) -> None:
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 255
                or any(type(value) is not str or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)
                    for value in (self.source_sha256, self.evidence_sha256))
                or type(self.context_count) is not int or not 0 <= self.context_count <= _MAX_CONTEXTS):
            raise ValueError("permission_retirement_checkpoint_invalid")


async def _capture_contexts(connection: AsyncConnection, *, max_contexts: int, max_bytes: int) -> tuple[str, list[dict]]:
    source = await _bound_authority_sources(connection, 100_000, max_bytes)
    creator_bytes = (await connection.execute(select(func.coalesce(func.sum(
        func.length(cast(Agent.created_by, LargeBinary))), 0)))).scalar_one()
    if creator_bytes > max_bytes:
        raise ValueError("permission_retirement_checkpoint_limit")
    contexts, size = [], 0
    async with AsyncSession(bind=connection, join_transaction_mode="rollback_only", expire_on_commit=False) as session:
        preset_rows = (await session.execute(select(PermissionPreset.id, PermissionPreset.flags,
            PermissionPreset.base_preset_id).order_by(PermissionPreset.id))).all()
        presets = tuple(HistoricalArchivePresetFacts(row.id, row.flags, row.base_preset_id) for row in preset_rows)
        agents = (await session.execute(select(Agent.id, Agent.created_by, Agent.is_active,
            Agent.permission_flags, Agent.permissions, Agent.preset_id).order_by(Agent.id))).all()
        source = _digest([source, [[agent.id, agent.created_by] for agent in agents]])
        bindings = (await session.execute(select(AgentBoard.id, AgentBoard.agent_id, AgentBoard.board_id,
            AgentBoard.permission_overrides).order_by(AgentBoard.id))).all()
        by_agent = {}
        for binding in bindings:
            by_agent.setdefault(binding.agent_id, []).append(binding)
        if len(agents) + len(bindings) > max_contexts:
            raise ValueError("permission_retirement_checkpoint_limit")
        for agent in agents:
            for binding in (None, *by_agent.get(agent.id, ())):
                if binding is not None:
                    board = (await session.execute(select(Board.realm_id).where(Board.id == binding.board_id))).first()
                    if board is None or (board.realm_id or LOCAL_REALM_ID) != LOCAL_REALM_ID:
                        raise ValueError("permission_retirement_checkpoint_realm_invalid")
                authority = capture_permission_retirement_authority(agent_flags=agent.permission_flags,
                    legacy_permissions=agent.permissions, preset_id=agent.preset_id, presets=presets,
                    board_overrides=binding.permission_overrides if binding is not None else None)
                context = {"agent_id": agent.id, "created_by": agent.created_by, "is_active": agent.is_active,
                    "preset_id": agent.preset_id, "realm_id": LOCAL_REALM_ID,
                    "board_id": binding.board_id if binding is not None else None,
                    "binding_id": binding.id if binding is not None else None,
                    "authority": authority.document()}
                size += len(_encoded(context))
                if size > max_bytes:
                    raise ValueError("permission_retirement_checkpoint_limit")
                contexts.append(context)
    return source, contexts


def _rows(migration_id: str, source: str, contexts: list[dict]) -> tuple[PermissionRetirementCheckpoint, list[dict]]:
    rows = []
    for context in contexts:
        identifier = _id(migration_id, [context["agent_id"], context["binding_id"]])
        details = {"format": _FORMAT, "migration_id": migration_id, "source_sha256": source, "context": context}
        rows.append({"id": identifier, "manifest_version": SOURCE_VERSION, "phase": _PHASE,
            "classification": "board_authority" if context["binding_id"] is not None else "global_authority",
            "subject_id": context["agent_id"], "base_preset_id": context["preset_id"],
            "before_digest": source, "after_digest": _digest(details),
            "introduced_true_count": 0, "introduced_false_count": 0,
            "owner_review_required": context["authority"]["owner_review_required"],
            "mutation_count": 0, "details": details})
    manifest = {"format": _FORMAT, "migration_id": migration_id, "source_sha256": source,
        "contexts": [[row["id"], row["after_digest"]] for row in rows]}
    reference = PermissionRetirementCheckpoint(migration_id, source, _digest(manifest), len(contexts))
    rows.append({"id": _id(migration_id, None), "manifest_version": SOURCE_VERSION, "phase": _PHASE,
        "classification": "checkpoint_manifest", "subject_id": None, "base_preset_id": None,
        "before_digest": source, "after_digest": reference.evidence_sha256,
        "introduced_true_count": 0, "introduced_false_count": 0,
        "owner_review_required": any(row["owner_review_required"] for row in rows),
        "mutation_count": 0, "details": manifest})
    return reference, rows


async def _stored_rows(connection: AsyncConnection, migration_id: str) -> list[dict]:
    # The version/phase is separate from introduction reconciliation. JSON
    # filtering counts unexpected rows as drift instead of silently ignoring them.
    selection = (
        _TABLE.c.phase == _PHASE, _TABLE.c.manifest_version == SOURCE_VERSION,
        _TABLE.c.details["migration_id"].as_string() == migration_id)
    count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(
        func.length(cast(_TABLE.c.details, LargeBinary))), 0)).where(*selection))).one()
    if count > _MAX_CONTEXTS + 1 or size > _MAX_BYTES:
        raise ValueError("permission_retirement_checkpoint_limit")
    result = await connection.execute(select(_TABLE).where(*selection))
    return [{key: value for key, value in row.items() if key != "created_at"} for row in result.mappings()]


async def capture_permission_retirement_checkpoint(
    engine: AsyncEngine, *, migration_id: str, max_contexts: int = _MAX_CONTEXTS, max_bytes: int = _MAX_BYTES,
    expected_checkpoint: PermissionRetirementCheckpoint | None = None,
) -> PermissionRetirementCheckpoint:
    """Capture all agents, including unbound/inactive identities, under one fence.

    Replays compare exact evidence, never repair a partial or modified journal.
    Supply the retained receipt on resume to detect complete journal deletion.
    Source
    policy and audit writes share BEGIN IMMEDIATE and roll back together on error.
    A credential rotation is deliberately absent from the source fingerprint.
    """
    if engine.dialect.name != "sqlite":
        raise ValueError("permission_retirement_backend_unsupported")
    if (type(migration_id) is not str or not 1 <= len(migration_id) <= 255
            or type(max_contexts) is not int or not 0 <= max_contexts <= _MAX_CONTEXTS
            or type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES):
        raise ValueError("permission_retirement_checkpoint_limits_invalid")
    if expected_checkpoint is not None and expected_checkpoint.migration_id != migration_id:
        raise ValueError("permission_retirement_checkpoint_replay_mismatch")
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            source, contexts = await _capture_contexts(connection, max_contexts=max_contexts, max_bytes=max_bytes)
            reference, expected = _rows(migration_id, source, contexts)
            # SQLAlchemy's JSON serializer uses ASCII escapes and spaces. Bound
            # that representation too, rather than only the compact digest form.
            if sum(len(json.dumps(row, allow_nan=False).encode("utf-8")) for row in expected) > max_bytes:
                raise ValueError("permission_retirement_checkpoint_limit")
            stored = await _stored_rows(connection, migration_id)
            if expected_checkpoint is not None and (expected_checkpoint != reference or not stored):
                raise ValueError("permission_retirement_checkpoint_replay_mismatch")
            if stored:
                if sorted(stored, key=lambda row: row["id"]) != sorted(expected, key=lambda row: row["id"]):
                    raise ValueError("permission_retirement_checkpoint_replay_mismatch")
            else:
                now = datetime.now(timezone.utc)
                await connection.execute(insert(_TABLE), [{**row, "created_at": now} for row in expected])
            await connection.commit()
            return reference
        except BaseException:
            await connection.rollback()
            raise


async def read_permission_retirement_checkpoint(
    connection: AsyncConnection, reference: PermissionRetirementCheckpoint,
) -> tuple[dict, ...]:
    """Verify against the caller's pre-cutover receipt, independent of live flags.

    The owning coordinator must provide its transaction and retain the receipt
    outside the candidate mutation. This journal is evidence, not a runtime
    review override; gateways must not infer authority from its mere presence.
    """
    if not connection.in_transaction():
        raise ValueError("permission_retirement_checkpoint_transaction_required")
    rows = await _stored_rows(connection, reference.migration_id)
    manifest = next((row for row in rows if row["id"] == _id(reference.migration_id, None)), None)
    if manifest is None or len(rows) != reference.context_count + 1:
        raise ValueError("permission_retirement_checkpoint_evidence_mismatch")
    try:
        entries = manifest["details"]["contexts"]
        by_id = {row["id"]: row for row in rows if row is not manifest}
        if len(entries) != reference.context_count or len({entry[0] for entry in entries}) != reference.context_count:
            raise ValueError
        contexts = [by_id[entry[0]]["details"]["context"] for entry in entries]
        for context in contexts:
            parse_permission_retirement_authority(context["authority"])
        expected_reference, expected = _rows(reference.migration_id, reference.source_sha256, contexts)
        if expected_reference != reference or sorted(rows, key=lambda row: row["id"]) != sorted(expected, key=lambda row: row["id"]):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError("permission_retirement_checkpoint_evidence_mismatch") from exc
    return tuple(contexts)
