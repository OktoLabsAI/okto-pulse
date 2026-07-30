"""One-bump-per-UoW policy subject versioning for Community SQLAlchemy.

The policy evaluator consumes relational facts that are not all columns on the
subject row.  These listeners convert effective direct and relational changes
into stable subject-version fences while preserving caller-owned transaction
lifecycle.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import event, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .sqlalchemy_models import (
    ArchitectureDesign,
    Attachment,
    Board,
    Card,
    CardDependency,
    Ideation,
    IdeationKnowledgeBase,
    IdeationQAItem,
    PolicyWaiverEventRow,
    PolicyWaiverRow,
    Refinement,
    RefinementKnowledgeBase,
    Spec,
    SpecKnowledgeBase,
    Sprint,
)


_INSTALLED = False
_BUMPED_KEY = "policy_subject_versioning_bumped"
_COMMITTED_KEY = "policy_subject_versioning_committed_transactions"


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


def _active_transaction(session: Session) -> Any:
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("policy_subject_versioning_transaction_missing")
    return transaction


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
            semantic_changes = changed - {"policy_version", "updated_at"}
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
        return

    markers = _bumped_by_transaction(session)
    child_markers = markers.pop(transaction, set())
    committed = _committed_transactions(session)
    if transaction in committed and child_markers:
        # A committed SAVEPOINT remains part of the caller-owned UoW.  A
        # rolled-back SAVEPOINT must discard only the keys first introduced
        # inside it because their corresponding token updates were reverted.
        markers.setdefault(parent, set()).update(child_markers)
    committed.discard(transaction)


def install_policy_subject_versioning() -> None:
    """Install process-wide Session listeners exactly once."""

    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "after_commit", _mark_transaction_committed)
    event.listen(
        Session,
        "after_transaction_end",
        _finish_transaction_markers,
    )
    _INSTALLED = True


__all__ = [
    "install_policy_subject_versioning",
    "lock_policy_board",
]
