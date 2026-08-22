"""Community SQLAlchemy adapter for Core application records."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import Any, AsyncIterator, Mapping
from weakref import WeakKeyDictionary, WeakSet

from sqlalchemy import and_, event, false, func, or_, select, true, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from okto_pulse.community.adapters import sqlalchemy_models as models
from okto_pulse.community.adapters.permission_policy import (
    direct_permission_review,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    is_knowledge_creation_race_error,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    materialize_pending_semantic_subject_mutations,
)
from okto_pulse.community.sql_like import SQL_LIKE_ESCAPE
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    ApplicationGroupCount,
    ApplicationGroupCountQuery,
    ApplicationQuery,
    ApplicationRecord,
    ApplicationRecordConflictError,
)
from okto_pulse.core.ports.permission_policy import (
    PermissionPresetLineageNode,
    legacy_permissions_to_flags,
    resolve_effective_permissions,
    resolve_preset_lineage,
)
from okto_pulse.core.domain.ownership import aggregate_ownership
from okto_pulse.core.domain.realm import (
    RealmIsolationViolation,
    RealmScope,
    require_realm_scope,
)


_ENTITY_CLASSES = {
    "activity_log": models.ActivityLog,
    "agent": models.Agent,
    "agent_board": models.AgentBoard,
    "agent_seen_item": models.AgentSeenItem,
    "amendment_hotfix_revision": models.AmendmentHotfixRevision,
    "architecture_design": models.ArchitectureDesign,
    "attachment": models.Attachment,
    "board": models.Board,
    "board_guideline": models.BoardGuideline,
    "board_share": models.BoardShare,
    "card": models.Card,
    "card_dependency": models.CardDependency,
    "comment": models.Comment,
    "guideline": models.Guideline,
    "ideation": models.Ideation,
    "ideation_history": models.IdeationHistory,
    "ideation_knowledge_base": models.IdeationKnowledgeBase,
    "ideation_qa_item": models.IdeationQAItem,
    "ideation_snapshot": models.IdeationSnapshot,
    "permission_preset": models.PermissionPreset,
    "qa_item": models.QAItem,
    "refinement": models.Refinement,
    "refinement_history": models.RefinementHistory,
    "refinement_knowledge_base": models.RefinementKnowledgeBase,
    "refinement_qa_item": models.RefinementQAItem,
    "refinement_snapshot": models.RefinementSnapshot,
    "spec": models.Spec,
    "spec_dependency": models.SpecDependency,
    "spec_dependency_operation": models.SpecDependencyOperation,
    "spec_history": models.SpecHistory,
    "spec_knowledge_base": models.SpecKnowledgeBase,
    "spec_qa_item": models.SpecQAItem,
    "sprint": models.Sprint,
    "sprint_history": models.SprintHistory,
    "sprint_qa_item": models.SprintQAItem,
    "story": models.Story,
    "story_ideation_link": models.StoryIdeationLink,
    "topic": models.Topic,
}
_CLASS_ENTITIES = {value: key for key, value in _ENTITY_CLASSES.items()}

_DIRECT_COMMIT_RECORDS_KEY = "okto_pulse.application_persistence.direct_commit_records"
_DIRECT_COMMIT_LISTENER_KEY = (
    "okto_pulse.application_persistence.direct_commit_listener"
)


def _synchronize_records_before_direct_commit(sync_session: Any) -> None:
    """Preserve detached-record semantics for legacy raw-session commits."""

    entries = sync_session.info.get(_DIRECT_COMMIT_RECORDS_KEY, ())
    for record, row in tuple(entries):
        if not record.dirty_fields:
            continue
        if row not in sync_session or row in sync_session.deleted:
            continue
        for field_name in tuple(record.dirty_fields):
            setattr(row, field_name, copy.deepcopy(record.values[field_name]))
        record.dirty_fields.clear()


def _register_direct_commit_record(
    context: Any,
    record: ApplicationRecord,
    row: Any,
) -> None:
    sync_session = context.sync_session
    entries = sync_session.info.setdefault(_DIRECT_COMMIT_RECORDS_KEY, [])
    if all(existing is not record for existing, _ in entries):
        entries.append((record, row))
    if not sync_session.info.get(_DIRECT_COMMIT_LISTENER_KEY):
        event.listen(
            sync_session,
            "before_commit",
            _synchronize_records_before_direct_commit,
        )
        sync_session.info[_DIRECT_COMMIT_LISTENER_KEY] = True


_PARENT_SCOPE_PATHS: dict[str, tuple[str, str]] = {
    "attachment": ("card_id", "card"),
    "card_dependency": ("card_id", "card"),
    "comment": ("card_id", "card"),
    "ideation_history": ("ideation_id", "ideation"),
    "ideation_knowledge_base": ("ideation_id", "ideation"),
    "ideation_qa_item": ("ideation_id", "ideation"),
    "ideation_snapshot": ("ideation_id", "ideation"),
    "qa_item": ("card_id", "card"),
    "refinement_history": ("refinement_id", "refinement"),
    "refinement_knowledge_base": ("refinement_id", "refinement"),
    "refinement_qa_item": ("refinement_id", "refinement"),
    "refinement_snapshot": ("refinement_id", "refinement"),
    "spec_history": ("spec_id", "spec"),
    "spec_knowledge_base": ("spec_id", "spec"),
    "spec_qa_item": ("spec_id", "spec"),
    "sprint_history": ("sprint_id", "sprint"),
    "sprint_qa_item": ("sprint_id", "sprint"),
}


def _model(entity: str):
    try:
        return _ENTITY_CLASSES[entity]
    except KeyError as exc:
        raise ValueError(f"unsupported_application_entity:{entity}") from exc


def _realm_scope(context: Any, entity: str) -> RealmScope | None:
    if aggregate_ownership(entity) == "global":
        return None
    info = getattr(context, "info", None) or {}
    return require_realm_scope(info.get("realm_scope"))


def _realm_predicate(entity: str, scope: RealmScope):
    """Correlated-EXISTS realm isolation predicate.

    The EXISTS form (instead of ``board_id IN (SELECT …)``) keeps the outer
    query free to walk the pagination covering indexes in order — the IN
    subquery variant made SQLite fall back to USE TEMP B-TREE FOR ORDER BY
    on the card page and lookup shapes (C3 gate repro, AC13/TR2).
    """
    model = _model(entity)
    if entity == "board":
        return model.realm_id == scope.realm_id
    if hasattr(model, "board_id"):
        return (
            select(models.Board.id)
            .where(
                models.Board.id == model.board_id,
                models.Board.realm_id == scope.realm_id,
            )
            .exists()
        )
    parent_path = _PARENT_SCOPE_PATHS.get(entity)
    if parent_path is None:
        raise RuntimeError(f"tenant_scope_path_not_configured:{entity}")
    foreign_key, parent_entity = parent_path
    parent_model = _model(parent_entity)
    return (
        select(parent_model.id)
        .where(
            parent_model.id == getattr(model, foreign_key),
            _realm_predicate(parent_entity, scope),
        )
        .exists()
    )


def _predicate(model: Any, item: ApplicationFilter):
    # Relational predicates (FR2): virtual fields resolved SERVER-SIDE as
    # correlated EXISTS — never loaded collections filtered post-fetch.
    if model is models.Card and item.field == "conclusion_actor_id":
        if item.operator != "eq" or not str(item.value or "").strip():
            raise ValueError(f"unsupported_application_operator:{item.operator}")
        entries = func.json_each(
            func.coalesce(models.Card.conclusions, "[]")
        ).table_valued("key", "value", joins_implicitly=True)
        actor_id = func.coalesce(
            func.json_extract(entries.c.value, "$.author_id"),
            func.json_extract(entries.c.value, "$.actor_id"),
            func.json_extract(entries.c.value, "$.author_agent_id"),
            func.json_extract(entries.c.value, "$.author"),
            func.json_extract(entries.c.value, "$.created_by"),
        )
        return (
            select(1)
            .select_from(entries)
            .where(actor_id == str(item.value))
            .correlate(models.Card)
            .exists()
        )
    if model is models.Spec and item.field in {
        "linked_to_cards",
        "linked_to_active_cards",
    }:
        predicates = [
            models.Card.board_id == models.Spec.board_id,
            models.Card.spec_id == models.Spec.id,
        ]
        if item.field == "linked_to_active_cards":
            predicates.append(models.Card.archived == false())
        link_exists = select(models.Card.id).where(*predicates).exists()
        if item.operator == "is_true" or (item.operator == "eq" and item.value is True):
            return link_exists
        if item.operator == "is_false" or (
            item.operator == "eq" and item.value is False
        ):
            return ~link_exists
        raise ValueError(f"unsupported_application_operator:{item.operator}")
    if model is models.Ideation and item.field == "derivation_pending":
        active_refinement_exists = (
            select(models.Refinement.id)
            .where(
                models.Refinement.board_id == models.Ideation.board_id,
                models.Refinement.ideation_id == models.Ideation.id,
                models.Refinement.archived == false(),
                models.Refinement.status != "cancelled",
            )
            .exists()
        )
        active_direct_spec_exists = (
            select(models.Spec.id)
            .where(
                models.Spec.board_id == models.Ideation.board_id,
                models.Spec.ideation_id == models.Ideation.id,
                models.Spec.refinement_id.is_(None),
                models.Spec.archived == false(),
                models.Spec.status != "cancelled",
            )
            .exists()
        )
        pending = func.coalesce(
            and_(
                models.Ideation.status == "done",
                or_(
                    and_(
                        models.Ideation.complexity.in_(("medium", "large")),
                        ~active_refinement_exists,
                    ),
                    and_(
                        models.Ideation.complexity == "small",
                        ~active_direct_spec_exists,
                    ),
                ),
            ),
            false(),
        )
        if item.operator == "is_true" or (item.operator == "eq" and item.value is True):
            return pending
        if item.operator == "is_false" or (
            item.operator == "eq" and item.value is False
        ):
            return ~pending
        raise ValueError(f"unsupported_application_operator:{item.operator}")
    if model is models.Refinement and item.field == "derivation_pending":
        active_spec_exists = (
            select(models.Spec.id)
            .where(
                models.Spec.board_id == models.Refinement.board_id,
                models.Spec.refinement_id == models.Refinement.id,
                models.Spec.archived == false(),
                models.Spec.status != "cancelled",
            )
            .exists()
        )
        pending = and_(
            models.Refinement.status == "done",
            ~active_spec_exists,
        )
        if item.operator == "is_true" or (item.operator == "eq" and item.value is True):
            return pending
        if item.operator == "is_false" or (
            item.operator == "eq" and item.value is False
        ):
            return ~pending
        raise ValueError(f"unsupported_application_operator:{item.operator}")
    if model is models.Story and item.field == "linked":
        link_exists = (
            select(models.StoryIdeationLink.id)
            .where(models.StoryIdeationLink.story_id == models.Story.id)
            .exists()
        )
        if item.operator == "is_true" or (item.operator == "eq" and item.value is True):
            return link_exists
        if item.operator == "is_false" or (
            item.operator == "eq" and item.value is False
        ):
            return ~link_exists
        raise ValueError(f"unsupported_application_operator:{item.operator}")
    if model is models.Story and item.field == "converted":
        is_converted = models.Story.status == "converted"
        if item.operator == "is_true" or (item.operator == "eq" and item.value is True):
            return is_converted
        if item.operator == "is_false" or (
            item.operator == "eq" and item.value is False
        ):
            return ~is_converted
        raise ValueError(f"unsupported_application_operator:{item.operator}")
    if model is models.Refinement and item.field == "ideation_title":
        column = (
            select(models.Ideation.title)
            .where(models.Ideation.id == models.Refinement.ideation_id)
            .correlate(models.Refinement)
            .scalar_subquery()
        )
    else:
        column = getattr(model, item.field)
    value: Any = item.value
    # SQLite's CURRENT_TIMESTAMP stores second precision without a fractional
    # suffix, while SQLAlchemy datetime binds include ``.000000``. Lexical
    # comparisons therefore redeliver the cursor anchor. Community owns this
    # concrete normalization; Core only supplies the timestamp+id contract.
    if (
        model is models.ActivityLog
        and item.field == "created_at"
        and item.operator
        in {
            "eq",
            "ne",
            "gte",
            "lte",
            "gt",
            "lt",
        }
    ):
        column = func.julianday(column)
        value = func.julianday(item.value)
    if item.operator == "eq":
        return column == value
    if item.operator == "ne":
        return column != value
    if item.operator == "in":
        return column.in_(tuple(item.value))
    if item.operator == "not_in":
        return column.notin_(tuple(item.value))
    if item.operator == "gte":
        return column >= value
    if item.operator == "lte":
        return column <= value
    if item.operator == "gt":
        return column > value
    if item.operator == "lt":
        return column < value
    if item.operator == "is_true":
        # Equality instead of IS: SQLite refuses to keep walking a covering
        # index past an ``archived IS 0`` predicate and falls back to
        # USE TEMP B-TREE FOR ORDER BY (C3 gate EXPLAIN repro). ``= 1``/``= 0``
        # is semantically identical for non-NULL comparisons (NULL matches
        # neither) and preserves the index order.
        return column == true()
    if item.operator == "is_false":
        return column == false()
    if item.operator == "is_none":
        return column.is_(None)
    if item.operator == "not_none":
        return column.is_not(None)
    if item.operator == "contains":
        return column.contains(item.value)
    if item.operator == "json_member":
        values = func.json_each(column).table_valued(
            "key",
            "value",
            joins_implicitly=True,
        )
        return (
            select(1).select_from(values).where(values.c.value == item.value).exists()
        )
    if item.operator == "ilike":
        # Community persistence is SQLite-backed.  SQLite LIKE is already
        # ASCII case-insensitive by default, while SQLAlchemy's generic ILIKE
        # emulation expands to ``lower(column) LIKE lower(?)`` and performs
        # two scalar-function calls per row.  The native form preserves the
        # established search contract and materially reduces full-scan cost
        # for the intentional ``%needle%`` pagination filters.
        return column.like(item.value, escape=SQL_LIKE_ESCAPE)
    raise ValueError(f"unsupported_application_operator:{item.operator}")


def _load_option(model: Any, path: str):
    parts = path.split(".")
    relationship = getattr(model, parts[0])
    option = selectinload(relationship)
    related_model = relationship.property.mapper.class_
    for part in parts[1:]:
        relationship = getattr(related_model, part)
        option = option.selectinload(relationship)
        related_model = relationship.property.mapper.class_
    return option


def _projection_expression(model: Any, field_name: str) -> Any:
    """Resolve a catalog-owned scalar/derived projection expression."""
    if model is models.Refinement and field_name == "ideation_title":
        return models.Ideation.title.label(field_name)
    if model is models.Story and field_name == "screen_mockups_count":
        return func.coalesce(
            func.json_array_length(models.Story.screen_mockups), 0
        ).label(field_name)
    if model is models.Story and field_name == "ideation_links_count":
        return (
            select(func.count())
            .select_from(models.StoryIdeationLink)
            .where(models.StoryIdeationLink.story_id == models.Story.id)
            .correlate(models.Story)
            .scalar_subquery()
            .label(field_name)
        )
    open_qa_binding = {
        models.Card: (models.QAItem, models.QAItem.card_id),
        models.Ideation: (
            models.IdeationQAItem,
            models.IdeationQAItem.ideation_id,
        ),
        models.Refinement: (
            models.RefinementQAItem,
            models.RefinementQAItem.refinement_id,
        ),
        models.Spec: (models.SpecQAItem, models.SpecQAItem.spec_id),
        models.Sprint: (models.SprintQAItem, models.SprintQAItem.sprint_id),
    }.get(model)
    if field_name == "open_qa_count" and open_qa_binding is not None:
        qa_model, parent_id = open_qa_binding
        return (
            select(func.count())
            .select_from(qa_model)
            .where(
                parent_id == model.id,
                qa_model.answered_at.is_(None),
            )
            .correlate(model)
            .scalar_subquery()
            .label(field_name)
        )
    if model is models.Card and field_name in {
        "validations_count",
        "conclusions_count",
    }:
        source = (
            models.Card.validations
            if field_name == "validations_count"
            else models.Card.conclusions
        )
        return func.json_array_length(func.coalesce(source, "[]")).label(field_name)
    if model is models.Card and field_name.startswith("recent_validation_"):
        raw_index = field_name.removeprefix("recent_validation_")
        if raw_index not in {"1", "2", "3", "4", "5"}:
            raise ValueError(f"unsupported_application_projection:{field_name}")
        return func.json_extract(
            func.coalesce(models.Card.validations, "[]"),
            f"$[#-{raw_index}]",
        ).label(field_name)
    if model is models.Card and field_name in {
        "validations_fail_count",
        "validations_has_pass",
    }:
        entries = func.json_each(
            func.coalesce(models.Card.validations, "[]")
        ).table_valued("key", "value", joins_implicitly=True)
        verdict = func.json_extract(entries.c.value, "$.verdict")
        if field_name == "validations_fail_count":
            return (
                select(func.count())
                .select_from(entries)
                .where(verdict == "fail")
                .correlate(models.Card)
                .scalar_subquery()
                .label(field_name)
            )
        return (
            select(1)
            .select_from(entries)
            .where(verdict == "pass")
            .correlate(models.Card)
            .exists()
            .label(field_name)
        )
    if model is models.Card and field_name.startswith("first_pass_"):
        metric = field_name.removeprefix("first_pass_")
        if metric not in {"confidence", "completeness", "drift"}:
            raise ValueError(f"unsupported_application_projection:{field_name}")
        key_entries = func.json_each(
            func.coalesce(models.Card.validations, "[]")
        ).table_valued("key", "value", joins_implicitly=True)
        first_pass_key = (
            select(func.min(key_entries.c.key))
            .select_from(key_entries)
            .where(func.json_extract(key_entries.c.value, "$.verdict") == "pass")
            .correlate(models.Card)
            .scalar_subquery()
        )
        entries = func.json_each(
            func.coalesce(models.Card.validations, "[]")
        ).table_valued("key", "value", joins_implicitly=True)
        return (
            select(func.json_extract(entries.c.value, f"$.{metric}"))
            .select_from(entries)
            .where(entries.c.key == first_pass_key)
            .correlate(models.Card)
            .scalar_subquery()
            .label(field_name)
        )
    if model is models.Card and field_name.startswith("last_conclusion_"):
        metric = field_name.removeprefix("last_conclusion_")
        if metric not in {"completeness", "drift"}:
            raise ValueError(f"unsupported_application_projection:{field_name}")
        key_entries = func.json_each(
            func.coalesce(models.Card.conclusions, "[]")
        ).table_valued("key", "value", joins_implicitly=True)
        last_key = (
            select(func.max(key_entries.c.key))
            .select_from(key_entries)
            .correlate(models.Card)
            .scalar_subquery()
        )
        entries = func.json_each(
            func.coalesce(models.Card.conclusions, "[]")
        ).table_valued("key", "value", joins_implicitly=True)
        return (
            select(func.json_extract(entries.c.value, f"$.{metric}"))
            .select_from(entries)
            .where(entries.c.key == last_key)
            .correlate(models.Card)
            .scalar_subquery()
            .label(field_name)
        )
    column = getattr(model, field_name, None)
    if column is None:
        raise ValueError(f"unsupported_application_projection:{field_name}")
    return column.label(field_name)


def _relationship_includes(includes: tuple[str, ...], name: str) -> tuple[str, ...]:
    nested: list[str] = []
    for path in includes:
        head, separator, tail = path.partition(".")
        if head == name:
            nested.append(tail if separator else "")
    return tuple(item for item in nested if item)


def _record(entity: str, row: Any, includes: tuple[str, ...] = ()) -> ApplicationRecord:
    values = {
        column.key: copy.deepcopy(getattr(row, column.key))
        for column in row.__table__.columns
    }
    if entity == "architecture_design":
        parent_type = values.get("parent_type")
        values["parent_id"] = values.get(f"{parent_type}_id")
    top_level = {path.split(".", 1)[0] for path in includes}
    mapper = row.__mapper__
    for name in top_level:
        relationship = mapper.relationships.get(name)
        if relationship is None:
            continue
        if name not in row.__dict__:
            raise RuntimeError(f"application_include_not_loaded:{entity}.{name}")
        related = getattr(row, name)
        related_entity = _CLASS_ENTITIES[relationship.mapper.class_]
        nested = _relationship_includes(includes, name)
        if relationship.uselist:
            values[name] = [_record(related_entity, item, nested) for item in related]
        else:
            values[name] = (
                _record(related_entity, related, nested)
                if related is not None
                else None
            )
    return ApplicationRecord(entity=entity, values=values)


class StatementBudgetExceeded(RuntimeError):
    """Fail-closed statement budget breach (TR1, spec 8b33f9a8).

    Raised BEFORE the over-budget statement executes: a paginated request
    that would exceed its declared budget (list <= 6; Kanban batch 23;
    column-mode 4) aborts instead of silently fanning out.
    """

    def __init__(self, *, used: int, limit: int) -> None:
        super().__init__(
            f"statement_budget_exceeded: request attempted statement "
            f"{used} of a {limit}-statement budget"
        )
        self.used = used
        self.limit = limit


class StatementBudget:
    """Per-request REAL-statement budget with a fail-closed counter.

    Attached via :func:`statement_budget` (or ``attach_statement_budget``),
    it charges on the engine's DRIVER-level ``before_cursor_execute`` event —
    i.e. on EVERY SQL statement of the current task on that engine: port
    reads, ``selectinload``/lazy relationship loader queries, flush DML and
    direct ``session.execute`` calls alike — and raises
    :class:`StatementBudgetExceeded` BEFORE the over-budget statement reaches
    the driver. Tasks/engines without an active binding are unaffected.
    """

    __slots__ = ("limit", "used")

    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self.used = 0

    def charge(self) -> None:
        self.used += 1
        if self.used > self.limit:
            raise StatementBudgetExceeded(used=self.used, limit=self.limit)


class _BudgetBinding:
    """One attach: a budget BOUND to one engine, revocable.

    ``active=False`` (set by detach AFTER a successful ContextVar reset — a
    foreign Context's reset raises first and leaves the owner intact) revokes
    the binding everywhere, including context COPIES captured by child tasks,
    so an orphaned task can never charge a closed budget. The engine field
    lets the driver hook ignore statements from OTHER engines in the same
    task (rounds 5-6, val_6ba6d4a5/val_cb5464e0).
    """

    __slots__ = ("engine", "budget", "active")

    def __init__(self, engine: Any, budget: StatementBudget) -> None:
        self.engine = engine
        self.budget = budget
        self.active = True


#: Task-scoped BINDINGS (round-5 design): a ContextVar isolates concurrent
#: requests; each entry pins {engine, budget, active} so charging filters by
#: the statement's engine and honours revocation.
_ACTIVE_BINDINGS: "ContextVar[tuple[_BudgetBinding, ...]]" = ContextVar(
    "okto_pulse_statement_budget_bindings", default=()
)
_ENGINES_WITH_BUDGET_LISTENER: "WeakSet[Any]" = WeakSet()


def _driver_charge(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
    """Engine-level ``before_cursor_execute`` hook: charge EVERY driver
    statement of the CURRENT task against the task's ACTIVE bindings FOR THIS
    ENGINE — port reads, relationship loaders, flush DML and direct executes
    alike. Raising prevents the over-budget statement from reaching the
    driver. Other tasks, other engines and revoked bindings pay nothing."""
    bindings = _ACTIVE_BINDINGS.get()
    if not bindings:
        return
    engine = conn.engine
    charged: set[int] = set()
    for binding in bindings:
        if (
            binding.active
            and binding.engine is engine
            and id(binding.budget) not in charged
        ):
            charged.add(id(binding.budget))
            binding.budget.charge()


def _ensure_driver_listener(sync_engine: Any) -> None:
    if sync_engine in _ENGINES_WITH_BUDGET_LISTENER:
        return
    event.listen(sync_engine, "before_cursor_execute", _driver_charge)
    _ENGINES_WITH_BUDGET_LISTENER.add(sync_engine)


def attach_statement_budget(
    session: Any, budget: StatementBudget
) -> tuple["Token[Any]", _BudgetBinding, tuple[_BudgetBinding, ...]]:
    """Attach a REQUEST(task)-scoped budget bound to the session's engine.

    EVERY attach creates a DISTINCT binding frame (even for the same
    budget+engine — the driver hook dedupes charges by budget id, so one SQL
    statement never double-charges a shared budget). The returned handle
    ``(token, binding, expected_stack)`` MUST go to
    :func:`detach_statement_budget`, which is fail-closed: it verifies LIFO
    order against ``expected_stack`` before mutating anything, resets the
    ContextVar FIRST (a foreign task's reset raises and leaves the owner
    intact) and only then revokes the binding for orphan context copies."""
    sync_engine = session.bind.sync_engine
    _ensure_driver_listener(sync_engine)
    current = _ACTIVE_BINDINGS.get()
    binding = _BudgetBinding(sync_engine, budget)
    expected = (*current, binding)
    token = _ACTIVE_BINDINGS.set(expected)
    return (token, binding, expected)


def detach_statement_budget(
    handle: tuple["Token[Any]", _BudgetBinding, tuple[_BudgetBinding, ...]],
) -> None:
    """Fail-closed detach: verify, reset, THEN revoke.

    1. The task's CURRENT stack must be exactly the frame this attach
       created (LIFO) — an out-of-order detach is rejected WITHOUT mutating,
       so still-open inner bindings are never silently dropped.
    2. ``ContextVar.reset(token)`` runs BEFORE the revocation — called from a
       foreign task/Context it raises ``ValueError`` and the owner stays
       fully intact (no half-revoked budget).
    3. Only after a successful reset is ``binding.active=False`` applied,
       which still revokes every orphan context copy with no async window
       (both steps are synchronous)."""
    token, binding, expected = handle
    if _ACTIVE_BINDINGS.get() is not expected:
        raise RuntimeError(
            "statement_budget_detach_out_of_order: the task's binding stack "
            "changed since this attach — detach in LIFO order from the "
            "owning task"
        )
    _ACTIVE_BINDINGS.reset(token)
    binding.active = False


@asynccontextmanager
async def statement_budget(
    session: Any, limit: int, *, budget: StatementBudget | None = None
) -> "AsyncIterator[StatementBudget]":
    """Request-scoped driver-level statement budget: attach, yield, ALWAYS
    revoke+reset in ``finally``. The route layers declare their caps here
    (list <= 6; Kanban batch 23; column-mode 4 — TR1) around the request's
    composition; pass ``budget=`` to share one budget across several
    sessions of the same request/task."""
    scoped = budget or StatementBudget(limit)
    handle = attach_statement_budget(session, scoped)
    try:
        yield scoped
    finally:
        detach_statement_budget(handle)


class CommunitySqlAlchemyApplicationPersistence:
    def __init__(self) -> None:
        self._tracked: WeakKeyDictionary[Any, list[ApplicationRecord]] = (
            WeakKeyDictionary()
        )

    def _track(
        self,
        context: Any,
        record: ApplicationRecord,
        row: Any,
    ) -> ApplicationRecord:
        records = self._tracked.setdefault(context, [])
        if all(existing is not record for existing in records):
            records.append(record)
        _register_direct_commit_record(context, record, row)
        return record

    def _clear_tracking(self, context: Any) -> None:
        self._tracked.pop(context, None)
        context.sync_session.info.pop(_DIRECT_COMMIT_RECORDS_KEY, None)

    async def resolve_user_permissions(
        self, context: Any, *, user_id: str, board_id: str
    ) -> Any:
        """Resolve direct flags, preset lineage and the board ceiling."""
        result = await context.execute(
            select(
                models.Agent.permission_flags,
                models.Agent.permissions,
                models.Agent.preset_id,
                models.AgentBoard.permission_overrides,
            )
            .outerjoin(
                models.AgentBoard,
                and_(
                    models.AgentBoard.agent_id == models.Agent.id,
                    models.AgentBoard.board_id == board_id,
                ),
            )
            .where(models.Agent.created_by == user_id)
            .limit(1)
        )
        row = result.first()
        if row is None:
            return resolve_effective_permissions(None, None, None)
        permission_flags, legacy_permissions, preset_id, board_overrides = row
        agent_flags = (
            copy.deepcopy(permission_flags) if permission_flags is not None else None
        )
        owner_review_required, review_reason = direct_permission_review(
            agent_flags,
            preset_id=preset_id,
        )
        if agent_flags is None and isinstance(legacy_permissions, list):
            agent_flags = legacy_permissions_to_flags(legacy_permissions)

        preset_flags = None
        if preset_id:
            preset_rows = list(
                (
                    await context.execute(
                        select(
                            models.PermissionPreset.id,
                            models.PermissionPreset.base_preset_id,
                            models.PermissionPreset.flags,
                        ).order_by(models.PermissionPreset.id)
                    )
                ).all()
            )
            lineage = resolve_preset_lineage(
                preset_id,
                tuple(
                    PermissionPresetLineageNode(
                        id=preset_row.id,
                        base_preset_id=preset_row.base_preset_id,
                        # Preserve malformed top-level JSON so the canonical
                        # lineage resolver can fail closed and request review.
                        flags=copy.deepcopy(preset_row.flags),
                    )
                    for preset_row in preset_rows
                ),
            )
            preset_flags = lineage.flags
            owner_review_required = lineage.owner_review_required
            review_reason = lineage.review_reason
        return resolve_effective_permissions(
            agent_flags,
            preset_flags,
            board_overrides,
            owner_review_required=owner_review_required,
            review_reason=review_reason,
        )

    async def list(
        self, context: Any, query: ApplicationQuery
    ) -> tuple[ApplicationRecord, ...]:
        model = _model(query.entity)
        if query.select_fields and query.includes:
            raise ValueError("application_projection_includes_conflict")
        statement = (
            select(
                *(
                    _projection_expression(model, field_name)
                    for field_name in query.select_fields
                )
            )
            if query.select_fields
            else select(model)
        )
        if model is models.Refinement and "ideation_title" in query.select_fields:
            statement = statement.select_from(models.Refinement).join(
                models.Ideation,
                models.Ideation.id == models.Refinement.ideation_id,
            )
        scope = _realm_scope(context, query.entity)
        if scope is not None:
            statement = statement.where(_realm_predicate(query.entity, scope))
        if query.filters:
            statement = statement.where(
                *(_predicate(model, item) for item in query.filters)
            )
        if query.any_filters:
            statement = statement.where(
                or_(*(_predicate(model, item) for item in query.any_filters))
            )
        if query.any_groups:
            statement = statement.where(
                or_(
                    *(
                        and_(*(_predicate(model, item) for item in group))
                        for group in query.any_groups
                    )
                )
            )
        if query.includes:
            statement = statement.options(
                *(_load_option(model, path) for path in query.includes)
            )
        for field_name, descending in query.order_by:
            column = getattr(model, field_name)
            statement = statement.order_by(
                column.desc() if descending else column.asc()
            )
        if query.offset:
            statement = statement.offset(query.offset)
        if query.limit is not None:
            statement = statement.limit(query.limit)
        result = await context.execute(
            statement.execution_options(populate_existing=True)
        )
        if query.select_fields:
            return tuple(
                ApplicationRecord(
                    entity=query.entity,
                    values=copy.deepcopy(dict(row)),
                )
                for row in result.mappings().all()
            )
        rows = result.scalars().all()
        return tuple(
            self._track(context, _record(query.entity, row, query.includes), row)
            for row in rows
        )

    async def list_with_count(
        self, context: Any, query: ApplicationQuery
    ) -> tuple[tuple[ApplicationRecord, ...], int]:
        """Return one page and its exact filtered total with one filter scan.

        A narrow window query computes ``count(*) over()`` together with the
        ordered page ids.  A second primary-key query hydrates only those page
        rows/projections, so expensive search predicates are not evaluated a
        second time and rich card projection expressions never run for every
        match.  An empty/out-of-range page falls back to a standalone count,
        because a window row is then unavailable to carry the total.
        """

        model = _model(query.entity)
        if query.select_fields and query.includes:
            raise ValueError("application_projection_includes_conflict")

        # Refinement query-plan tests (TR2) require the canonical index order
        # to remain free of a table-level TEMP B-TREE. SQLite's COUNT window
        # introduces such a sort even when the underlying page is indexable;
        # retain the regular count + list plan for this entity.
        if model is models.Refinement:
            total_filtered = await self.count(context, query)
            rows = await self.list(context, query)
            return rows, total_filtered

        page_id_label = "__okto_page_id"
        page_total_label = "__okto_page_total"
        key_statement = select(
            model.id.label(page_id_label),
            func.count().over().label(page_total_label),
        )
        scope = _realm_scope(context, query.entity)
        if scope is not None:
            key_statement = key_statement.where(_realm_predicate(query.entity, scope))
        if query.filters:
            key_statement = key_statement.where(
                *(_predicate(model, item) for item in query.filters)
            )
        if query.any_filters:
            key_statement = key_statement.where(
                or_(*(_predicate(model, item) for item in query.any_filters))
            )
        if query.any_groups:
            key_statement = key_statement.where(
                or_(
                    *(
                        and_(*(_predicate(model, item) for item in group))
                        for group in query.any_groups
                    )
                )
            )
        for field_name, descending in query.order_by:
            column = getattr(model, field_name)
            key_statement = key_statement.order_by(
                column.desc() if descending else column.asc()
            )
        if query.offset:
            key_statement = key_statement.offset(query.offset)
        if query.limit is not None:
            key_statement = key_statement.limit(query.limit)

        key_result = await context.execute(key_statement)
        key_rows = key_result.mappings().all()
        if not key_rows:
            return (), await self.count(context, query)

        ordered_ids = tuple(str(row[page_id_label]) for row in key_rows)
        total_filtered = int(key_rows[0][page_total_label])

        if query.select_fields:
            fetch_id_label = "__okto_fetch_id"
            fetch_statement = select(
                model.id.label(fetch_id_label),
                *(
                    _projection_expression(model, field_name)
                    for field_name in query.select_fields
                ),
            )
            if model is models.Refinement and "ideation_title" in query.select_fields:
                fetch_statement = fetch_statement.select_from(models.Refinement).join(
                    models.Ideation,
                    models.Ideation.id == models.Refinement.ideation_id,
                )
        else:
            fetch_statement = select(model)

        if scope is not None:
            fetch_statement = fetch_statement.where(
                _realm_predicate(query.entity, scope)
            )
        fetch_statement = fetch_statement.where(model.id.in_(ordered_ids))
        if query.includes:
            fetch_statement = fetch_statement.options(
                *(_load_option(model, path) for path in query.includes)
            )
        fetch_result = await context.execute(
            fetch_statement.execution_options(populate_existing=True)
        )

        if query.select_fields:
            records_by_id: dict[str, ApplicationRecord] = {}
            for raw_row in fetch_result.mappings().all():
                values = dict(raw_row)
                row_id = str(values.pop(fetch_id_label))
                records_by_id[row_id] = ApplicationRecord(
                    entity=query.entity,
                    values=copy.deepcopy(values),
                )
        else:
            model_rows = fetch_result.scalars().all()
            records_by_id = {
                str(row.id): self._track(
                    context,
                    _record(query.entity, row, query.includes),
                    row,
                )
                for row in model_rows
            }

        return (
            tuple(records_by_id[row_id] for row_id in ordered_ids),
            total_filtered,
        )

    async def count(self, context: Any, query: ApplicationQuery) -> int:
        """Count rows matching the query's predicates, ignoring the window.

        Mirrors :meth:`list` EXACTLY on scoping and filtering — including the
        realm-isolation predicate — so ``total_filtered``/``total_overall``
        can never disagree with the rows ``list`` would return. ``order_by``,
        ``offset``, ``limit`` and ``includes`` are intentionally ignored: a
        count is window-independent by contract (spec 8b33f9a8).
        """
        model = _model(query.entity)
        statement = select(func.count()).select_from(model)
        scope = _realm_scope(context, query.entity)
        if scope is not None:
            statement = statement.where(_realm_predicate(query.entity, scope))
        if query.filters:
            statement = statement.where(
                *(_predicate(model, item) for item in query.filters)
            )
        if query.any_filters:
            statement = statement.where(
                or_(*(_predicate(model, item) for item in query.any_filters))
            )
        if query.any_groups:
            statement = statement.where(
                or_(
                    *(
                        and_(*(_predicate(model, item) for item in group))
                        for group in query.any_groups
                    )
                )
            )
        result = await context.execute(statement)
        return int(result.scalar_one())

    async def group_count(
        self, context: Any, query: ApplicationGroupCountQuery
    ) -> tuple[ApplicationGroupCount, ...]:
        """Run one realm-scoped GROUP BY count with composable OR dimensions."""

        model = _model(query.entity)
        if not query.group_by:
            raise ValueError("application_group_count_fields_required")
        group_columns: list[Any] = []
        for field_name in query.group_by:
            column = getattr(model, field_name, None)
            if column is None:
                raise ValueError(f"unsupported_application_group_field:{field_name}")
            group_columns.append(column)
        for dimension in query.disjunctions:
            if not dimension:
                raise ValueError("application_group_count_disjunction_empty")
            if any(not branch for branch in dimension):
                raise ValueError("application_group_count_branch_empty")

        statement = select(
            *(
                column.label(field_name)
                for column, field_name in zip(
                    group_columns, query.group_by, strict=True
                )
            ),
            func.count().label("count"),
        ).select_from(model)
        scope = _realm_scope(context, query.entity)
        if scope is not None:
            statement = statement.where(_realm_predicate(query.entity, scope))
        if query.filters:
            statement = statement.where(
                *(_predicate(model, item) for item in query.filters)
            )
        for dimension in query.disjunctions:
            statement = statement.where(
                or_(
                    *(
                        and_(*(_predicate(model, item) for item in branch))
                        for branch in dimension
                    )
                )
            )
        statement = statement.group_by(*group_columns)
        result = await context.execute(statement)
        return tuple(
            ApplicationGroupCount(
                values=tuple(row[:-1]),
                count=int(row[-1]),
            )
            for row in result.all()
        )

    async def get(
        self,
        context: Any,
        *,
        entity: str,
        record_id: str,
        includes: tuple[str, ...] = (),
    ) -> ApplicationRecord | None:
        rows = await self.list(
            context,
            ApplicationQuery(
                entity=entity,
                filters=(ApplicationFilter("id", "eq", record_id),),
                includes=includes,
                limit=1,
            ),
        )
        return rows[0] if rows else None

    async def fence(
        self,
        context: Any,
        *,
        entity: str,
        record_id: str,
        expected_values: Mapping[str, object],
    ) -> bool:
        """Acquire a write fence only while authoritative scalar facts match."""

        model = _model(entity)
        scope = _realm_scope(context, entity)
        predicates = [model.id == record_id]
        if scope is not None:
            predicates.append(_realm_predicate(entity, scope))
        for field_name, expected in expected_values.items():
            if field_name not in model.__table__.columns:
                raise ValueError(f"unsupported_application_fence_field:{field_name}")
            predicates.append(getattr(model, field_name) == expected)
        fence_values = {
            column.key: getattr(model, column.key)
            for column in model.__table__.columns
            if column.primary_key or column.onupdate is not None
        }
        try:
            result = await context.execute(
                update(model)
                .where(*predicates)
                .values(**fence_values)
                .execution_options(synchronize_session=False)
            )
        except OperationalError as exc:
            if is_knowledge_creation_race_error(
                exc,
                target_type=entity,
                target_id=record_id,
            ):
                return False
            raise
        return int(result.rowcount or 0) == 1

    async def add(
        self,
        context: Any,
        record: ApplicationRecord,
        *,
        conflict_error: Exception | None = None,
    ) -> ApplicationRecord:
        model = _model(record.entity)
        scope = _realm_scope(context, record.entity)
        if scope is not None:
            if record.entity == "board":
                supplied = record.values.get("realm_id")
                if supplied not in (None, "", scope.realm_id):
                    raise RealmIsolationViolation()
                record.values["realm_id"] = scope.realm_id
            else:
                parent_path = _PARENT_SCOPE_PATHS.get(record.entity)
                if hasattr(model, "board_id"):
                    board_id = record.values.get("board_id")
                    if not board_id:
                        raise RealmIsolationViolation()
                    allowed = await context.scalar(
                        select(models.Board.id).where(
                            models.Board.id == board_id,
                            models.Board.realm_id == scope.realm_id,
                        )
                    )
                elif parent_path is not None:
                    foreign_key, parent_entity = parent_path
                    parent_id = record.values.get(foreign_key)
                    if not parent_id:
                        raise RealmIsolationViolation()
                    parent_model = _model(parent_entity)
                    allowed = await context.scalar(
                        select(parent_model.id).where(
                            parent_model.id == parent_id,
                            _realm_predicate(parent_entity, scope),
                        )
                    )
                else:
                    raise RuntimeError(
                        f"tenant_scope_path_not_configured:{record.entity}"
                    )
                if allowed is None:
                    raise RealmIsolationViolation()
        allowed = {column.key for column in model.__table__.columns}
        values = {
            key: copy.deepcopy(value)
            for key, value in record.values.items()
            if key in allowed
        }
        row = model(**values)
        context.add(row)
        try:
            await context.flush()
        except (IntegrityError, OperationalError) as exc:
            record_id = str(record.values.get("id") or "")
            if (
                isinstance(conflict_error, ApplicationRecordConflictError)
                and conflict_error.entity == record.entity
                and conflict_error.record_id == record_id
                and record.entity in {"card", "spec"}
                and record_id
                and is_knowledge_creation_race_error(
                    exc,
                    target_type=record.entity,
                    target_id=record_id,
                )
            ):
                raise conflict_error from exc
            raise
        fresh = _record(record.entity, row)
        record.values.clear()
        record.values.update(fresh.values)
        record.dirty_fields.clear()
        return self._track(context, record, row)

    async def delete(self, context: Any, record: ApplicationRecord) -> None:
        rows = await self.list(
            context,
            ApplicationQuery(
                entity=record.entity,
                filters=(ApplicationFilter("id", "eq", record.id),),
                limit=1,
            ),
        )
        row = await context.get(_model(record.entity), record.id) if rows else None
        if row is not None:
            await context.delete(row)
            await context.flush()
        self._tracked[context] = [
            existing
            for existing in self._tracked.get(context, [])
            if existing is not record
        ]
        entries = context.sync_session.info.get(_DIRECT_COMMIT_RECORDS_KEY, [])
        entries[:] = [
            (existing, tracked_row)
            for existing, tracked_row in entries
            if not (
                existing.entity == record.entity
                and existing.values.get("id") == record.values.get("id")
            )
        ]

    async def flush(self, context: Any) -> None:
        for record in self._tracked.get(context, []):
            if not record.dirty_fields:
                continue
            row = await context.get(_model(record.entity), record.id)
            if row is None:
                continue
            for field_name in tuple(record.dirty_fields):
                setattr(row, field_name, copy.deepcopy(record.values[field_name]))
            record.dirty_fields.clear()
        await context.flush()

    async def refresh(
        self, context: Any, record: ApplicationRecord
    ) -> ApplicationRecord:
        await self.flush(context)
        rows = await self.list(
            context,
            ApplicationQuery(
                entity=record.entity,
                filters=(ApplicationFilter("id", "eq", record.id),),
                limit=1,
            ),
        )
        row = await context.get(_model(record.entity), record.id) if rows else None
        if row is None:
            raise ValueError(
                f"application record not found: {record.entity}:{record.id}"
            )
        await context.refresh(row)
        fresh = _record(record.entity, row)
        record.values.clear()
        record.values.update(fresh.values)
        record.dirty_fields.clear()
        return record

    async def commit(self, context: Any) -> None:
        await self.flush(context)
        await materialize_pending_semantic_subject_mutations(context)
        await context.commit()
        self._clear_tracking(context)

    async def rollback(self, context: Any) -> None:
        await context.rollback()
        self._clear_tracking(context)

    async def backfill_qa_answered_at(self, context: Any) -> dict[str, int]:
        from sqlalchemy import text

        tables = (
            ("ideation_qa_items", True),
            ("refinement_qa_items", True),
            ("spec_qa_items", True),
            ("sprint_qa_items", True),
            ("qa_items", False),
        )
        fixed: dict[str, int] = {}
        for table, has_selected in tables:
            answered = "(answer IS NOT NULL AND answer != '')"
            if has_selected:
                answered = (
                    f"({answered} OR (selected IS NOT NULL "
                    "AND CAST(selected AS TEXT) NOT IN ('', '[]', 'null')))"
                )
            result = await context.execute(
                text(
                    f"UPDATE {table} "
                    "SET answered_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
                    f"WHERE answered_at IS NULL AND {answered}"
                )
            )
            count = result.rowcount if result.rowcount and result.rowcount > 0 else 0
            if count:
                fixed[table] = count
        await context.commit()
        return fixed


__all__ = ["CommunitySqlAlchemyApplicationPersistence"]
