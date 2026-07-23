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

from sqlalchemy import func, select, update
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
    KnowledgeOriginClass,
    KnowledgeRelevanceLink,
    KnowledgeTargetType,
)
from okto_pulse.core.domain.resource_revision import ResourceRevisionStamp
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeIdempotencyLookup,
    KnowledgeLegacyAttachment,
    KnowledgeMutationAttempt,
    KnowledgeMutationKind,
    KnowledgeMutationLedgerEntry,
    KnowledgeMutationOutcome,
    KnowledgeMutationPlan,
    KnowledgeMutationReceipt,
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
    KnowledgeGrandfatherAttachment,
    KnowledgeGrandfatherEvidence,
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


def _target_predicates(
    model: type[KnowledgePropagationScopeRecord] | type[KnowledgeMutationLedgerRecord],
    target: KnowledgeTargetKey,
) -> tuple[object, ...]:
    return (
        model.board_id == target.board_id,
        model.target_type == _enum_value(target.target_type),
        model.target_id == target.target_id,
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
    if explicit not in (None, ""):
        return str(explicit)
    payload = _canonical_kb_payload(item)
    digest = hashlib.sha256(knowledge_content_bytes(payload)).hexdigest()
    return f"legacy_kb_{digest}"


def _kb_content(item: object) -> tuple[bytes, str]:
    identity = _kb_identity(item)
    payload = _canonical_kb_payload(item, fallback_id=identity)
    content_bytes = knowledge_content_bytes(payload)
    content_sha256 = hashlib.sha256(content_bytes).hexdigest()
    return content_bytes, content_sha256


def _legacy_kb_stamp(item: object) -> ResourceRevisionStamp:
    """Preserve nullable historical revision/hash evidence exactly as stored."""

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

    History remains nullable through ``_legacy_kb_stamp``. A live source
    without an explicit revision uses its canonical digest as a deterministic
    revision identity.
    """

    legacy = _legacy_kb_stamp(item)
    return ResourceRevisionStamp(
        root_id=legacy.root_id,
        immediate_parent_id=legacy.immediate_parent_id,
        source_revision=legacy.source_revision or content_sha256,
        source_content_sha256=content_sha256,
    )


def _legacy_origin(item: object, *, actual_sha256: str) -> KnowledgeOriginClass:
    raw_origin = _kb_value(item, "origin_class")
    if raw_origin not in (None, ""):
        try:
            origin = KnowledgeOriginClass(str(raw_origin))
        except ValueError:
            return KnowledgeOriginClass.LEGACY_UNRESOLVED
        # An historical payload may explicitly carry unresolved evidence, but
        # selected_legacy is accepted only from the canonical grandfather
        # ledger parsed below.  Arbitrary legacy JSON is not selection proof.
        if origin is KnowledgeOriginClass.LEGACY_UNRESOLVED:
            return origin

    lineage_status = str(
        _kb_value(item, "lineage_status") or _kb_value(item, "origin_resolution") or ""
    ).strip()
    if lineage_status in {"missing", "cycle", "divergent"}:
        return KnowledgeOriginClass.LEGACY_UNRESOLVED
    identity = _kb_identity(item)
    parent = _first_kb_value(
        item,
        "immediate_parent_kb_id",
        "source_kb_id",
    )
    if parent not in (None, "") and str(parent) == identity:
        return KnowledgeOriginClass.LEGACY_UNRESOLVED
    persisted_hash = _first_kb_value(
        item,
        "source_content_sha256",
        "content_hash",
    )
    if persisted_hash not in (None, "") and str(persisted_hash) != actual_sha256:
        return KnowledgeOriginClass.LEGACY_UNRESOLVED
    # selected_legacy is accepted only when an existing durable envelope says
    # so.  Absence of such evidence must preserve the conservative legacy-all
    # compatibility behavior.
    return KnowledgeOriginClass.LEGACY_ALL


def _legacy_attachment(
    item: object,
    grandfathered: Mapping[str, object] | None = None,
) -> KnowledgeLegacyAttachment:
    stamp = _legacy_kb_stamp(item)
    _content_bytes, physical_hash = _kb_content(item)
    origin = _legacy_origin(
        item,
        actual_sha256=physical_hash,
    )
    physical_unresolved = origin is KnowledgeOriginClass.LEGACY_UNRESOLVED
    raw_effective = _kb_value(item, "effective")
    effective = raw_effective if isinstance(raw_effective, bool) else True
    if grandfathered is not None:
        try:
            durable_origin = KnowledgeOriginClass(str(grandfathered["origin_class"]))
        except (KeyError, ValueError) as exc:
            raise ValueError(
                "knowledge_propagation_grandfather_origin_invalid"
            ) from exc
        if durable_origin is KnowledgeOriginClass.V2:
            raise ValueError("knowledge_propagation_grandfather_origin_invalid")
        source_knowledge_id = str(grandfathered["source_knowledge_id"])
        if source_knowledge_id != _kb_identity(item):
            raise ValueError("knowledge_propagation_grandfather_identity_mismatch")
        durable_hash = grandfathered.get("source_content_sha256")
        evidence = grandfathered.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("knowledge_propagation_grandfather_evidence_invalid")
        evidence_fields = (
            "durable_selection_evidence",
            "origin_missing",
            "origin_cycle",
            "content_divergent",
        )
        if set(evidence) != set(evidence_fields) or any(
            not isinstance(evidence.get(field_name), bool)
            for field_name in evidence_fields
        ):
            raise ValueError("knowledge_propagation_grandfather_evidence_invalid")
        declared_unresolved = any(
            evidence.get(field_name) is True
            for field_name in (
                "origin_missing",
                "origin_cycle",
                "content_divergent",
            )
        )
        selected = evidence.get("durable_selection_evidence") is True
        expected_origin = (
            KnowledgeOriginClass.LEGACY_UNRESOLVED
            if declared_unresolved
            else (
                KnowledgeOriginClass.SELECTED_LEGACY
                if selected
                else KnowledgeOriginClass.LEGACY_ALL
            )
        )
        if durable_origin is not expected_origin:
            raise ValueError("knowledge_propagation_grandfather_origin_invalid")
        stamp = ResourceRevisionStamp(
            root_id=str(grandfathered["root_id"]),
            immediate_parent_id=(
                None
                if grandfathered.get("immediate_parent_id") in (None, "")
                else str(grandfathered["immediate_parent_id"])
            ),
            source_revision=(
                None
                if grandfathered.get("source_revision") in (None, "")
                else str(grandfathered["source_revision"])
            ),
            source_content_sha256=(
                None if durable_hash in (None, "") else str(durable_hash)
            ),
        )
        origin = durable_origin
        durable_effective = grandfathered.get("effective")
        if not isinstance(durable_effective, bool):
            raise ValueError("knowledge_propagation_grandfather_effective_invalid")
        if durable_effective is not (
            durable_origin is not KnowledgeOriginClass.LEGACY_UNRESOLVED
        ):
            raise ValueError("knowledge_propagation_grandfather_effective_invalid")
        runtime_unresolved = declared_unresolved or physical_unresolved
        if durable_hash not in (None, "") and str(durable_hash) != physical_hash:
            durable_origin = KnowledgeOriginClass.LEGACY_UNRESOLVED
            runtime_unresolved = True
        elif runtime_unresolved:
            durable_origin = KnowledgeOriginClass.LEGACY_UNRESOLVED
        origin = durable_origin
        effective = durable_effective and not runtime_unresolved
    return KnowledgeLegacyAttachment(
        source_knowledge_id=_kb_identity(item),
        revision_stamp=stamp,
        origin_class=origin,
        effective=effective,
    )


def _grandfathered_classifications(
    details: object,
) -> dict[str, Mapping[str, object]]:
    if not isinstance(details, Mapping):
        raise ValueError("knowledge_propagation_grandfather_details_invalid")
    if set(details) != {
        "contract_version",
        "legacy_content_preserved",
        "grandfathered_attachments",
    }:
        raise ValueError("knowledge_propagation_grandfather_details_invalid")
    if (
        details.get("contract_version") != 2
        or details.get("legacy_content_preserved") is not True
    ):
        raise ValueError("knowledge_propagation_grandfather_details_invalid")
    raw_items = details.get("grandfathered_attachments")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("knowledge_propagation_grandfather_details_invalid")
    classifications: dict[str, Mapping[str, object]] = {}
    physical_identities: set[tuple[str, str, str, str]] = set()
    prior_sort_key: tuple[str, str, str, str, str] | None = None
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        if set(raw) != {
            "source_knowledge_id",
            "root_id",
            "immediate_parent_id",
            "source_revision",
            "source_content_sha256",
            "origin_class",
            "effective",
            "evidence",
            "physical_locator",
        }:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        source_id = raw.get("source_knowledge_id")
        root_id = raw.get("root_id")
        immediate_parent_id = raw.get("immediate_parent_id")
        source_revision = raw.get("source_revision")
        source_hash = raw.get("source_content_sha256")
        raw_origin = raw.get("origin_class")
        raw_effective = raw.get("effective")
        evidence = raw.get("evidence")
        locator = raw.get("physical_locator")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        if not isinstance(root_id, str) or not root_id.strip():
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        for optional_text in (
            immediate_parent_id,
            source_revision,
            source_hash,
        ):
            if optional_text is not None and (
                not isinstance(optional_text, str) or not optional_text.strip()
            ):
                raise ValueError("knowledge_propagation_grandfather_details_invalid")
        if source_hash is not None and _SHA256_HEX.fullmatch(source_hash) is None:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "durable_selection_evidence",
            "origin_missing",
            "origin_cycle",
            "content_divergent",
        }:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        if any(type(value) is not bool for value in evidence.values()):
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        unresolved = any(
            evidence[field_name]
            for field_name in (
                "origin_missing",
                "origin_cycle",
                "content_divergent",
            )
        )
        expected_origin = (
            KnowledgeOriginClass.LEGACY_UNRESOLVED.value
            if unresolved
            else (
                KnowledgeOriginClass.SELECTED_LEGACY.value
                if evidence["durable_selection_evidence"]
                else KnowledgeOriginClass.LEGACY_ALL.value
            )
        )
        if raw_origin != expected_origin:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        expected_effective = raw_origin != KnowledgeOriginClass.LEGACY_UNRESOLVED.value
        if type(raw_effective) is not bool or raw_effective is not expected_effective:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        if not isinstance(locator, Mapping):
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        if set(locator) != {
            "storage_kind",
            "table",
            "owner_id",
            "attachment_id",
        }:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        locator_values = tuple(
            locator.get(field_name)
            for field_name in (
                "storage_kind",
                "table",
                "owner_id",
                "attachment_id",
            )
        )
        if any(not isinstance(value, str) or not value for value in locator_values):
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        storage_kind, table, owner_id, attachment_id = cast(
            tuple[str, str, str, str],
            locator_values,
        )
        if storage_kind not in {"entity_row", "card_json"}:
            raise ValueError("knowledge_propagation_grandfather_details_invalid")
        physical_identity = (
            storage_kind,
            table,
            owner_id,
            attachment_id,
        )
        if physical_identity in physical_identities:
            raise ValueError("knowledge_propagation_grandfather_identity_ambiguous")
        physical_identities.add(physical_identity)
        sort_key = (
            source_id,
            storage_kind,
            table,
            owner_id,
            attachment_id,
        )
        if prior_sort_key is not None and sort_key <= prior_sort_key:
            raise ValueError("knowledge_propagation_grandfather_order_invalid")
        prior_sort_key = sort_key
        if source_id in classifications:
            raise ValueError("knowledge_propagation_grandfather_identity_ambiguous")
        classifications[source_id] = raw
    return classifications


def _physical_grandfather_record(
    classifications: Mapping[str, Mapping[str, object]],
    item: object,
    *,
    storage_kind: str,
    table: str,
    owner_id: str,
) -> Mapping[str, object] | None:
    source_id = _kb_identity(item)
    record = classifications.get(source_id)
    if record is None:
        return None
    locator = cast(Mapping[str, object], record["physical_locator"])
    expected = {
        "storage_kind": storage_kind,
        "table": table,
        "owner_id": owner_id,
        "attachment_id": source_id,
    }
    if dict(locator) != expected:
        raise ValueError("knowledge_propagation_grandfather_locator_mismatch")
    return record


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
    )


class CommunitySqlAlchemyKnowledgePropagationStore:
    """SQLAlchemy implementation of both propagation and rejected-audit ports."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        if not callable(session_factory):
            raise TypeError("knowledge_propagation_session_factory_invalid")
        self._session_factory = session_factory

    async def _load_target(
        self,
        context: Any,
        target: KnowledgeTargetKey,
    ) -> Spec | Card:
        if target.target_type is KnowledgeTargetType.SPEC:
            row = (
                await context.execute(
                    select(Spec).where(
                        Spec.id == target.target_id,
                        Spec.board_id == target.board_id,
                    )
                )
            ).scalar_one_or_none()
        else:
            row = (
                await context.execute(
                    select(Card).where(
                        Card.id == target.target_id,
                        Card.board_id == target.board_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_target_not_found",
                "knowledge propagation target does not exist in the requested board",
                details=target.to_dict(),
            )
        return row

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

    async def _legacy_attachments(
        self,
        context: Any,
        *,
        target: KnowledgeTargetKey,
        target_row: Spec | Card,
    ) -> tuple[KnowledgeLegacyAttachment, ...]:
        grandfathered = await self._latest_grandfather_classifications(
            context,
            target,
        )
        if target.target_type is KnowledgeTargetType.SPEC:
            rows = (
                (
                    await context.execute(
                        select(SpecKnowledgeBase)
                        .where(SpecKnowledgeBase.spec_id == target.target_id)
                        .order_by(
                            SpecKnowledgeBase.created_at.asc(),
                            SpecKnowledgeBase.id.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return tuple(
                _legacy_attachment(
                    row,
                    _physical_grandfather_record(
                        grandfathered,
                        row,
                        storage_kind="entity_row",
                        table="spec_knowledge_bases",
                        owner_id=target.target_id,
                    ),
                )
                for row in rows
            )

        values = target_row.knowledge_bases
        if values is None:
            values = []
        if not isinstance(values, list) or any(
            not isinstance(item, Mapping) for item in values
        ):
            raise KnowledgePropagationPortError(
                "knowledge_propagation_legacy_payload_corrupt",
                "card knowledge_bases must contain only JSON objects",
                details=target.to_dict(),
            )
        return tuple(
            sorted(
                (
                    _legacy_attachment(
                        item,
                        _physical_grandfather_record(
                            grandfathered,
                            item,
                            storage_kind="card_json",
                            table="cards",
                            owner_id=target.target_id,
                        ),
                    )
                    for item in values
                ),
                key=lambda item: item.source_knowledge_id,
            )
        )

    async def _latest_grandfather_classifications(
        self,
        context: Any,
        target: KnowledgeTargetKey,
    ) -> dict[str, Mapping[str, object]]:
        """Select the highest-revision canonical grandfather classification.

        Resumable backfills may legitimately append multiple grandfather
        ledgers. ``recorded_at`` is not an authority boundary: the scope
        revision is. Duplicate rows at that revision are tolerated only when
        their canonical attachment classifications are identical.
        """

        predicates = (
            *_target_predicates(KnowledgeMutationLedgerRecord, target),
            KnowledgeMutationLedgerRecord.operation_kind
            == KnowledgeMutationKind.GRANDFATHER.value,
            KnowledgeMutationLedgerRecord.outcome
            == KnowledgeMutationOutcome.GRANDFATHERED.value,
        )
        latest_revision = await context.scalar(
            select(func.max(KnowledgeMutationLedgerRecord.revision)).where(*predicates)
        )
        if latest_revision is None:
            return {}
        rows = (
            (
                await context.execute(
                    select(KnowledgeMutationLedgerRecord)
                    .where(
                        *predicates,
                        KnowledgeMutationLedgerRecord.revision == int(latest_revision),
                    )
                    .order_by(
                        KnowledgeMutationLedgerRecord.operation_id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        parsed: list[
            tuple[
                KnowledgeMutationLedgerRecord,
                dict[str, Mapping[str, object]],
            ]
        ] = []
        for row in rows:
            try:
                classifications = _grandfathered_classifications(row.details)
            except (TypeError, ValueError) as exc:
                raise KnowledgePropagationPortError(
                    "knowledge_propagation_grandfather_ledger_corrupt",
                    "the canonical grandfather ledger cannot be reconstructed",
                    details={
                        **target.to_dict(),
                        "revision": int(latest_revision),
                        "operation_id": str(row.operation_id),
                    },
                ) from exc
            parsed.append((row, classifications))
        if not parsed:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_grandfather_ledger_corrupt",
                "the canonical grandfather revision has no ledger row",
                details={
                    **target.to_dict(),
                    "revision": int(latest_revision),
                },
            )
        canonical = parsed[0][1]
        if any(classifications != canonical for _, classifications in parsed[1:]):
            raise KnowledgePropagationPortError(
                "knowledge_propagation_grandfather_ledger_conflict",
                "canonical grandfather ledgers conflict at the same scope revision",
                details={
                    **target.to_dict(),
                    "revision": int(latest_revision),
                    "operation_ids": [str(row.operation_id) for row, _ in parsed],
                },
            )
        return canonical

    async def _selectable_sources(
        self,
        context: Any,
        *,
        target: KnowledgeTargetKey,
        target_row: Spec | Card,
        requested_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSelectableSource, ...]:
        """Resolve only the target's legitimate immediate-parent KB set."""

        if not requested_ids:
            return ()
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
        rows = (
            (
                await context.execute(
                    statement.where(
                        *parent_predicates,
                        model.id.in_(requested_ids),
                    ).order_by(model.id.asc())
                )
            )
            .scalars()
            .all()
        )
        by_id = {str(row.id): row for row in rows}
        return tuple(
            _selectable_source(requested_id, by_id[requested_id])
            for requested_id in requested_ids
            if requested_id in by_id
        )

    async def load_grandfather_inventory(
        self,
        context: Any,
        target: KnowledgeTargetKey,
    ) -> tuple[KnowledgeGrandfatherAttachment, ...]:
        """Build the complete conservative inventory for resumable backfill.

        This read never mutates the historical entity row/card JSON. Revision
        and hash evidence remain nullable exactly as stored. Canonical bytes
        are computed only to classify divergence.
        """

        if not isinstance(target, KnowledgeTargetKey):
            raise TypeError("knowledge_propagation_grandfather_target_invalid")
        try:
            target_row = await self._load_target(context, target)
            if target.target_type is KnowledgeTargetType.SPEC:
                physical_items: tuple[object, ...] = tuple(
                    (
                        await context.execute(
                            select(SpecKnowledgeBase)
                            .where(SpecKnowledgeBase.spec_id == target.target_id)
                            .order_by(
                                SpecKnowledgeBase.created_at.asc(),
                                SpecKnowledgeBase.id.asc(),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                storage_kind = "entity_row"
                table = "spec_knowledge_bases"
            else:
                raw_items = target_row.knowledge_bases
                if raw_items is None:
                    raw_items = []
                if not isinstance(raw_items, list) or any(
                    not isinstance(item, Mapping) for item in raw_items
                ):
                    raise KnowledgePropagationPortError(
                        "knowledge_propagation_legacy_payload_corrupt",
                        "card knowledge_bases must contain only JSON objects",
                        details=target.to_dict(),
                    )
                physical_items = tuple(raw_items)
                storage_kind = "card_json"
                table = "cards"

            parent_ids = tuple(
                sorted(
                    {
                        str(parent_id)
                        for item in physical_items
                        if (
                            parent_id := _first_kb_value(
                                item,
                                "immediate_parent_kb_id",
                                "source_kb_id",
                            )
                        )
                        not in (None, "")
                    }
                )
            )
            resolved_parents = await self._selectable_sources(
                context,
                target=target,
                target_row=target_row,
                requested_ids=parent_ids,
            )
            available_parent_ids = {
                item.requested_knowledge_id for item in resolved_parents
            }

            inventory: list[KnowledgeGrandfatherAttachment] = []
            for item in physical_items:
                source_id = _kb_identity(item)
                parent_id = _first_kb_value(
                    item,
                    "immediate_parent_kb_id",
                    "source_kb_id",
                )
                parent_text = None if parent_id in (None, "") else str(parent_id)
                _content_bytes, physical_hash = _kb_content(item)
                persisted_hash = _first_kb_value(
                    item,
                    "source_content_sha256",
                    "content_hash",
                )
                lineage_status = str(
                    _kb_value(item, "lineage_status")
                    or _kb_value(item, "origin_resolution")
                    or ""
                ).strip()
                inventory.append(
                    KnowledgeGrandfatherAttachment(
                        source_knowledge_id=source_id,
                        revision_stamp=_legacy_kb_stamp(item),
                        evidence=KnowledgeGrandfatherEvidence(
                            # No pre-v2 typed selection store exists. Generic
                            # JSON/source lineage is not sufficient proof.
                            durable_selection_evidence=False,
                            origin_missing=(
                                lineage_status == "missing"
                                or (
                                    parent_text is not None
                                    and parent_text not in available_parent_ids
                                )
                            ),
                            origin_cycle=(
                                lineage_status == "cycle"
                                or (
                                    parent_text is not None and parent_text == source_id
                                )
                            ),
                            content_divergent=(
                                lineage_status == "divergent"
                                or (
                                    persisted_hash not in (None, "")
                                    and str(persisted_hash) != physical_hash
                                )
                            ),
                        ),
                        physical_locator={
                            "storage_kind": storage_kind,
                            "table": table,
                            "owner_id": target.target_id,
                            "attachment_id": source_id,
                        },
                    )
                )
            return tuple(
                sorted(
                    inventory,
                    key=lambda item: (
                        item.source_knowledge_id,
                        item.physical_locator["storage_kind"],
                        item.physical_locator["table"],
                        item.physical_locator["owner_id"],
                        item.physical_locator["attachment_id"],
                    ),
                )
            )
        except KnowledgePropagationPortError:
            raise
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            raise KnowledgePropagationPortError(
                "knowledge_propagation_grandfather_inventory_failed",
                "legacy knowledge inventory could not be classified safely",
                details=target.to_dict(),
            ) from exc

    async def get_idempotency_entry(
        self,
        context: Any,
        request: KnowledgeIdempotencyLookup,
    ) -> KnowledgeMutationLedgerEntry | None:
        if not isinstance(request, KnowledgeIdempotencyLookup):
            raise TypeError("knowledge_propagation_idempotency_lookup_invalid")
        try:
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

            legacy = await self._legacy_attachments(
                context,
                target=request.target,
                target_row=target_row,
            )
            sources = await self._selectable_sources(
                context,
                target=request.target,
                target_row=target_row,
                requested_ids=tuple(sorted(source_ids)),
            )
            return KnowledgePropagationScope(
                target=request.target,
                scope_revision=0 if scope is None else int(scope.scope_revision),
                v2_active=False if scope is None else bool(scope.v2_active),
                selection_state=None if scope is None else scope.selection_state,
                assignments=assignments,
                tombstones=tombstones,
                snapshots=snapshots,
                legacy_attachments=legacy,
                sources=sources,
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
                    v2_active=plan.next_scope_v2_active,
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
                v2_active=plan.next_scope_v2_active,
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
            await self._load_target(context, plan.target)
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
            raise KnowledgePropagationPortError(
                "knowledge_propagation_constraint_conflict",
                "a concurrent or divergent mutation violated a durable invariant",
                details=plan.target.to_dict(),
            ) from exc
        except SQLAlchemyError as exc:
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


__all__ = ["CommunitySqlAlchemyKnowledgePropagationStore"]
