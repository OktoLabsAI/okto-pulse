"""Community SQLAlchemy persistence for selective Knowledge propagation v2.

The Core service builds a complete immutable mutation plan.  This adapter
stages the scope CAS, temporal closures, new records, and canonical ledger row
in the caller-owned transaction.  It deliberately never commits or rolls back
that transaction.

Rejected-request attempts use :meth:`append_after_rollback`, which owns one
short independent session.  The application boundary must call it only after
the failed domain unit of work has been rolled back and closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    Ideation,
    IdeationKnowledgeBase,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    Refinement,
    RefinementKnowledgeBase,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.core.domain.knowledge_fingerprint import (
    knowledge_content_bytes,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeAssignment,
    KnowledgeAssignmentState,
    KnowledgeSelectionState,
    KnowledgePropagationMode,
    KnowledgeRelevanceLink,
    KnowledgeTargetType,
)
from okto_pulse.core.domain.resource_revision import ResourceRevisionStamp
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeIdempotencyLookup,
    KnowledgeLocalAttachment,
    KnowledgeMutationAttempt,
    KnowledgeMutationKind,
    KnowledgeMutationLedgerEntry,
    KnowledgeMutationOutcome,
    KnowledgeMutationPlan,
    KnowledgeMutationReceipt,
    KnowledgeParentEvidence,
    KnowledgeParentKey,
    KnowledgeParentLookup,
    KnowledgeParentType,
    KnowledgePropagationPortError,
    KnowledgePropagationScope,
    KnowledgePropagationSnapshot,
    KnowledgePropagationTombstone,
    KnowledgeRecordKind,
    KnowledgeScopeLookup,
    KnowledgeSelectableSource,
    KnowledgeTargetKey,
    KnowledgeTemporalWindow,
    TemporalKnowledgeAssignment,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgePropagationService,
)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive DateTime round trip to UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _copy_mapping(value: object | None) -> dict[str, object]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    return {}


def _copy_sequence(value: object | None) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return copy.deepcopy(list(value))
    return []


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value)).strip()


def _structured_ids(value: object | None) -> tuple[str, ...]:
    """Project canonical ids from a structured Spec JSON collection.

    Historical specs can contain partially-authored rows. Those rows are not
    valid evidence for a relevance link, but they also must not make unrelated
    valid ids unreadable. The Core preflight rejects any requested link absent
    from this conservative projection.
    """

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    identities: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identity = item.get("id")
        if isinstance(identity, str) and identity.strip():
            identities.add(identity.strip())
    return tuple(sorted(identities))


def _target_predicates(
    model: type[KnowledgePropagationScopeRecord] | type[KnowledgeMutationLedgerRecord],
    target: KnowledgeTargetKey,
) -> tuple[object, ...]:
    return (
        model.board_id == target.board_id,
        model.target_type == _enum_value(target.target_type),
        model.target_id == target.target_id,
    )


def _has_sqlite_busy_snapshot(exc: BaseException) -> bool:
    """Recognize SQLITE_BUSY_SNAPSHOT (517) without textual lock heuristics."""

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if getattr(current, "sqlite_errorcode", None) == 517:
            return True
        for candidate in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(candidate, BaseException):
                pending.append(candidate)
    return False


def is_knowledge_creation_race_error(
    exc: BaseException,
    *,
    target_type: KnowledgeTargetType | str | None = None,
    target_id: str | None = None,
) -> bool:
    """Recognize only deterministic-target/first-scope concurrency failures.

    Creation routes call ``uow.synchronize()`` before the propagation adapter
    stages its plan. They use this same narrow classifier for raw SQLAlchemy
    errors, avoiding a second drift-prone list of backend markers.
    """

    normalized_target_type = (
        None
        if target_type is None
        else str(getattr(target_type, "value", target_type)).strip()
    )
    if (
        normalized_target_type
        in {
            KnowledgeTargetType.CARD.value,
            KnowledgeTargetType.SPEC.value,
        }
        and bool(target_id)
        and _has_sqlite_busy_snapshot(exc)
    ):
        return True

    message = str(exc).lower()
    common_collision_markers = (
        "uq_knowledge_propagation_scope_target",
        "unique constraint failed: knowledge_propagation_scopes.board_id, "
        "knowledge_propagation_scopes.target_type, "
        "knowledge_propagation_scopes.target_id",
        "uq_knowledge_mutation_ledger_target_key",
        "unique constraint failed: knowledge_mutation_ledger.board_id, "
        "knowledge_mutation_ledger.target_type, "
        "knowledge_mutation_ledger.target_id, "
        "knowledge_mutation_ledger.idempotency_key",
    )
    target_collision_markers: tuple[str, ...]
    if normalized_target_type == KnowledgeTargetType.CARD.value:
        target_collision_markers = (
            "unique constraint failed: cards.id",
            "cards_pkey",
        )
    elif normalized_target_type == KnowledgeTargetType.SPEC.value:
        target_collision_markers = (
            "unique constraint failed: specs.id",
            "specs_pkey",
        )
    else:
        target_collision_markers = (
            "unique constraint failed: cards.id",
            "unique constraint failed: specs.id",
            "cards_pkey",
            "specs_pkey",
        )
    matched = any(
        marker in message
        for marker in (*common_collision_markers, *target_collision_markers)
    )
    if (
        matched
        and target_id is not None
        and ("[parameters:" in message or "key (" in message)
    ):
        return target_id.lower() in message
    return matched


def _plan_has_creation_receipt(plan: KnowledgeMutationPlan) -> bool:
    ledger = plan.ledger_entry
    if ledger is None:
        return False
    result_v2 = ledger.receipt.details.get("result_v2")
    if not isinstance(result_v2, Mapping):
        return False
    creation_result = result_v2.get("creation_result")
    return isinstance(creation_result, Mapping) and bool(creation_result)


def _is_first_creation_race(
    exc: SQLAlchemyError,
    plan: KnowledgeMutationPlan,
) -> bool:
    return (
        plan.expected_revision == 0
        and _plan_has_creation_receipt(plan)
        and is_knowledge_creation_race_error(
            exc,
            target_type=cast(KnowledgeTargetType, plan.target.target_type),
            target_id=plan.target.target_id,
        )
    )


def _revision_stamp(
    *,
    root_id: object,
    immediate_parent_id: object | None,
    source_revision: object | None,
    source_content_sha256: object | None,
) -> ResourceRevisionStamp:
    return ResourceRevisionStamp(
        root_id=str(root_id),
        immediate_parent_id=(
            None if immediate_parent_id in (None, "") else str(immediate_parent_id)
        ),
        source_revision=(
            None if source_revision in (None, "") else str(source_revision)
        ),
        source_content_sha256=(
            None if source_content_sha256 in (None, "") else str(source_content_sha256)
        ),
    )


def _temporal(row: object) -> KnowledgeTemporalWindow:
    effective_to = getattr(row, "effective_to", None)
    return KnowledgeTemporalWindow(
        effective_from=_as_utc(getattr(row, "effective_from")),
        effective_to=None if effective_to is None else _as_utc(effective_to),
        superseded_by_id=getattr(row, "superseded_by_id", None),
    )


def _assignment_from_row(
    row: KnowledgeAssignmentRecord,
    target: KnowledgeTargetKey,
) -> TemporalKnowledgeAssignment:
    links: list[KnowledgeRelevanceLink] = []
    for raw in _copy_sequence(row.relevance_links):
        if not isinstance(raw, Mapping):
            raise ValueError("knowledge_assignment_relevance_link_corrupt")
        links.append(
            KnowledgeRelevanceLink(
                entity_type=raw.get("entity_type"),
                entity_id=cast(str, raw.get("entity_id")),
            )
        )
    return TemporalKnowledgeAssignment(
        assignment=KnowledgeAssignment(
            assignment_id=str(row.assignment_id),
            board_id=target.board_id,
            target_type=target.target_type,
            target_id=target.target_id,
            source_knowledge_id=str(row.source_knowledge_id),
            revision_stamp=_revision_stamp(
                root_id=row.root_id,
                immediate_parent_id=row.immediate_parent_id,
                source_revision=row.source_revision,
                source_content_sha256=row.source_content_sha256,
            ),
            mode=str(row.mode),
            state=str(row.state),
            origin_class=str(row.origin_class),
            actor_id=str(row.actor_id),
            revision=int(row.revision),
            justification=row.justification,
            relevance_links=tuple(links),
        ),
        temporal=_temporal(row),
    )


def _snapshot_from_row(row: KnowledgeSnapshotRecord) -> KnowledgePropagationSnapshot:
    return KnowledgePropagationSnapshot(
        snapshot_id=str(row.snapshot_id),
        assignment_id=str(row.assignment_id),
        revision_stamp=_revision_stamp(
            root_id=row.root_id,
            immediate_parent_id=row.immediate_parent_id,
            source_revision=row.source_revision,
            source_content_sha256=row.source_content_sha256,
        ),
        content_bytes=bytes(row.content_bytes),
        temporal=_temporal(row),
        governance_metadata=copy.deepcopy(getattr(row, "governance_metadata", None)),
    )


def _tombstone_from_row(
    row: KnowledgeTombstoneRecord,
    target: KnowledgeTargetKey,
) -> KnowledgePropagationTombstone:
    return KnowledgePropagationTombstone(
        tombstone_id=str(row.tombstone_id),
        target=target,
        root_id=None if row.root_id is None else str(row.root_id),
        actor_id=str(row.actor_id),
        justification=str(row.justification),
        temporal=_temporal(row),
    )


def _receipt_from_ledger(
    row: KnowledgeMutationLedgerRecord,
) -> KnowledgeMutationReceipt:
    target = KnowledgeTargetKey(
        board_id=str(row.board_id),
        target_type=str(row.target_type),
        target_id=str(row.target_id),
    )
    return KnowledgeMutationReceipt(
        operation_id=str(row.operation_id),
        target=target,
        operation_kind=str(row.operation_kind),
        previous_revision=int(row.previous_revision),
        revision=int(row.revision),
        request_hash=str(row.request_hash),
        applied_at=_as_utc(row.applied_at),
        outcome=str(row.outcome),
        reason_code=row.reason_code,
        reason_detail=row.reason_detail,
        details=_copy_mapping(row.details),
    )


def _ledger_from_row(
    row: KnowledgeMutationLedgerRecord,
) -> KnowledgeMutationLedgerEntry:
    receipt = _receipt_from_ledger(row)
    return KnowledgeMutationLedgerEntry(
        target=receipt.target,
        idempotency_key=str(row.idempotency_key),
        request_hash=str(row.request_hash),
        operation_kind=str(row.operation_kind),
        receipt=receipt,
        recorded_at=_as_utc(row.recorded_at),
        actor_id=str(row.actor_id),
    )


def _attempt_from_row(
    row: KnowledgeMutationAttemptRecord,
) -> KnowledgeMutationAttempt:
    return KnowledgeMutationAttempt(
        attempt_id=str(row.attempt_id),
        target=KnowledgeTargetKey(
            board_id=str(row.board_id),
            target_type=str(row.target_type),
            target_id=str(row.target_id),
        ),
        idempotency_key=str(row.idempotency_key),
        request_hash=str(row.request_hash),
        operation_kind=str(row.operation_kind),
        actor_id=str(row.actor_id),
        outcome=str(row.outcome),
        recorded_at=_as_utc(row.recorded_at),
        original_operation_id=row.original_operation_id,
        reason_code=row.reason_code,
        reason_detail=row.reason_detail,
        details=_copy_mapping(row.details),
    )


def _kb_value(item: object, field_name: str) -> object | None:
    if isinstance(item, Mapping):
        return item.get(field_name)
    return getattr(item, field_name, None)


def _first_kb_value(item: object, *field_names: str) -> object | None:
    for field_name in field_names:
        value = _kb_value(item, field_name)
        if value not in (None, ""):
            return value
    return None


def _canonical_kb_payload(
    item: object, *, fallback_id: str | None = None
) -> dict[str, object]:
    return {
        "id": _kb_value(item, "id") or fallback_id,
        "title": _kb_value(item, "title"),
        "description": _kb_value(item, "description"),
        "content": _kb_value(item, "content"),
        "mime_type": _kb_value(item, "mime_type") or "text/markdown",
    }


def _kb_identity(item: object) -> str:
    explicit = _kb_value(item, "id")
    if not isinstance(explicit, str) or not explicit.strip():
        raise ValueError("knowledge_propagation_source_identity_required")
    return explicit


def _kb_content(item: object) -> tuple[bytes, str]:
    identity = _kb_identity(item)
    payload = _canonical_kb_payload(item, fallback_id=identity)
    content_bytes = knowledge_content_bytes(payload)
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    return content_bytes, content_sha256


def _knowledge_source_stamp(item: object) -> ResourceRevisionStamp:
    """Read the current source identity and its explicitly stored lineage."""

    identity = _kb_identity(item)
    source_revision = _first_kb_value(
        item,
        "source_revision",
        "source_version",
    )
    source_hash = _first_kb_value(
        item,
        "source_content_sha256",
        "content_hash",
    )
    root_id = (
        _first_kb_value(
            item,
            "root_source_kb_id",
        )
        or identity
    )
    immediate_parent_id = _first_kb_value(
        item,
        "immediate_parent_kb_id",
        "source_kb_id",
    )
    return ResourceRevisionStamp(
        root_id=str(root_id),
        immediate_parent_id=(
            None if immediate_parent_id in (None, "") else str(immediate_parent_id)
        ),
        source_revision=(
            None if source_revision in (None, "") else str(source_revision)
        ),
        source_content_sha256=(None if source_hash in (None, "") else str(source_hash)),
    )


def _selectable_kb_stamp(
    item: object,
    *,
    content_sha256: str,
) -> ResourceRevisionStamp:
    """Return complete revision evidence for a new v2 assignment.

    A live source without an explicit revision uses its canonical digest as a deterministic
    revision identity.
    """

    source = _knowledge_source_stamp(item)
    return ResourceRevisionStamp(
        root_id=source.root_id,
        immediate_parent_id=source.immediate_parent_id,
        source_revision=source.source_revision or content_sha256,
        source_content_sha256=content_sha256,
    )






def _local_attachment(
    item: object,
    *,
    attached_at: datetime,
) -> KnowledgeLocalAttachment:
    content_bytes, content_sha256 = _kb_content(item)
    return KnowledgeLocalAttachment(
        source_knowledge_id=_kb_identity(item),
        revision_stamp=_selectable_kb_stamp(
            item,
            content_sha256=content_sha256,
        ),
        attached_at=_as_utc(attached_at),
        content_bytes=content_bytes,
        governance_metadata=copy.deepcopy(_kb_value(item, "governance_metadata")),
    )


def _is_direct_target_local_attachment(item: object) -> bool:
    """Distinguish direct v2 authorship from a forbidden physical copy.

    Timestamp evidence proves only *when* a row appeared. Legacy propagation
    bugs could still create a copied row after v2 activation, so any durable
    upstream/copy provenance keeps that row in history. A direct row may carry
    an explicit self-root because some writers normalize ``root=id``.
    """

    identity = _kb_identity(item)
    root_id = _first_kb_value(item, "root_source_kb_id")
    if root_id not in (None, "", identity):
        return False
    if any(
        _first_kb_value(item, field_name) not in (None, "")
        for field_name in (
            "source_kb_id",
            "immediate_parent_kb_id",
            "source_type",
            "source_id",
            "source_title",
        )
    ):
        return False
    for field_name in ("source", "source_ref", "origin_ref"):
        value = _kb_value(item, field_name)
        if value in (None, ""):
            continue
        normalized = str(value).strip().lower()
        if normalized.startswith(("copied_from_", "copied-from-")):
            return False
    description = str(_kb_value(item, "description") or "").strip().lower()
    return not description.startswith("[propagated from parent]")






def _selectable_source(
    requested_id: str,
    item: object,
) -> KnowledgeSelectableSource:
    content_bytes, content_sha256 = _kb_content(item)
    stamp = _selectable_kb_stamp(item, content_sha256=content_sha256)
    return KnowledgeSelectableSource(
        requested_knowledge_id=requested_id,
        source_knowledge_id=_kb_identity(item),
        revision_stamp=stamp,
        content_bytes=content_bytes,
        source_deleted=False,
        governance_metadata=copy.deepcopy(_kb_value(item, "governance_metadata")),
    )


def _current_assignment_bindings(
    assignments: Sequence[TemporalKnowledgeAssignment],
) -> dict[str, TemporalKnowledgeAssignment]:
    """Return the one durable current assignment for each lineage root.

    The physical source row is not the authority for an already-open v2
    assignment.  Its immutable assignment row is.  Keep that evidence intact
    so a selective DROP can still identify its exact root after physical
    source deletion.  Corrupt duplicate roots fail closed instead of choosing
    one assignment by iteration order.
    """

    by_root: dict[str, TemporalKnowledgeAssignment] = {}
    for item in assignments:
        if not item.temporal.is_current:
            continue
        root_id = item.assignment.revision_stamp.root_id
        prior = by_root.get(root_id)
        if prior is not None and prior != item:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_current_binding_ambiguous",
                "multiple current assignments claim the same Knowledge root",
                details={
                    "root_id": root_id,
                    "assignment_ids": sorted(
                        (
                            prior.assignment.assignment_id,
                            item.assignment.assignment_id,
                        )
                    ),
                },
            )
        by_root[root_id] = item
    return by_root


def _bound_assignment(
    requested_id: str,
    bindings: Mapping[str, TemporalKnowledgeAssignment],
) -> TemporalKnowledgeAssignment | None:
    """Resolve a durable root/source token uniquely, or fail closed."""

    matches = tuple(
        item
        for root_id, item in bindings.items()
        if requested_id
        in {
            root_id,
            item.assignment.source_knowledge_id,
        }
    )
    if len(matches) > 1:
        raise KnowledgePropagationPortError(
            "knowledge_propagation_current_binding_ambiguous",
            "a Knowledge token resolves to multiple current assignments",
            details={
                "requested_knowledge_id": requested_id,
                "assignment_ids": sorted(
                    item.assignment.assignment_id for item in matches
                ),
                "root_ids": sorted(
                    item.assignment.revision_stamp.root_id for item in matches
                ),
            },
        )
    return None if not matches else matches[0]


def _deleted_selectable_source(
    requested_id: str,
    assignment: TemporalKnowledgeAssignment,
) -> KnowledgeSelectableSource:
    """Project immutable assignment evidence without inventing source bytes."""

    durable = assignment.assignment
    return KnowledgeSelectableSource(
        requested_knowledge_id=requested_id,
        source_knowledge_id=durable.source_knowledge_id,
        revision_stamp=durable.revision_stamp,
        content_bytes=None,
        source_deleted=True,
    )


def _current_physical_source(
    candidates: Sequence[object],
    *,
    root_id: str,
    known_parent_root_by_id: Mapping[str, str] | None = None,
) -> object | None:
    """Resolve the sole current leaf of one complete linear root, fail-closed.

    A copied parent row can remain physically present after a newer revision
    is materialized.  Merely finding one leaf is insufficient: a disconnected
    cycle could otherwise coexist with that leaf and be silently ignored.
    Every candidate must therefore belong to the requested root and participate
    in one connected, acyclic chain.

    The oldest row in the table can legitimately point outside this physical
    table when it was copied from the preceding entity level.  That one anchor
    is accepted only when it points at the canonical root itself or carries the
    durable ``source_type``/``source_id`` copy evidence written by propagation.
    Any other missing parent is a dangling/cross-root reference and fails
    closed.
    """

    if not candidates:
        return None

    known_parent_roots = dict(known_parent_root_by_id or {})
    by_id: dict[str, object] = {}
    parent_by_id: dict[str, str | None] = {}
    malformed_reasons: dict[str, str] = {}
    for item in candidates:
        identity = _kb_identity(item)
        if identity in by_id:
            malformed_reasons[identity] = "duplicate_identity"
            continue
        by_id[identity] = item

        immediate_parent = _kb_value(item, "immediate_parent_kb_id")
        legacy_parent = _kb_value(item, "source_kb_id")
        if (
            immediate_parent not in (None, "")
            and legacy_parent not in (None, "")
            and str(immediate_parent) != str(legacy_parent)
        ):
            malformed_reasons[identity] = "parent_alias_conflict"
        parent = (
            immediate_parent if immediate_parent not in (None, "") else legacy_parent
        )
        parent_by_id[identity] = None if parent in (None, "") else str(parent)

        if _knowledge_source_stamp(item).root_id != root_id:
            malformed_reasons[identity] = "root_mismatch"

    candidate_ids = set(by_id)
    children_by_id: dict[str, list[str]] = {identity: [] for identity in candidate_ids}
    anchors: list[str] = []
    dangling_parent_ids: dict[str, str] = {}
    for identity, item in by_id.items():
        parent_id = parent_by_id[identity]
        if parent_id is None:
            anchors.append(identity)
            continue
        if parent_id in candidate_ids:
            children_by_id[parent_id].append(identity)
            continue
        if parent_id in known_parent_roots:
            malformed_reasons[identity] = "cross_root_parent"
            continue

        propagated_anchor = _first_kb_value(item, "source_type") not in (
            None,
            "",
        ) and _first_kb_value(item, "source_id") not in (None, "")
        if parent_id == root_id or propagated_anchor:
            anchors.append(identity)
        else:
            dangling_parent_ids[identity] = parent_id

    branch_parent_ids = sorted(
        identity for identity, children in children_by_id.items() if len(children) > 1
    )
    leaves = tuple(
        by_id[identity] for identity, children in children_by_id.items() if not children
    )

    visited: set[str] = set()
    terminal_anchor: str | None = None
    if len(leaves) == 1:
        cursor = _kb_identity(leaves[0])
        while cursor not in visited:
            visited.add(cursor)
            parent_id = parent_by_id[cursor]
            if parent_id not in candidate_ids:
                terminal_anchor = cursor
                break
            cursor = cast(str, parent_id)

    complete_linear_chain = (
        not malformed_reasons
        and not dangling_parent_ids
        and not branch_parent_ids
        and len(anchors) == 1
        and len(leaves) == 1
        and terminal_anchor == anchors[0]
        and visited == candidate_ids
    )
    if not complete_linear_chain:
        raise KnowledgePropagationPortError(
            "knowledge_propagation_source_revision_ambiguous",
            "the source root is not one complete linear physical revision chain",
            details={
                "root_id": root_id,
                "candidate_ids": sorted(candidate_ids),
                "leaf_ids": sorted(_kb_identity(item) for item in leaves),
                "anchor_ids": sorted(anchors),
                "visited_ids": sorted(visited),
                "branch_parent_ids": branch_parent_ids,
                "dangling_parent_ids": dict(sorted(dangling_parent_ids.items())),
                "malformed_reasons": dict(sorted(malformed_reasons.items())),
                "known_parent_roots": dict(sorted(known_parent_roots.items())),
            },
        )
    return leaves[0]


class CommunitySqlAlchemyKnowledgePropagationStore:
    """SQLAlchemy implementation of both propagation and rejected-audit ports."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        if not callable(session_factory):
            raise TypeError("knowledge_propagation_session_factory_invalid")
        self._session_factory = session_factory

    @staticmethod
    async def _lock_rows(
        context: Any,
        model: Any,
        *predicates: object,
    ) -> int:
        """Acquire a write fence without mutating timestamps/defaults."""

        fence_values = {
            column.key: getattr(model, column.key)
            for column in model.__table__.columns
            if column.primary_key or column.onupdate is not None
        }
        result = await context.execute(
            update(model)
            .where(*predicates)
            .values(**fence_values)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    async def _lock_target(
        self,
        context: Any,
        plan: KnowledgeMutationPlan,
    ) -> Spec | Card:
        target = plan.target
        model: type[Spec] | type[Card]
        predicates: list[object]
        if target.target_type is KnowledgeTargetType.SPEC:
            model = Spec
            predicates = [
                Spec.id == target.target_id,
                Spec.board_id == target.board_id,
            ]
            if plan.parent is not None:
                if plan.parent.parent_type is KnowledgeParentType.REFINEMENT:
                    predicates.append(Spec.refinement_id == plan.parent.parent_id)
                elif plan.parent.parent_type is KnowledgeParentType.IDEATION:
                    predicates.append(Spec.ideation_id == plan.parent.parent_id)
                else:
                    raise KnowledgePropagationPortError(
                        "knowledge_propagation_parent_changed",
                        "the Spec target has an invalid physical parent type",
                        details=plan.parent.to_dict(),
                    )
        else:
            model = Card
            predicates = [
                Card.id == target.target_id,
                Card.board_id == target.board_id,
            ]
            if plan.parent is not None:
                predicates.append(Card.spec_id == plan.parent.parent_id)

        matched = await self._lock_rows(context, model, *predicates)
        if matched != 1:
            actual_parent: KnowledgeParentKey | None = None
            if plan.parent is not None:
                try:
                    actual_row = await self._load_target(context, target)
                except KnowledgePropagationPortError:
                    actual_row = None
                if actual_row is not None:
                    actual_parent = self._target_parent_key(
                        target,
                        actual_row,
                    )
            code = (
                "knowledge_propagation_parent_changed"
                if plan.parent is not None
                else "knowledge_propagation_target_not_found"
            )
            raise KnowledgePropagationPortError(
                code,
                (
                    "the target parent changed after propagation preflight"
                    if plan.parent is not None
                    else "knowledge propagation target no longer exists"
                ),
                details=(
                    target.to_dict()
                    if plan.parent is None
                    else {
                        "target": target.to_dict(),
                        "expected_parent": plan.parent.to_dict(),
                        "actual_parent": (
                            None if actual_parent is None else actual_parent.to_dict()
                        ),
                    }
                ),
            )
        return await self._load_target(context, target)

    @staticmethod
    def _parent_model(
        parent: KnowledgeParentKey,
    ) -> type[Ideation] | type[Refinement] | type[Spec]:
        if parent.parent_type is KnowledgeParentType.IDEATION:
            return Ideation
        if parent.parent_type is KnowledgeParentType.REFINEMENT:
            return Refinement
        return Spec

    async def _lock_parent(
        self,
        context: Any,
        parent: KnowledgeParentKey,
    ) -> None:
        model = self._parent_model(parent)
        matched = await self._lock_rows(
            context,
            model,
            model.id == parent.parent_id,
            model.board_id == parent.board_id,
        )
        if matched != 1:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_parent_not_eligible",
                "the target parent no longer exists in the target board",
                details=parent.to_dict(),
            )

    async def _lock_parent_sources(
        self,
        context: Any,
        evidence: KnowledgeParentEvidence,
    ) -> None:
        source_ids = tuple(
            sorted({source.source_knowledge_id for source in evidence.sources})
        )
        if not source_ids:
            return
        if evidence.parent.parent_type is KnowledgeParentType.SPEC:
            # _fence_parent_evidence already write-locks the Spec target. Every
            # Spec propagation mutation locks that same row before its scope
            # CAS, so a fresh effective read below is the authoritative fence.
            return
        await self._lock_source_ids(
            context,
            parent=evidence.parent,
            source_ids=source_ids,
        )

    async def _lock_source_ids(
        self,
        context: Any,
        *,
        parent: KnowledgeParentKey,
        source_ids: tuple[str, ...],
    ) -> None:
        if not source_ids:
            return
        if parent.parent_type is KnowledgeParentType.IDEATION:
            model = IdeationKnowledgeBase
            owner_column = IdeationKnowledgeBase.ideation_id
        elif parent.parent_type is KnowledgeParentType.REFINEMENT:
            model = RefinementKnowledgeBase
            owner_column = RefinementKnowledgeBase.refinement_id
        else:
            model = SpecKnowledgeBase
            owner_column = SpecKnowledgeBase.spec_id
        matched = await self._lock_rows(
            context,
            model,
            owner_column == parent.parent_id,
            model.id.in_(source_ids),
        )
        if matched != len(source_ids):
            raise KnowledgePropagationPortError(
                "knowledge_propagation_preflight_stale",
                "parent Knowledge evidence changed after creation preflight",
                details=parent.to_dict(),
            )

    async def _fence_planned_sources(
        self,
        context: Any,
        *,
        plan: KnowledgeMutationPlan,
        parent: KnowledgeParentKey | None,
    ) -> None:
        if parent is None or plan.parent_evidence is not None:
            return
        expected = {
            item.assignment.source_knowledge_id: (
                item.assignment.revision_stamp.to_dict()
            )
            for item in plan.assignments_to_open
        }
        if not expected:
            return
        source_ids = tuple(sorted(expected))
        if plan.operation_kind is KnowledgeMutationKind.DROP_DELTA:
            # DROP is the one mutation that remains valid after physical
            # deletion.  Reconstruct from the target's freshly loaded scope:
            # a missing source can appear here only through an exact current
            # assignment binding, while a never-assigned arbitrary id remains
            # absent.  Comparing the immutable stamp also prevents a stale
            # preflight from changing the deletion fingerprint.
            fresh_scope = await self.load_scope(
                context,
                KnowledgeScopeLookup(
                    target=plan.target,
                    source_knowledge_ids=source_ids,
                ),
            )
            verified: dict[str, dict[str, str | None]] = {}
            for source_id, expected_stamp in expected.items():
                matches = tuple(
                    source
                    for source in fresh_scope.sources
                    if source.source_knowledge_id == source_id
                    and source.revision_stamp.to_dict() == expected_stamp
                )
                if not matches:
                    raise KnowledgePropagationPortError(
                        "knowledge_propagation_preflight_stale",
                        "selected Knowledge changed before the DROP write fence",
                        details={
                            "parent": parent.to_dict(),
                            "source_knowledge_id": source_id,
                            "expected_stamp": expected_stamp,
                        },
                    )
                verified[source_id] = expected_stamp
            if verified != expected:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_preflight_stale",
                    "selected Knowledge changed before the DROP write fence",
                    details={
                        "parent": parent.to_dict(),
                        "expected_sources": expected,
                        "actual_sources": verified,
                    },
                )
            return
        if parent.parent_type is not KnowledgeParentType.SPEC:
            await self._lock_source_ids(
                context,
                parent=parent,
                source_ids=source_ids,
            )
        fresh = await self.load_parent_evidence(
            context,
            KnowledgeParentLookup(
                parent=parent,
                source_knowledge_ids=source_ids,
            ),
        )
        actual = {
            source.source_knowledge_id: source.revision_stamp.to_dict()
            for source in fresh.sources
        }
        if actual != expected:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_preflight_stale",
                "selected Knowledge changed before the mutation write fence",
                details={
                    "parent": parent.to_dict(),
                    "expected_sources": expected,
                    "actual_sources": actual,
                },
            )

    async def _fence_parent_evidence(
        self,
        context: Any,
        plan: KnowledgeMutationPlan,
        physical_parent: KnowledgeParentKey | None,
    ) -> None:
        parent = plan.parent or physical_parent
        if parent is None:
            return
        await self._lock_parent(context, parent)
        expected = plan.parent_evidence
        if expected is None:
            return
        await self._lock_parent_sources(context, expected)
        requested_ids = tuple(
            source.requested_knowledge_id for source in expected.sources
        )
        fresh = await self.load_parent_evidence(
            context,
            KnowledgeParentLookup(
                parent=parent,
                source_knowledge_ids=requested_ids,
            ),
        )
        if fresh.to_dict() != expected.to_dict():
            raise KnowledgePropagationPortError(
                "knowledge_propagation_preflight_stale",
                "parent evidence changed after creation preflight",
                details={
                    "parent": parent.to_dict(),
                    "expected_evidence": expected.to_dict(),
                    "actual_evidence": fresh.to_dict(),
                },
            )

    async def _load_target(
        self,
        context: Any,
        target: KnowledgeTargetKey,
    ) -> Spec | Card:
        if target.target_type is KnowledgeTargetType.SPEC:
            row = (
                await context.execute(
                    select(Spec)
                    .where(
                        Spec.id == target.target_id,
                        Spec.board_id == target.board_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        else:
            row = (
                await context.execute(
                    select(Card)
                    .where(
                        Card.id == target.target_id,
                        Card.board_id == target.board_id,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if row is None:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_target_not_found",
                "knowledge propagation target does not exist in the requested board",
                details=target.to_dict(),
            )
        return row

    @staticmethod
    async def _load_parent_row(
        context: Any,
        parent: KnowledgeParentKey,
    ) -> Ideation | Refinement | Spec | None:
        model: type[Ideation] | type[Refinement] | type[Spec]
        if parent.parent_type is KnowledgeParentType.IDEATION:
            model = Ideation
        elif parent.parent_type is KnowledgeParentType.REFINEMENT:
            model = Refinement
        else:
            model = Spec
        return (
            await context.execute(
                select(model)
                .where(model.id == parent.parent_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _parent_source_rows(
        context: Any,
        *,
        parent: KnowledgeParentKey,
        requested_ids: tuple[str, ...],
    ) -> tuple[Any, ...]:
        if not requested_ids:
            return ()
        if parent.parent_type is KnowledgeParentType.IDEATION:
            model = IdeationKnowledgeBase
            owner_column = IdeationKnowledgeBase.ideation_id
        elif parent.parent_type is KnowledgeParentType.REFINEMENT:
            model = RefinementKnowledgeBase
            owner_column = RefinementKnowledgeBase.refinement_id
        else:
            model = SpecKnowledgeBase
            owner_column = SpecKnowledgeBase.spec_id
        return tuple(
            (
                (
                    await context.execute(
                        select(model)
                        .where(
                            owner_column == parent.parent_id,
                            model.id.in_(requested_ids),
                        )
                        .order_by(model.id.asc())
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .all()
            )
        )

    async def _effective_spec_parent_sources(
        self,
        context: Any,
        *,
        parent: KnowledgeParentKey,
        requested_ids: tuple[str, ...],
        assignment_bindings: Mapping[
            str,
            TemporalKnowledgeAssignment,
        ]
        | None = None,
    ) -> tuple[KnowledgeSelectableSource, ...] | None:
        """Resolve Spec sources through native assignments and local attachments.

        Other parent types use their own physical source collections.
        """

        if parent.parent_type is not KnowledgeParentType.SPEC:
            return None
        if not requested_ids:
            return ()

        read = await KnowledgePropagationService(port=self).read(
            context,
            KnowledgeTargetKey(
                board_id=parent.board_id,
                target_type=KnowledgeTargetType.SPEC,
                target_id=parent.parent_id,
            ),
        )
        # One logical representative per root. Target-local Spec attachments
        # shadow an inherited assignment of the same root, matching Resource
        # Lineage's direct-over-inherited rule.
        representatives: dict[
            str,
            tuple[
                int,
                str,
                ResourceRevisionStamp,
                bytes,
                frozenset[str],
                object | None,
            ],
        ] = {}
        for item in read.effective_assignments:
            content_bytes = item.content_bytes
            if content_bytes is None:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_parent_effective_source_unreadable",
                    "an effective parent assignment has no canonical content",
                    details={
                        **parent.to_dict(),
                        "assignment_id": item.assignment.assignment_id,
                    },
                )
            assignment = item.assignment
            root = item.revision_stamp.root_id
            source_id = assignment.source_knowledge_id
            aliases = {root}
            if assignment.mode is KnowledgePropagationMode.REFERENCE:
                current_source_id = item.resolved_source_knowledge_id
                if current_source_id is not None:
                    source_id = current_source_id
                    aliases.add(current_source_id)
            else:
                # A parent Snapshot's frozen source remains its effective
                # identity until that parent assignment is explicitly
                # refreshed. The upstream physical "current" id is only stale
                # evidence and must not become a downstream selection token.
                aliases.add(assignment.source_knowledge_id)
            candidate = (
                0,
                source_id,
                item.revision_stamp,
                content_bytes,
                frozenset(aliases),
                item.governance_metadata,
            )
            prior = representatives.get(root)
            if prior is not None and prior != candidate:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_parent_effective_root_ambiguous",
                    "the parent Spec exposes multiple inherited resources for one root",
                    details={
                        **parent.to_dict(),
                        "root_id": root,
                    },
                )
            representatives[root] = candidate

        for item in read.effective_local_attachments:
            if item.content_bytes is None:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_parent_effective_source_unreadable",
                    "an effective parent-local attachment has no canonical content",
                    details={
                        **parent.to_dict(),
                        "source_knowledge_id": item.source_knowledge_id,
                    },
                )
            root = item.revision_stamp.root_id
            candidate = (
                1,
                item.source_knowledge_id,
                item.revision_stamp,
                item.content_bytes,
                frozenset((root, item.source_knowledge_id)),
                item.governance_metadata,
            )
            prior = representatives.get(root)
            if prior is not None and prior[0] == 1 and prior != candidate:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_parent_effective_root_ambiguous",
                    "the parent Spec exposes multiple local resources for one root",
                    details={
                        **parent.to_dict(),
                        "root_id": root,
                    },
                )
            representatives[root] = candidate

        # A physically deleted upstream source is intentionally absent from
        # ``effective_assignments``.  Preserve its current parent assignment as
        # deletion evidence only; dropped/inactive/history rows are not
        # selectable fallbacks.  An effective local/direct representative of
        # the same root continues to win.
        deleted_representatives: dict[
            str,
            tuple[
                str,
                ResourceRevisionStamp,
                frozenset[str],
                TemporalKnowledgeAssignment,
            ],
        ] = {}
        current_parent_assignments = {
            item.assignment.assignment_id: item
            for item in read.history_assignments
            if item.temporal.is_current
        }
        for item in read.resolved_assignments:
            if (
                item.state is not KnowledgeAssignmentState.SOURCE_DELETED
                or item.reason != "source_deleted"
            ):
                continue
            temporal = current_parent_assignments.get(item.assignment.assignment_id)
            if temporal is None:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_parent_deleted_source_unbound",
                    "a deleted parent source has no current durable assignment",
                    details={
                        **parent.to_dict(),
                        "assignment_id": item.assignment.assignment_id,
                    },
                )
            root = item.assignment.revision_stamp.root_id
            deleted_candidate = (
                item.assignment.source_knowledge_id,
                item.assignment.revision_stamp,
                frozenset(
                    (
                        root,
                        item.assignment.source_knowledge_id,
                    )
                ),
                temporal,
            )
            deleted_prior = deleted_representatives.get(root)
            if deleted_prior is not None and deleted_prior != deleted_candidate:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_parent_effective_root_ambiguous",
                    "the parent Spec exposes conflicting deleted resources for one root",
                    details={
                        **parent.to_dict(),
                        "root_id": root,
                    },
                )
            deleted_representatives[root] = deleted_candidate

        bindings = dict(assignment_bindings or {})
        resolved: list[KnowledgeSelectableSource] = []
        for requested_id in requested_ids:
            bound = _bound_assignment(requested_id, bindings)
            bound_root = (
                None if bound is None else bound.assignment.revision_stamp.root_id
            )
            matches = tuple(
                candidate
                for root_id, candidate in representatives.items()
                if (
                    root_id == bound_root
                    if bound_root is not None
                    else requested_id in candidate[4]
                )
            )
            deleted_matches = tuple(
                candidate
                for root_id, candidate in deleted_representatives.items()
                if root_id not in representatives
                and bound_root is not None
                and root_id == bound_root
            )
            if len(matches) + len(deleted_matches) > 1:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_parent_source_alias_ambiguous",
                    "a parent source token resolves to multiple effective roots",
                    details={
                        **parent.to_dict(),
                        "requested_knowledge_id": requested_id,
                    },
                )
            if not matches:
                if not deleted_matches:
                    continue
                assert bound is not None
                resolved.append(
                    _deleted_selectable_source(
                        requested_id,
                        bound,
                    )
                )
                continue
            (
                _priority,
                source_id,
                stamp,
                content_bytes,
                _aliases,
                governance_metadata,
            ) = matches[0]
            resolved.append(
                KnowledgeSelectableSource(
                    requested_knowledge_id=requested_id,
                    source_knowledge_id=source_id,
                    revision_stamp=stamp,
                    content_bytes=content_bytes,
                    source_deleted=False,
                    governance_metadata=governance_metadata,
                )
            )
        return tuple(resolved)

    async def load_parent_evidence(
        self,
        context: Any,
        request: KnowledgeParentLookup,
    ) -> KnowledgeParentEvidence:
        """Resolve creation preflight facts without requiring a target row."""

        if not isinstance(request, KnowledgeParentLookup):
            raise TypeError("knowledge_propagation_parent_lookup_invalid")
        try:
            row = await self._load_parent_row(context, request.parent)
            parent_exists = row is not None
            same_board = (
                row is not None and str(row.board_id) == request.parent.board_id
            )
            parent_state = (
                None
                if row is None
                else (
                    "archived"
                    if bool(getattr(row, "archived", False))
                    else _status_value(getattr(row, "status", ""))
                )
            )
            sources: tuple[KnowledgeSelectableSource, ...] = ()
            linked_spec_id: str | None = None
            functional_requirement_ids: tuple[str, ...] = ()
            acceptance_criterion_ids: tuple[str, ...] = ()
            test_scenario_ids: tuple[str, ...] = ()
            if same_board:
                effective_spec_sources = await self._effective_spec_parent_sources(
                    context,
                    parent=request.parent,
                    requested_ids=request.source_knowledge_ids,
                )
                if effective_spec_sources is None:
                    source_rows = await self._parent_source_rows(
                        context,
                        parent=request.parent,
                        requested_ids=request.source_knowledge_ids,
                    )
                    by_id = {str(item.id): item for item in source_rows}
                    sources = tuple(
                        _selectable_source(source_id, by_id[source_id])
                        for source_id in request.source_knowledge_ids
                        if source_id in by_id
                    )
                else:
                    sources = effective_spec_sources
                if request.parent.parent_type is KnowledgeParentType.SPEC:
                    assert isinstance(row, Spec)
                    linked_spec_id = str(row.id)
                    functional_requirement_ids = _structured_ids(
                        row.functional_requirements
                    )
                    acceptance_criterion_ids = _structured_ids(row.acceptance_criteria)
                    test_scenario_ids = _structured_ids(row.test_scenarios)

            return KnowledgeParentEvidence(
                parent=request.parent,
                parent_exists=parent_exists,
                same_board=same_board,
                parent_state=parent_state,
                sources=sources,
                linked_spec_id=linked_spec_id,
                functional_requirement_ids=functional_requirement_ids,
                acceptance_criterion_ids=acceptance_criterion_ids,
                test_scenario_ids=test_scenario_ids,
            )
        except KnowledgePropagationPortError:
            raise
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_parent_evidence_read_failed",
                "knowledge propagation parent evidence could not be read safely",
                details=request.parent.to_dict(),
            ) from exc

    @staticmethod
    def _target_parent_key(
        target: KnowledgeTargetKey,
        target_row: Spec | Card,
    ) -> KnowledgeParentKey | None:
        if target.target_type is KnowledgeTargetType.CARD:
            spec_id = cast(Card, target_row).spec_id
            if not spec_id:
                return None
            return KnowledgeParentKey(
                board_id=target.board_id,
                parent_type=KnowledgeParentType.SPEC,
                parent_id=str(spec_id),
            )

        spec = cast(Spec, target_row)
        if spec.refinement_id:
            return KnowledgeParentKey(
                board_id=target.board_id,
                parent_type=KnowledgeParentType.REFINEMENT,
                parent_id=str(spec.refinement_id),
            )
        if spec.ideation_id:
            return KnowledgeParentKey(
                board_id=target.board_id,
                parent_type=KnowledgeParentType.IDEATION,
                parent_id=str(spec.ideation_id),
            )
        return None

    async def _revalidate_target_parent(
        self,
        context: Any,
        *,
        plan: KnowledgeMutationPlan,
        target_row: Spec | Card,
    ) -> None:
        """Fence the CAS with fresh physical parent/board facts."""

        physical_parent = self._target_parent_key(plan.target, target_row)
        if plan.parent is not None and physical_parent != plan.parent:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_parent_changed",
                "the target parent changed after propagation preflight",
                details={
                    "expected_parent": plan.parent.to_dict(),
                    "actual_parent": (
                        None if physical_parent is None else physical_parent.to_dict()
                    ),
                },
            )
        parent = plan.parent or physical_parent
        if parent is None:
            # Legacy manually-created targets have no derivation parent. Their
            # target existence fence remains authoritative for compatibility.
            return
        evidence = await self.load_parent_evidence(
            context,
            KnowledgeParentLookup(
                parent=parent,
            ),
        )
        if not evidence.parent_exists or not evidence.same_board:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_parent_not_eligible",
                "the target parent no longer exists in the target board",
                details=evidence.to_dict(),
            )

    async def _scope_row(
        self,
        context: Any,
        target: KnowledgeTargetKey,
    ) -> KnowledgePropagationScopeRecord | None:
        return (
            await context.execute(
                select(KnowledgePropagationScopeRecord).where(
                    *_target_predicates(KnowledgePropagationScopeRecord, target)
                )
            )
        ).scalar_one_or_none()

    async def _physical_attachments(
        self, context: Any, *, target: KnowledgeTargetKey,
        target_row: Spec | Card,
    ) -> tuple[KnowledgeLocalAttachment, ...]:
        if target.target_type is KnowledgeTargetType.CARD:
            return ()
        rows = (await context.execute(
            select(SpecKnowledgeBase)
            .where(SpecKnowledgeBase.spec_id == target.target_id)
            .order_by(SpecKnowledgeBase.created_at.asc(), SpecKnowledgeBase.id.asc())
        )).scalars().all()
        local = []
        for row in rows:
            if not _is_direct_target_local_attachment(row):
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_physical_spec_attachment_invalid",
                    "Inherited Knowledge must use native assignments",
                    details=target.to_dict(),
                )
            local.append(_local_attachment(row, attached_at=row.created_at))
        return tuple(local)

    async def _selectable_sources(
        self,
        context: Any,
        *,
        target: KnowledgeTargetKey,
        target_row: Spec | Card,
        requested_ids: tuple[str, ...],
        assignment_bindings: Mapping[
            str,
            TemporalKnowledgeAssignment,
        ]
        | None = None,
    ) -> tuple[KnowledgeSelectableSource, ...]:
        """Resolve only the target's legitimate immediate-parent KB set."""

        if not requested_ids:
            return ()
        assignment_bindings = dict(assignment_bindings or {})
        root_bindings = {
            root_id: item.assignment.source_knowledge_id
            for root_id, item in assignment_bindings.items()
        }
        model: (
            type[IdeationKnowledgeBase]
            | type[RefinementKnowledgeBase]
            | type[SpecKnowledgeBase]
        )
        parent_predicates: tuple[object, ...]
        if target.target_type is KnowledgeTargetType.CARD:
            spec_id = cast(Card, target_row).spec_id
            if not spec_id:
                return ()
            effective_spec_sources = await self._effective_spec_parent_sources(
                context,
                parent=KnowledgeParentKey(
                    board_id=target.board_id,
                    parent_type=KnowledgeParentType.SPEC,
                    parent_id=str(spec_id),
                ),
                requested_ids=requested_ids,
                assignment_bindings=assignment_bindings,
            )
            if effective_spec_sources is not None:
                return effective_spec_sources
            model = SpecKnowledgeBase
            parent_predicates = (
                SpecKnowledgeBase.spec_id == spec_id,
                Spec.id == spec_id,
                Spec.board_id == target.board_id,
            )
            statement = select(SpecKnowledgeBase).join(
                Spec,
                Spec.id == SpecKnowledgeBase.spec_id,
            )
        else:
            spec = cast(Spec, target_row)
            if spec.refinement_id:
                model = RefinementKnowledgeBase
                parent_predicates = (
                    RefinementKnowledgeBase.refinement_id == spec.refinement_id,
                    Refinement.id == spec.refinement_id,
                    Refinement.board_id == target.board_id,
                )
                statement = select(RefinementKnowledgeBase).join(
                    Refinement,
                    Refinement.id == RefinementKnowledgeBase.refinement_id,
                )
            elif spec.ideation_id:
                model = IdeationKnowledgeBase
                parent_predicates = (
                    IdeationKnowledgeBase.ideation_id == spec.ideation_id,
                    Ideation.id == spec.ideation_id,
                    Ideation.board_id == target.board_id,
                )
                statement = select(IdeationKnowledgeBase).join(
                    Ideation,
                    Ideation.id == IdeationKnowledgeBase.ideation_id,
                )
            else:
                return ()
        physical_ids = set(requested_ids)
        physical_ids.update(root_bindings.values())
        root_ids = set(root_bindings)
        initial_rows = (
            (
                await context.execute(
                    statement.where(
                        *parent_predicates,
                        or_(
                            model.id.in_(tuple(sorted(physical_ids))),
                            model.root_source_kb_id.in_(tuple(sorted(root_ids))),
                        ),
                    ).order_by(model.id.asc())
                )
            )
            .scalars()
            .all()
        )
        discovered_roots = set(root_ids)
        for row in initial_rows:
            _content, content_hash = _kb_content(row)
            discovered_roots.add(
                _selectable_kb_stamp(
                    row,
                    content_sha256=content_hash,
                ).root_id
            )
        expanded_rows: Sequence[object] = ()
        if discovered_roots:
            expanded_rows = (
                (
                    await context.execute(
                        statement.where(
                            *parent_predicates,
                            or_(
                                model.id.in_(tuple(sorted(discovered_roots))),
                                model.root_source_kb_id.in_(
                                    tuple(sorted(discovered_roots))
                                ),
                            ),
                        ).order_by(model.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        by_id = {str(row.id): row for row in (*initial_rows, *expanded_rows)}
        rows = tuple(by_id[identity] for identity in sorted(by_id))

        external_parent_ids = {
            str(parent_id)
            for row in rows
            if (
                parent_id := _first_kb_value(
                    row,
                    "immediate_parent_kb_id",
                    "source_kb_id",
                )
            )
            not in (None, "")
            and str(parent_id) not in by_id
        }
        known_parent_roots: dict[str, str] = {}
        if external_parent_ids:
            same_table_parents = (
                (
                    await context.execute(
                        select(model).where(
                            model.id.in_(tuple(sorted(external_parent_ids)))
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in same_table_parents:
                _content, content_hash = _kb_content(row)
                known_parent_roots[str(row.id)] = _selectable_kb_stamp(
                    row,
                    content_sha256=content_hash,
                ).root_id

        by_root: dict[str, list[object]] = {}
        for row in rows:
            _content, content_hash = _kb_content(row)
            root_id = _selectable_kb_stamp(
                row,
                content_sha256=content_hash,
            ).root_id
            by_root.setdefault(root_id, []).append(row)

        resolved: list[KnowledgeSelectableSource] = []
        for requested_id in requested_ids:
            bound = _bound_assignment(requested_id, assignment_bindings)
            lookup_root = (
                None if bound is None else bound.assignment.revision_stamp.root_id
            )
            row = (
                None
                if lookup_root is None
                else _current_physical_source(
                    by_root.get(lookup_root, ()),
                    root_id=lookup_root,
                    known_parent_root_by_id=known_parent_roots,
                )
            )
            if row is None and requested_id in by_id:
                exact = by_id[requested_id]
                _content, content_hash = _kb_content(exact)
                exact_root = _selectable_kb_stamp(
                    exact,
                    content_sha256=content_hash,
                ).root_id
                current = _current_physical_source(
                    by_root.get(exact_root, ()),
                    root_id=exact_root,
                    known_parent_root_by_id=known_parent_roots,
                )
                # A new request by physical id is valid only for the current
                # leaf. Existing assignments carry an explicit root binding
                # and take the lookup_root branch above.
                if current is not None and _kb_identity(current) == requested_id:
                    row = current
            if row is None:
                row = _current_physical_source(
                    by_root.get(requested_id, ()),
                    root_id=requested_id,
                    known_parent_root_by_id=known_parent_roots,
                )
            if row is not None:
                resolved.append(_selectable_source(requested_id, row))
                continue
            if bound is not None:
                resolved.append(_deleted_selectable_source(requested_id, bound))
        return tuple(resolved)


    async def get_idempotency_entry(
        self,
        context: Any,
        request: KnowledgeIdempotencyLookup,
    ) -> KnowledgeMutationLedgerEntry | None:
        if not isinstance(request, KnowledgeIdempotencyLookup):
            raise TypeError("knowledge_propagation_idempotency_lookup_invalid")
        try:
            # Replay is intentionally target-independent. A deterministic
            # target may already be staged in this caller UoW; flushing it
            # before consulting the durable ledger would turn a valid retry
            # into a target-PK collision instead of a replay.
            with context.no_autoflush:
                row = (
                    await context.execute(
                        select(KnowledgeMutationLedgerRecord).where(
                            *_target_predicates(
                                KnowledgeMutationLedgerRecord,
                                request.target,
                            ),
                            KnowledgeMutationLedgerRecord.idempotency_key
                            == request.idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
            return None if row is None else _ledger_from_row(row)
        except KnowledgePropagationPortError:
            raise
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_ledger_read_failed",
                "canonical knowledge mutation ledger could not be read safely",
                details=request.target.to_dict(),
            ) from exc

    async def load_scope(
        self,
        context: Any,
        request: KnowledgeScopeLookup,
    ) -> KnowledgePropagationScope:
        if not isinstance(request, KnowledgeScopeLookup):
            raise TypeError("knowledge_propagation_scope_lookup_invalid")
        try:
            target_row = await self._load_target(context, request.target)
            scope = await self._scope_row(context, request.target)
            assignments: tuple[TemporalKnowledgeAssignment, ...] = ()
            tombstones: tuple[KnowledgePropagationTombstone, ...] = ()
            snapshots: tuple[KnowledgePropagationSnapshot, ...] = ()
            source_ids = set(request.source_knowledge_ids)
            assignment_bindings: dict[
                str,
                TemporalKnowledgeAssignment,
            ] = {}
            if scope is not None:
                assignment_rows = (
                    (
                        await context.execute(
                            select(KnowledgeAssignmentRecord)
                            .where(KnowledgeAssignmentRecord.scope_id == scope.id)
                            .order_by(
                                KnowledgeAssignmentRecord.effective_from.asc(),
                                KnowledgeAssignmentRecord.assignment_id.asc(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assignments = tuple(
                    _assignment_from_row(row, request.target) for row in assignment_rows
                )
                source_ids.update(
                    item.assignment.source_knowledge_id
                    for item in assignments
                    if item.temporal.is_current
                )
                assignment_bindings = _current_assignment_bindings(assignments)
                source_ids.update(assignment_bindings)
                tombstone_rows = (
                    (
                        await context.execute(
                            select(KnowledgeTombstoneRecord)
                            .where(KnowledgeTombstoneRecord.scope_id == scope.id)
                            .order_by(
                                KnowledgeTombstoneRecord.effective_from.asc(),
                                KnowledgeTombstoneRecord.tombstone_id.asc(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                tombstones = tuple(
                    _tombstone_from_row(row, request.target) for row in tombstone_rows
                )
                snapshot_rows = (
                    (
                        await context.execute(
                            select(KnowledgeSnapshotRecord)
                            .where(KnowledgeSnapshotRecord.scope_id == scope.id)
                            .order_by(
                                KnowledgeSnapshotRecord.effective_from.asc(),
                                KnowledgeSnapshotRecord.snapshot_id.asc(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                snapshots = tuple(_snapshot_from_row(row) for row in snapshot_rows)

            local = await self._physical_attachments(
                context,
                target=request.target,
                target_row=target_row,
            )
            sources = await self._selectable_sources(
                context,
                target=request.target,
                target_row=target_row,
                requested_ids=tuple(sorted(source_ids)),
                assignment_bindings=assignment_bindings,
            )
            return KnowledgePropagationScope(
                target=request.target,
                scope_revision=0 if scope is None else int(scope.scope_revision),
                selection_state=KnowledgeSelectionState.OMITTED if scope is None else scope.selection_state,
                assignments=assignments,
                tombstones=tombstones,
                snapshots=snapshots,
                sources=sources,
                local_attachments=local,
            )
        except KnowledgePropagationPortError:
            raise
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_scope_read_failed",
                "knowledge propagation scope could not be reconstructed safely",
                details=request.target.to_dict(),
            ) from exc

    async def _existing_ledger(
        self,
        context: Any,
        plan: KnowledgeMutationPlan,
    ) -> KnowledgeMutationLedgerEntry | None:
        return await self.get_idempotency_entry(
            context,
            KnowledgeIdempotencyLookup(
                target=plan.target,
                idempotency_key=plan.idempotency_key,
            ),
        )

    async def _advance_scope(
        self,
        context: Any,
        plan: KnowledgeMutationPlan,
    ) -> str:
        next_selection_state = (
            None
            if plan.next_scope_selection_state is None
            else _enum_value(plan.next_scope_selection_state)
        )
        scope = await self._scope_row(context, plan.target)
        if scope is None:
            if plan.expected_revision != 0:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_revision_conflict",
                    "expected revision does not match the current scope revision",
                    details={
                        "expected_revision": plan.expected_revision,
                        "current_revision": 0,
                    },
                )
            scope_id = str(uuid4())
            context.add(
                KnowledgePropagationScopeRecord(
                    id=scope_id,
                    board_id=plan.target.board_id,
                    target_type=_enum_value(plan.target.target_type),
                    target_id=plan.target.target_id,
                    scope_revision=plan.next_revision,
                    selection_state=next_selection_state,
                    created_at=plan.occurred_at,
                    updated_at=plan.occurred_at,
                )
            )
            await context.flush()
            return scope_id

        statement = (
            update(KnowledgePropagationScopeRecord)
            .where(
                KnowledgePropagationScopeRecord.id == scope.id,
                KnowledgePropagationScopeRecord.scope_revision
                == plan.expected_revision,
            )
            .values(
                scope_revision=plan.next_revision,
                selection_state=next_selection_state,
                updated_at=plan.occurred_at,
            )
            .execution_options(synchronize_session=False)
        )
        result = await context.execute(statement)
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            current = (
                await context.execute(
                    select(KnowledgePropagationScopeRecord.scope_revision).where(
                        KnowledgePropagationScopeRecord.id == scope.id
                    )
                )
            ).scalar_one_or_none()
            raise KnowledgePropagationPortError(
                "knowledge_propagation_revision_conflict",
                "expected revision does not match the current scope revision",
                details={
                    "expected_revision": plan.expected_revision,
                    "current_revision": current,
                },
            )
        return str(scope.id)

    @staticmethod
    async def _close_records(
        context: Any,
        *,
        scope_id: str,
        plan: KnowledgeMutationPlan,
    ) -> None:
        record_sets = (
            (
                KnowledgeRecordKind.ASSIGNMENT,
                KnowledgeAssignmentRecord,
                KnowledgeAssignmentRecord.assignment_id,
                plan.assignment_ids_to_close,
            ),
            (
                KnowledgeRecordKind.SNAPSHOT,
                KnowledgeSnapshotRecord,
                KnowledgeSnapshotRecord.snapshot_id,
                plan.snapshot_ids_to_close,
            ),
            (
                KnowledgeRecordKind.TOMBSTONE,
                KnowledgeTombstoneRecord,
                KnowledgeTombstoneRecord.tombstone_id,
                plan.tombstone_ids_to_close,
            ),
        )
        for record_kind, model, identity_column, identities in record_sets:
            for identity in identities:
                result = await context.execute(
                    update(model)
                    .where(
                        model.scope_id == scope_id,
                        identity_column == identity,
                        model.effective_to.is_(None),
                    )
                    .values(
                        effective_to=plan.occurred_at,
                        superseded_by_id=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    raise KnowledgePropagationPortError(
                        "knowledge_propagation_temporal_conflict",
                        "a record selected for closure is missing or no longer current",
                        details={
                            "record_kind": record_kind.value,
                            "record_id": identity,
                        },
                    )

    @staticmethod
    async def _link_supersessions(
        context: Any,
        *,
        scope_id: str,
        plan: KnowledgeMutationPlan,
    ) -> None:
        """Link closed rows after their successors have been inserted.

        SQLite checks self-referential foreign keys immediately. Closing with
        a future id before inserting the successor fails, while inserting the
        successor first can violate the partial-current unique index. Both
        steps remain invisible inside this same uncommitted transaction.
        """

        models = {
            KnowledgeRecordKind.ASSIGNMENT: (
                KnowledgeAssignmentRecord,
                KnowledgeAssignmentRecord.assignment_id,
            ),
            KnowledgeRecordKind.SNAPSHOT: (
                KnowledgeSnapshotRecord,
                KnowledgeSnapshotRecord.snapshot_id,
            ),
            KnowledgeRecordKind.TOMBSTONE: (
                KnowledgeTombstoneRecord,
                KnowledgeTombstoneRecord.tombstone_id,
            ),
        }
        for link in plan.supersession_links:
            record_kind = cast(KnowledgeRecordKind, link.record_kind)
            model, identity_column = models[record_kind]
            result = await context.execute(
                update(model)
                .where(
                    model.scope_id == scope_id,
                    identity_column == link.previous_id,
                    model.effective_to == plan.occurred_at,
                    model.superseded_by_id.is_(None),
                )
                .values(superseded_by_id=link.successor_id)
                .execution_options(synchronize_session=False)
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_supersession_conflict",
                    "a temporal predecessor could not be linked to its successor",
                    details=link.to_dict(),
                )

    @staticmethod
    def _stage_open_records(
        context: Any,
        *,
        scope_id: str,
        plan: KnowledgeMutationPlan,
    ) -> None:
        for temporal in plan.assignments_to_open:
            item = temporal.assignment
            context.add(
                KnowledgeAssignmentRecord(
                    assignment_id=item.assignment_id,
                    scope_id=scope_id,
                    source_knowledge_id=item.source_knowledge_id,
                    root_id=item.revision_stamp.root_id,
                    immediate_parent_id=item.revision_stamp.immediate_parent_id,
                    source_revision=item.revision_stamp.source_revision,
                    source_content_sha256=item.revision_stamp.source_content_sha256,
                    mode=_enum_value(item.mode),
                    state=_enum_value(item.state),
                    origin_class=_enum_value(item.origin_class),
                    actor_id=item.actor_id,
                    revision=item.revision,
                    justification=item.justification,
                    relevance_links=[link.to_dict() for link in item.relevance_links],
                    effective_from=temporal.temporal.effective_from,
                    effective_to=temporal.temporal.effective_to,
                    superseded_by_id=temporal.temporal.superseded_by_id,
                )
            )
        for item in plan.snapshots_to_open:
            context.add(
                KnowledgeSnapshotRecord(
                    snapshot_id=item.snapshot_id,
                    scope_id=scope_id,
                    assignment_id=item.assignment_id,
                    root_id=item.revision_stamp.root_id,
                    immediate_parent_id=item.revision_stamp.immediate_parent_id,
                    source_revision=item.revision_stamp.source_revision,
                    source_content_sha256=item.revision_stamp.source_content_sha256,
                    content_bytes=item.content_bytes,
                    effective_from=item.temporal.effective_from,
                    effective_to=item.temporal.effective_to,
                    superseded_by_id=item.temporal.superseded_by_id,
                    governance_metadata=copy.deepcopy(item.governance_metadata),
                )
            )
        for item in plan.tombstones_to_open:
            context.add(
                KnowledgeTombstoneRecord(
                    tombstone_id=item.tombstone_id,
                    scope_id=scope_id,
                    root_id=item.root_id,
                    actor_id=item.actor_id,
                    justification=item.justification,
                    effective_from=item.temporal.effective_from,
                    effective_to=item.temporal.effective_to,
                    superseded_by_id=item.temporal.superseded_by_id,
                )
            )

    @staticmethod
    def _stage_ledger(
        context: Any,
        *,
        scope_id: str,
        entry: KnowledgeMutationLedgerEntry,
    ) -> None:
        receipt = entry.receipt
        context.add(
            KnowledgeMutationLedgerRecord(
                operation_id=receipt.operation_id,
                scope_id=scope_id,
                board_id=entry.target.board_id,
                target_type=_enum_value(entry.target.target_type),
                target_id=entry.target.target_id,
                idempotency_key=entry.idempotency_key,
                request_hash=entry.request_hash,
                operation_kind=_enum_value(entry.operation_kind),
                actor_id=entry.actor_id,
                previous_revision=receipt.previous_revision,
                revision=receipt.revision,
                outcome=_enum_value(receipt.outcome),
                reason_code=receipt.reason_code,
                reason_detail=receipt.reason_detail,
                details=copy.deepcopy(dict(receipt.details)),
                applied_at=receipt.applied_at,
                recorded_at=entry.recorded_at,
            )
        )

    async def stage_mutation(
        self,
        context: Any,
        plan: KnowledgeMutationPlan,
    ) -> KnowledgeMutationReceipt:
        if not isinstance(plan, KnowledgeMutationPlan):
            raise TypeError("knowledge_propagation_mutation_plan_invalid")
        assert plan.ledger_entry is not None
        try:
            # This protects direct re-staging of the exact immutable plan.
            # A service-level replay normally returns earlier via
            # get_idempotency_entry and records a replay attempt.
            existing = await self._existing_ledger(context, plan)
            if existing is not None:
                if existing == plan.ledger_entry:
                    return existing.receipt
                same_request = (
                    existing.target == plan.target
                    and existing.idempotency_key == plan.idempotency_key
                    and existing.request_hash == plan.request_hash
                    and existing.operation_kind is plan.operation_kind
                    and existing.actor_id == plan.actor_id
                )
                if same_request:
                    if existing.receipt.outcome is KnowledgeMutationOutcome.REJECTED:
                        raise KnowledgePropagationPortError(
                            str(existing.receipt.reason_code),
                            str(existing.receipt.reason_detail),
                            details=existing.receipt.details,
                        )
                    replay = existing.receipt.as_replay()
                    await self._stage_attempt(
                        context,
                        KnowledgeMutationAttempt(
                            attempt_id=f"kbatm_late_{plan.operation_id}",
                            target=plan.target,
                            idempotency_key=plan.idempotency_key,
                            request_hash=plan.request_hash,
                            operation_kind=plan.operation_kind,
                            actor_id=plan.actor_id,
                            outcome=KnowledgeMutationOutcome.REPLAYED,
                            recorded_at=plan.occurred_at,
                            original_operation_id=(existing.receipt.operation_id),
                            details={"late_stage_replay": True},
                        ),
                    )
                    return replay
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_idempotency_conflict",
                    "idempotency key was already used with a different mutation",
                    details={
                        "idempotency_key": plan.idempotency_key,
                        "original_request_hash": existing.request_hash,
                        "request_hash": plan.request_hash,
                    },
                )

            # The scope target is polymorphic and therefore cannot carry one
            # relational FK. Revalidate it after replay lookup and directly
            # before the CAS/insert boundary. SQLAlchemy autoflush also makes
            # a target newly staged in this same caller UoW visible here.
            target_row = await self._lock_target(context, plan)
            physical_parent = self._target_parent_key(plan.target, target_row)
            await self._fence_parent_evidence(
                context,
                plan,
                physical_parent,
            )
            await self._fence_planned_sources(
                context,
                plan=plan,
                parent=plan.parent or physical_parent,
            )
            await self._revalidate_target_parent(
                context,
                plan=plan,
                target_row=target_row,
            )
            scope_id = await self._advance_scope(context, plan)
            await self._close_records(context, scope_id=scope_id, plan=plan)
            self._stage_open_records(context, scope_id=scope_id, plan=plan)
            await context.flush()
            await self._link_supersessions(context, scope_id=scope_id, plan=plan)
            self._stage_ledger(
                context,
                scope_id=scope_id,
                entry=plan.ledger_entry,
            )
            await context.flush()
            return plan.ledger_entry.receipt
        except KnowledgePropagationPortError:
            raise
        except IntegrityError as exc:
            if _is_first_creation_race(exc, plan):
                raise KnowledgePropagationPortError(
                    "knowledge_creation_race",
                    "a concurrent deterministic creation won; retry for replay",
                    details=plan.target.to_dict(),
                ) from exc
            raise KnowledgePropagationPortError(
                "knowledge_propagation_constraint_conflict",
                "a concurrent or divergent mutation violated a durable invariant",
                details=plan.target.to_dict(),
            ) from exc
        except SQLAlchemyError as exc:
            if _is_first_creation_race(exc, plan):
                raise KnowledgePropagationPortError(
                    "knowledge_creation_race",
                    "a concurrent deterministic creation won; retry for replay",
                    details=plan.target.to_dict(),
                ) from exc
            if _has_sqlite_busy_snapshot(exc):
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_concurrent_write",
                    ("a concurrent writer changed the propagation preflight snapshot"),
                    details=plan.target.to_dict(),
                ) from exc
            raise KnowledgePropagationPortError(
                "knowledge_propagation_stage_failed",
                "knowledge propagation mutation could not be staged",
                details=plan.target.to_dict(),
            ) from exc

    async def _stage_attempt(
        self,
        context: Any,
        attempt: KnowledgeMutationAttempt,
    ) -> None:
        existing = await context.get(
            KnowledgeMutationAttemptRecord,
            attempt.attempt_id,
        )
        if existing is not None:
            if _attempt_from_row(existing) == attempt:
                return
            raise KnowledgePropagationPortError(
                "knowledge_propagation_attempt_conflict",
                "attempt id already identifies a different immutable observation",
                details={"attempt_id": attempt.attempt_id},
            )
        scope = await self._scope_row(context, attempt.target)
        context.add(
            KnowledgeMutationAttemptRecord(
                attempt_id=attempt.attempt_id,
                scope_id=None if scope is None else scope.id,
                board_id=attempt.target.board_id,
                target_type=_enum_value(attempt.target.target_type),
                target_id=attempt.target.target_id,
                idempotency_key=attempt.idempotency_key,
                request_hash=attempt.request_hash,
                operation_kind=_enum_value(attempt.operation_kind),
                actor_id=attempt.actor_id,
                outcome=_enum_value(attempt.outcome),
                recorded_at=attempt.recorded_at,
                original_operation_id=attempt.original_operation_id,
                reason_code=attempt.reason_code,
                reason_detail=attempt.reason_detail,
                details=copy.deepcopy(dict(attempt.details)),
            )
        )
        await context.flush()

    async def stage_attempt(
        self,
        context: Any,
        attempt: KnowledgeMutationAttempt,
    ) -> None:
        if not isinstance(attempt, KnowledgeMutationAttempt):
            raise TypeError("knowledge_propagation_attempt_invalid")
        try:
            await self._stage_attempt(context, attempt)
        except KnowledgePropagationPortError:
            raise
        except IntegrityError as exc:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_attempt_conflict",
                "concurrent append won the immutable attempt id",
                details={"attempt_id": attempt.attempt_id},
            ) from exc
        except SQLAlchemyError as exc:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_attempt_stage_failed",
                "knowledge mutation attempt could not be staged",
                details={"attempt_id": attempt.attempt_id},
            ) from exc

    async def append_after_rollback(
        self,
        attempt: KnowledgeMutationAttempt,
    ) -> None:
        """Append one failed-request observation in an autonomous transaction."""

        if not isinstance(attempt, KnowledgeMutationAttempt):
            raise TypeError("knowledge_propagation_attempt_invalid")
        async with self._session_factory() as session:
            try:
                await self.stage_attempt(session, attempt)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


__all__ = [
    "CommunitySqlAlchemyKnowledgePropagationStore",
    "is_knowledge_creation_race_error",
]
