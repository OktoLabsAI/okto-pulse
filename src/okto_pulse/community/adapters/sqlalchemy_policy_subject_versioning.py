"""One-bump-per-UoW policy subject versioning for Community SQLAlchemy.

The policy evaluator consumes relational facts that are not all columns on the
subject row.  These listeners convert effective direct and relational changes
into stable subject-version fences while preserving caller-owned transaction
lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
import uuid

from sqlalchemy import event, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, object_session

from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from okto_pulse.core.domain.guideline_semantic_assessment import (
    LEGACY_UNKNOWN_SEMANTIC_EDITOR_ID,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256

from .sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureDiagramPayload,
    Attachment,
    Board,
    Card,
    CardDependency,
    Ideation,
    IdeationKnowledgeBase,
    IdeationQAItem,
    PolicyWaiverEventRow,
    PolicyWaiverRow,
    QAItem,
    Refinement,
    RefinementKnowledgeBase,
    RefinementQAItem,
    Spec,
    SpecKnowledgeBase,
    SpecQAItem,
    Sprint,
    SprintQAItem,
)


_INSTALLED = False
_GUARDS_INSTALLED = False
_BUMPED_KEY = "policy_subject_versioning_bumped"
_COMMITTED_KEY = "policy_subject_versioning_committed_transactions"
_SEMANTIC_BRIDGE_ENABLED_KEY = "semantic_subject_bridge_enabled"
_SEMANTIC_ACTOR_ID_KEY = "semantic_subject_bridge_actor_id"
_SEMANTIC_PENDING_KEY = "semantic_subject_bridge_pending"

_SEMANTIC_ENTITY_BY_MODEL: dict[type, str] = {
    Ideation: "ideation",
    Refinement: "refinement",
    Spec: "spec",
    Sprint: "sprint",
    Card: "card",
}

_CARD_NON_SEMANTIC_OPERATIONAL_FIELDS = frozenset(
    {
        "policy_version",
        "position",
        "updated_at",
    }
)


class CommunitySemanticSession(Session):
    """Synchronous Session owned by the Community semantic composition root.

    ``AsyncSession`` delegates ORM work to a synchronous Session instance.  A
    dedicated class gives the Community edition a stable event target without
    mutating ``sqlalchemy.orm.Session`` process-wide.
    """


_PROTECTED_SEMANTIC_MODELS: tuple[type, ...] = (
    Ideation,
    Refinement,
    Spec,
    Sprint,
    Card,
    CardDependency,
    Attachment,
    ArchitectureDesign,
    ArchitectureDiagramPayload,
    IdeationQAItem,
    RefinementQAItem,
    SpecQAItem,
    SprintQAItem,
    QAItem,
    IdeationKnowledgeBase,
    RefinementKnowledgeBase,
    SpecKnowledgeBase,
    PolicyWaiverRow,
    PolicyWaiverEventRow,
)


def _require_composed_semantic_session(
    _mapper: object,
    _connection: object,
    target: object,
) -> None:
    """Reject protected ORM DML outside the Community composition root."""

    session = object_session(target)
    if session is not None and not isinstance(session, CommunitySemanticSession):
        raise RuntimeError("semantic_subject_session_not_composed")


def _install_semantic_model_guards() -> None:
    """Install mapper guards once; mapper events do not contaminate Session."""

    global _GUARDS_INSTALLED
    if _GUARDS_INSTALLED:
        return
    for model in _PROTECTED_SEMANTIC_MODELS:
        for event_name in ("before_insert", "before_update", "before_delete"):
            if not event.contains(
                model,
                event_name,
                _require_composed_semantic_session,
            ):
                event.listen(model, event_name, _require_composed_semantic_session)
    _GUARDS_INSTALLED = True


def bind_semantic_subject_actor(
    session: AsyncSession,
    actor: ActorContext | None,
) -> bool:
    """Bind the authenticated UoW actor used by semantic mutation evidence.

    A missing actor is retained as an explicit unauthenticated state so reads
    remain available while a later semantic write can fail closed at commit.
    The return value is ``True`` only when this call owns a new binding.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("semantic_subject_bridge_session_invalid")
    if actor is not None and not isinstance(actor, ActorContext):
        raise TypeError("semantic_subject_bridge_actor_invalid")
    actor_id = (
        actor.actor_id.strip()
        if actor is not None and isinstance(actor.actor_id, str)
        else ""
    )
    if actor is not None and not actor_id:
        raise ValueError("semantic_subject_bridge_actor_id_required")
    if actor_id == LEGACY_UNKNOWN_SEMANTIC_EDITOR_ID:
        raise ValueError("semantic_subject_bridge_actor_id_reserved")
    sync_session = session.sync_session
    if sync_session.info.get(_SEMANTIC_BRIDGE_ENABLED_KEY):
        current_actor_id = sync_session.info.get(_SEMANTIC_ACTOR_ID_KEY)
        requested_actor_id = actor_id or None
        if current_actor_id != requested_actor_id:
            raise RuntimeError("semantic_subject_bridge_actor_conflict")
        return False
    if sync_session.info.get(_SEMANTIC_PENDING_KEY):
        raise RuntimeError("semantic_subject_bridge_pending_without_actor")
    sync_session.info[_SEMANTIC_BRIDGE_ENABLED_KEY] = True
    if actor_id:
        sync_session.info[_SEMANTIC_ACTOR_ID_KEY] = actor_id
    else:
        sync_session.info.pop(_SEMANTIC_ACTOR_ID_KEY, None)
    return True


def unbind_semantic_subject_actor(session: AsyncSession) -> None:
    """Remove UoW-lifetime semantic bridge metadata from a closing session."""

    if not isinstance(session, AsyncSession):
        raise TypeError("semantic_subject_bridge_session_invalid")
    info = session.sync_session.info
    info.pop(_SEMANTIC_BRIDGE_ENABLED_KEY, None)
    info.pop(_SEMANTIC_ACTOR_ID_KEY, None)
    info.pop(_SEMANTIC_PENDING_KEY, None)


def _board_mutex_statement(*, dialect: str, board_id: str):
    if dialect == "sqlite":
        return (
            update(Board)
            .where(Board.id == board_id)
            .values(
                id=Board.id,
                updated_at=Board.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
    return select(Board.id).where(Board.id == board_id).with_for_update()


async def lock_policy_board(
    session: AsyncSession,
    *,
    board_id: str,
) -> None:
    """Acquire the policy-snapshot mutex for bulk or SQL-native writers."""

    if not isinstance(session, AsyncSession):
        raise TypeError("policy_board_mutex_session_invalid")
    normalized_board_id = board_id.strip() if isinstance(board_id, str) else ""
    if not normalized_board_id:
        raise ValueError("policy_board_mutex_board_id_required")
    dialect = session.get_bind().dialect.name
    result = await session.execute(
        _board_mutex_statement(
            dialect=dialect,
            board_id=normalized_board_id,
        )
    )
    found = (
        int(result.rowcount or 0) == 1
        if dialect == "sqlite"
        else result.scalar_one_or_none() is not None
    )
    if not found:
        raise ValueError("policy_board_mutex_board_not_found")


def _changed_attribute_names(instance: object) -> set[str]:
    state = inspect(instance)
    changed: set[str] = set()
    for attribute in state.mapper.column_attrs:
        history = state.attrs[attribute.key].history
        if not history.has_changes():
            continue
        if (
            history.added
            and history.deleted
            and tuple(history.added) == tuple(history.deleted)
        ):
            continue
        changed.add(attribute.key)
    return changed


def _history_values(instance: object, attribute_name: str) -> set[str]:
    history = inspect(instance).attrs[attribute_name].history
    values = {
        str(value) for value in (*history.added, *history.deleted) if value is not None
    }
    current = getattr(instance, attribute_name, None)
    if current is not None:
        values.add(str(current))
    return values


def _bumped_by_transaction(
    session: Session,
) -> dict[Any, set[tuple[str, str]]]:
    value = session.info.setdefault(_BUMPED_KEY, {})
    if not isinstance(value, dict):
        value = {}
        session.info[_BUMPED_KEY] = value
    return value


def _committed_transactions(session: Session) -> set[Any]:
    value = session.info.setdefault(_COMMITTED_KEY, set())
    if not isinstance(value, set):
        value = set()
        session.info[_COMMITTED_KEY] = value
    return value


def _semantic_pending_by_transaction(
    session: Session,
) -> dict[Any, set[tuple[str, str, str]]]:
    value = session.info.setdefault(_SEMANTIC_PENDING_KEY, {})
    if not isinstance(value, dict):
        value = {}
        session.info[_SEMANTIC_PENDING_KEY] = value
    return value


def _active_transaction(session: Session) -> Any:
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("policy_subject_versioning_transaction_missing")
    return transaction


def _active_semantic_targets(
    session: Session,
) -> set[tuple[str, str, str]]:
    targets: set[tuple[str, str, str]] = set()
    markers = _semantic_pending_by_transaction(session)
    transaction = _active_transaction(session)
    current = transaction
    while current is not None:
        targets.update(markers.get(current, set()))
        current = getattr(current, "parent", None)
    return targets


def _discard_active_semantic_targets(
    session: Session,
    targets: set[tuple[str, str, str]],
) -> None:
    markers = _semantic_pending_by_transaction(session)
    transaction = _active_transaction(session)
    current = transaction
    while current is not None:
        pending = markers.get(current)
        if pending is not None:
            pending.difference_update(targets)
        current = getattr(current, "parent", None)


def _queue_semantic_target(
    session: Session,
    *,
    entity_type: str,
    board_id: object,
    subject_id: object,
) -> None:
    if not session.info.get(_SEMANTIC_BRIDGE_ENABLED_KEY):
        return
    normalized_board_id = board_id.strip() if isinstance(board_id, str) else ""
    normalized_subject_id = subject_id.strip() if isinstance(subject_id, str) else ""
    if not normalized_board_id or not normalized_subject_id:
        return
    transaction = _active_transaction(session)
    _semantic_pending_by_transaction(session).setdefault(
        transaction,
        set(),
    ).add((entity_type, normalized_board_id, normalized_subject_id))


def queue_semantic_subject_mutation(
    session: AsyncSession,
    *,
    entity_type: PolicyEntityType,
    board_id: str,
    subject_id: str,
    expected_actor_id: str | None = None,
) -> None:
    """Queue one SQL-native subject mutation in the active Community UoW."""

    if not isinstance(session, AsyncSession):
        raise TypeError("semantic_subject_bridge_session_invalid")
    if not isinstance(entity_type, PolicyEntityType):
        raise TypeError("semantic_subject_bridge_entity_type_invalid")
    sync_session = session.sync_session
    if not sync_session.info.get(_SEMANTIC_BRIDGE_ENABLED_KEY):
        return
    if expected_actor_id is not None:
        normalized_expected = (
            expected_actor_id.strip() if isinstance(expected_actor_id, str) else ""
        )
        if not normalized_expected:
            raise ValueError("semantic_subject_bridge_expected_actor_required")
        if sync_session.info.get(_SEMANTIC_ACTOR_ID_KEY) != normalized_expected:
            raise RuntimeError("semantic_subject_bridge_actor_mismatch")
    _queue_semantic_target(
        sync_session,
        entity_type=entity_type.value,
        board_id=board_id,
        subject_id=subject_id,
    )


def _queue_model_targets(
    session: Session,
    *,
    model: type,
    identities: Iterable[str],
) -> None:
    entity_type = _SEMANTIC_ENTITY_BY_MODEL[model]
    for identity in sorted(set(identities)):
        with session.no_autoflush:
            row = session.get(model, identity)
        if row is None or row in session.deleted:
            continue
        _queue_semantic_target(
            session,
            entity_type=entity_type,
            board_id=getattr(row, "board_id", None),
            subject_id=getattr(row, "id", None),
        )


def _queue_scenarios_from_spec(session: Session, spec: Spec) -> None:
    for scenario in spec.test_scenarios or ():
        if not isinstance(scenario, dict):
            continue
        _queue_semantic_target(
            session,
            entity_type="test_scenario",
            board_id=spec.board_id,
            subject_id=scenario.get("id"),
        )


def _queue_spec_scenarios(session: Session, spec_ids: Iterable[str]) -> None:
    for spec_id in sorted(set(spec_ids)):
        with session.no_autoflush:
            spec = session.get(Spec, spec_id)
        if spec is None or spec in session.deleted:
            continue
        _queue_scenarios_from_spec(session, spec)


def _collect_architecture_owners(
    session: Session,
    *,
    design_ids: Iterable[str],
    direct_version_targets: dict[type, set[str]],
    card_ids: set[str],
) -> None:
    for design_id in sorted(set(design_ids)):
        with session.no_autoflush:
            design = session.get(ArchitectureDesign, design_id)
        if design is None or design in session.deleted:
            continue
        if design.ideation_id:
            direct_version_targets[Ideation].add(str(design.ideation_id))
        if design.refinement_id:
            direct_version_targets[Refinement].add(str(design.refinement_id))
        if design.spec_id:
            direct_version_targets[Spec].add(str(design.spec_id))
        if design.card_id:
            card_ids.add(str(design.card_id))


def _was_bumped(
    session: Session,
    *,
    transaction: Any,
    key: tuple[str, str],
) -> bool:
    markers = _bumped_by_transaction(session)
    current = transaction
    while current is not None:
        if key in markers.get(current, set()):
            return True
        current = getattr(current, "parent", None)
    return False


def _bump(
    session: Session,
    *,
    model: type,
    identity: str,
    field_name: str,
) -> None:
    key = (model.__tablename__, identity)
    transaction = _active_transaction(session)
    if _was_bumped(session, transaction=transaction, key=key):
        return
    with session.no_autoflush:
        row = session.get(model, identity)
    # A brand-new aggregate has no prior policy snapshot to invalidate.  Its
    # initial relational facts are already represented by version 1, whose
    # Python default is not necessarily materialized until INSERT.
    if row is None or row in session.new or row in session.deleted:
        return
    state = inspect(row)
    field_history = state.attrs[field_name].history
    # A service that already advanced the same token owns the exact value.
    if not field_history.has_changes():
        setattr(row, field_name, int(getattr(row, field_name)) + 1)
    _bumped_by_transaction(session).setdefault(transaction, set()).add(key)


def _bump_many(
    session: Session,
    *,
    model: type,
    identities: Iterable[str],
    field_name: str,
) -> None:
    for identity in sorted(set(identities)):
        _bump(
            session,
            model=model,
            identity=identity,
            field_name=field_name,
        )


def _before_flush(
    session: Session,
    _flush_context: object,
    _instances: object,
) -> None:
    direct_version_targets: dict[type, set[str]] = {
        Ideation: set(),
        Refinement: set(),
        Spec: set(),
        Sprint: set(),
    }
    card_ids: set[str] = set()
    sprint_ids: set[str] = set()
    scenario_spec_ids: set[str] = set()

    for instance in tuple(session.dirty):
        changed = _changed_attribute_names(instance)
        if not changed:
            continue
        if isinstance(instance, (Ideation, Refinement, Spec, Sprint)):
            semantic_changes = changed - {
                "version",
                "updated_at",
                "test_scenario_policy_epoch",
            }
            if semantic_changes and instance.id:
                direct_version_targets[type(instance)].add(instance.id)
        if isinstance(instance, Spec):
            if "test_scenarios" in changed and instance.id:
                scenario_spec_ids.add(instance.id)
        if isinstance(instance, Card):
            semantic_changes = changed - _CARD_NON_SEMANTIC_OPERATIONAL_FIELDS
            if semantic_changes and instance.id:
                card_ids.add(instance.id)
            if "status" in changed and instance.id:
                with session.no_autoflush:
                    dependent_ids = session.execute(
                        select(CardDependency.card_id).where(
                            CardDependency.depends_on_id == instance.id
                        )
                    ).scalars()
                    card_ids.update(str(value) for value in dependent_ids)
            if {"status", "sprint_id", "test_scenario_ids"} & changed:
                sprint_ids.update(_history_values(instance, "sprint_id"))
            if {"spec_id", "test_scenario_ids"} & changed:
                scenario_spec_ids.update(_history_values(instance, "spec_id"))

    for instance in tuple(session.new) + tuple(session.deleted):
        if isinstance(instance, Card):
            if instance.id:
                card_ids.add(str(instance.id))
            if instance.sprint_id:
                sprint_ids.add(str(instance.sprint_id))
            if instance.spec_id and instance.test_scenario_ids:
                scenario_spec_ids.add(str(instance.spec_id))

    for relation in (
        *tuple(session.new),
        *tuple(session.dirty),
        *tuple(session.deleted),
    ):
        if isinstance(relation, CardDependency):
            card_ids.update(_history_values(relation, "card_id"))
        elif isinstance(relation, Attachment):
            card_ids.update(_history_values(relation, "card_id"))
        elif isinstance(relation, ArchitectureDesign):
            if relation in session.dirty and not _changed_attribute_names(relation):
                continue
            direct_version_targets[Ideation].update(
                _history_values(relation, "ideation_id")
            )
            direct_version_targets[Refinement].update(
                _history_values(relation, "refinement_id")
            )
            direct_version_targets[Spec].update(_history_values(relation, "spec_id"))
            card_ids.update(_history_values(relation, "card_id"))
        elif isinstance(relation, IdeationQAItem):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at"}
            ):
                continue
            direct_version_targets[Ideation].update(
                _history_values(relation, "ideation_id")
            )
        elif isinstance(relation, RefinementQAItem):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at"}
            ):
                continue
            direct_version_targets[Refinement].update(
                _history_values(relation, "refinement_id")
            )
        elif isinstance(relation, SpecQAItem):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at"}
            ):
                continue
            direct_version_targets[Spec].update(_history_values(relation, "spec_id"))
        elif isinstance(relation, SprintQAItem):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at"}
            ):
                continue
            direct_version_targets[Sprint].update(
                _history_values(relation, "sprint_id")
            )
        elif isinstance(relation, QAItem):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at"}
            ):
                continue
            card_ids.update(_history_values(relation, "card_id"))
        elif isinstance(relation, IdeationKnowledgeBase):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at", "updated_at"}
            ):
                continue
            direct_version_targets[Ideation].update(
                _history_values(relation, "ideation_id")
            )
        elif isinstance(relation, RefinementKnowledgeBase):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at", "updated_at"}
            ):
                continue
            direct_version_targets[Refinement].update(
                _history_values(relation, "refinement_id")
            )
        elif isinstance(relation, SpecKnowledgeBase):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at", "updated_at"}
            ):
                continue
            direct_version_targets[Spec].update(_history_values(relation, "spec_id"))
        elif isinstance(relation, ArchitectureDiagramPayload):
            if relation in session.dirty and not (
                _changed_attribute_names(relation) - {"created_at"}
            ):
                continue
            _collect_architecture_owners(
                session,
                design_ids=_history_values(relation, "design_id"),
                direct_version_targets=direct_version_targets,
                card_ids=card_ids,
            )

    _bump_many(
        session,
        model=Card,
        identities=card_ids,
        field_name="policy_version",
    )
    _bump_many(
        session,
        model=Spec,
        identities=scenario_spec_ids,
        field_name="test_scenario_policy_epoch",
    )
    direct_version_targets[Sprint].update(sprint_ids)
    for model, identities in direct_version_targets.items():
        _bump_many(
            session,
            model=model,
            identities=identities,
            field_name="version",
        )

    _queue_model_targets(
        session,
        model=Card,
        identities=card_ids,
    )
    _queue_spec_scenarios(session, scenario_spec_ids)
    for model, identities in direct_version_targets.items():
        _queue_model_targets(
            session,
            model=model,
            identities=identities,
        )

    # Preview/adoption holds this same board-row mutex while taking its
    # artifact and waiver snapshots.  Acquiring it here, after relational
    # changes have materialized their owning subject-version bump, prevents a
    # PostgreSQL writer from committing between that snapshot and the policy
    # mutation.  SQLite already serializes writers; the query is harmless
    # there.  Sorting avoids cross-board deadlocks in batch UoWs.
    fence_types = (
        Ideation,
        Refinement,
        Spec,
        Sprint,
        Card,
        PolicyWaiverRow,
        PolicyWaiverEventRow,
    )
    board_ids = {
        str(instance.board_id)
        for instance in (
            *tuple(session.new),
            *tuple(session.dirty),
            *tuple(session.deleted),
        )
        if isinstance(instance, fence_types) and getattr(instance, "board_id", None)
    }
    dialect = session.get_bind().dialect.name
    for board_id in sorted(board_ids):
        with session.no_autoflush:
            session.execute(
                _board_mutex_statement(
                    dialect=dialect,
                    board_id=board_id,
                )
            )


def _after_flush_collect_new_subjects(
    session: Session,
    _flush_context: object,
) -> None:
    if not session.info.get(_SEMANTIC_BRIDGE_ENABLED_KEY):
        return
    for instance in tuple(session.new):
        entity_type = _SEMANTIC_ENTITY_BY_MODEL.get(type(instance))
        if entity_type is None:
            continue
        _queue_semantic_target(
            session,
            entity_type=entity_type,
            board_id=getattr(instance, "board_id", None),
            subject_id=getattr(instance, "id", None),
        )
        if isinstance(instance, Spec):
            _queue_scenarios_from_spec(session, instance)


async def materialize_pending_semantic_subject_mutations(
    session: AsyncSession,
) -> None:
    """Persist queued semantic editor heads/events in the caller transaction."""

    if not isinstance(session, AsyncSession):
        raise TypeError("semantic_subject_bridge_session_invalid")
    sync_session = session.sync_session
    if not sync_session.info.get(_SEMANTIC_BRIDGE_ENABLED_KEY):
        return
    targets = _active_semantic_targets(sync_session)
    if not targets:
        return
    actor_id = sync_session.info.get(_SEMANTIC_ACTOR_ID_KEY)
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise RuntimeError("semantic_subject_mutation_actor_required")

    # Imported lazily because the assessment adapter uses this module's board
    # mutex.  The bridge remains a Community transaction concern and does not
    # introduce a second application-service or Core port.
    from .sqlalchemy_semantic_guideline_assessment import (
        CommunitySqlAlchemySemanticGuidelineAssessment,
    )

    adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
    changed_at = datetime.now(timezone.utc)
    batch_id = uuid.uuid4().hex
    for entity_value, board_id, subject_id in sorted(targets):
        entity_type = PolicyEntityType(entity_value)
        idempotency_key = f"semantic-subject-uow:{batch_id}:{entity_value}:{subject_id}"
        request_digest = canonical_sha256(
            {
                "contract": "semantic-subject-uow-mutation/v1",
                "board_id": board_id,
                "subject_type": entity_value,
                "subject_id": subject_id,
                "actor_id": actor_id,
                "changed_at": changed_at.isoformat(),
                "idempotency_key": idempotency_key,
            }
        )
        await adapter.record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            changed_at=changed_at,
        )
    _discard_active_semantic_targets(sync_session, targets)


def _mark_transaction_committed(session: Session) -> None:
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is not None:
        _committed_transactions(session).add(transaction)


def _finish_transaction_markers(
    session: Session,
    transaction: Any,
) -> None:
    parent = getattr(transaction, "parent", None)
    if parent is None:
        session.info.pop(_BUMPED_KEY, None)
        session.info.pop(_COMMITTED_KEY, None)
        session.info.pop(_SEMANTIC_PENDING_KEY, None)
        return

    markers = _bumped_by_transaction(session)
    child_markers = markers.pop(transaction, set())
    semantic_markers = _semantic_pending_by_transaction(session)
    child_semantic_markers = semantic_markers.pop(transaction, set())
    committed = _committed_transactions(session)
    if transaction in committed and child_markers:
        # A committed SAVEPOINT remains part of the caller-owned UoW.  A
        # rolled-back SAVEPOINT must discard only the keys first introduced
        # inside it because their corresponding token updates were reverted.
        markers.setdefault(parent, set()).update(child_markers)
    if transaction in committed and child_semantic_markers:
        semantic_markers.setdefault(parent, set()).update(child_semantic_markers)
    committed.discard(transaction)


def install_policy_subject_versioning() -> None:
    """Install listeners on the Community Session class exactly once."""

    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(CommunitySemanticSession, "before_flush", _before_flush)
    event.listen(
        CommunitySemanticSession,
        "after_flush",
        _after_flush_collect_new_subjects,
    )
    event.listen(
        CommunitySemanticSession,
        "after_commit",
        _mark_transaction_committed,
    )
    event.listen(
        CommunitySemanticSession,
        "after_transaction_end",
        _finish_transaction_markers,
    )
    _INSTALLED = True


# Mapper guards and subclass listeners are deterministic at import time.  They
# do not attach any callback to sqlalchemy.orm.Session itself.
_install_semantic_model_guards()
install_policy_subject_versioning()


__all__ = [
    "CommunitySemanticSession",
    "bind_semantic_subject_actor",
    "install_policy_subject_versioning",
    "lock_policy_board",
    "materialize_pending_semantic_subject_mutations",
    "queue_semantic_subject_mutation",
    "unbind_semantic_subject_actor",
]
