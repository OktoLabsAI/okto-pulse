"""Community ``policy-constraint/v1`` relational-to-KG projection.

Core owns the closed event contract and the meaning of adopt/unlink/retire.
This adapter owns only the Community mechanisms: resolve the current exact
binding/revision/rule rows and reconcile their graph derivative.

The graph is an at-least-once target.  A handler may commit graph writes and
crash before its relational execution ACK; consequently every write is keyed
by the stable public identity
``guideline-revision:{revision_id}:rule:{rule_id}`` and a replay reconciles the
same node instead of minting a duplicate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, func, select

from okto_pulse.core.application.kg_runtime_access import (
    resolve_graph_transaction,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.policy_constraint_projection import (
    POLICY_CONSTRAINT_GUIDELINE_RETIRED_REASON,
    POLICY_CONSTRAINT_GUIDELINE_SUPERSEDED_REASON,
    POLICY_CONSTRAINT_REBUILD_NOT_ADOPTED_REASON,
    POLICY_CONSTRAINT_RULE_REMOVED_REASON,
    POLICY_CONSTRAINT_UNLINKED_REASON,
    PolicyConstraintProjectionResult,
)

from .sqlalchemy_guideline_policy import (
    guideline_rule_from_payload,
    guideline_rule_payload,
)
from .sqlalchemy_models import (
    GuidelineBoardBindingRow,
    GuidelineRetirementRow,
    GuidelineRevisionRow,
)


POLICY_CONSTRAINT_CONTRACT = "policy-constraint/v1"
POLICY_CONSTRAINT_ACTOR = "policy-constraint-projector"
POLICY_CONSTRAINT_NODE_TYPE = "Constraint"
POLICY_CONSTRAINT_ROOT_RULE = (
    "belongs_to/policy_constraint_to_board@policy-constraint/v1"
)
POLICY_CONSTRAINT_LINEAGE_RULE = (
    "supersedes/policy_constraint_revision@policy-constraint/v1"
)
POLICY_CONSTRAINT_REVISION_SUPERSEDED_REASON = (
    "policy_constraint_revision_superseded"
)


class PolicyConstraintProjectionConflict(RuntimeError):
    """The relational authority or its graph derivative is inconsistent."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _DesiredConstraint:
    node_id: str
    guideline_id: str
    revision_id: str
    rule_id: str
    attrs: dict[str, Any]
    content_digest: str


@dataclass(frozen=True, slots=True)
class _GraphConstraint:
    node_id: str
    guideline_id: str
    revision_id: str
    rule_id: str
    source_artifact_ref: str
    created_by_agent: str
    revocation_reason: str
    superseded_by: str
    source_content_hash: str
    title: str
    content: str
    context: str
    justification: str

    @property
    def active(self) -> bool:
        return not self.revocation_reason and not self.superseded_by


def _required_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyConstraintProjectionConflict(code)
    return value.strip()


def _aware_utc(value: object, code: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PolicyConstraintProjectionConflict(code)
    return value.astimezone(timezone.utc)


def _node_id(*, revision_id: str, rule_id: str) -> str:
    return f"guideline-revision:{revision_id}:rule:{rule_id}"


def _event_revision_id(event: object, *, operation: str) -> str:
    del operation
    value = getattr(event, "exact_revision_id", None)
    return _required_text(value, "policy_constraint_event_revision_required")


def _constraint_context(
    *,
    guideline_id: str,
    revision_id: str,
    semantic_version: str,
    rule_payload: dict[str, object],
) -> str:
    return json.dumps(
        {
            "contract": POLICY_CONSTRAINT_CONTRACT,
            "guideline_id": guideline_id,
            "revision_id": revision_id,
            "semantic_version": semantic_version,
            "rule": rule_payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _desired_constraint(
    *,
    binding: GuidelineBoardBindingRow,
    revision: GuidelineRevisionRow,
    rule_payload: object,
    source_session_id: str,
    projected_at: datetime,
) -> _DesiredConstraint:
    rule = guideline_rule_from_payload(rule_payload)
    canonical_rule = guideline_rule_payload(rule)
    node_id = _node_id(
        revision_id=revision.revision_id,
        rule_id=rule.rule_id,
    )
    content_digest = canonical_sha256(
        {
            "contract": POLICY_CONSTRAINT_CONTRACT,
            "guideline_id": revision.guideline_id,
            "revision_id": revision.revision_id,
            "revision_digest": revision.content_digest,
            "binding_id": binding.binding_id,
            "binding_revision": binding.binding_revision,
            "default_enforcement": binding.default_enforcement,
            "rule": canonical_rule,
        }
    )
    context = _constraint_context(
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        rule_payload=canonical_rule,
    )
    return _DesiredConstraint(
        node_id=node_id,
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        rule_id=rule.rule_id,
        attrs={
            "title": rule.title,
            "content": rule.description,
            "context": context,
            "justification": revision.content,
            "source_artifact_ref": node_id,
            "graph_layer": "canonical",
            "maturity_status": "canonical_eligible",
            "source_session_id": source_session_id,
            "created_at": projected_at.isoformat(),
            "created_by_agent": POLICY_CONSTRAINT_ACTOR,
            "source_confidence": 1.0,
            "relevance_score": 0.5,
            "priority_boost": 0.0,
            "superseded_by": None,
            "superseded_at": None,
            "revocation_reason": None,
            "human_curated": False,
            "generation": revision.revision_number,
            "source_content_hash": content_digest,
            "kind_of": None,
        },
        content_digest=content_digest,
    )


def _graph_constraints(scope: Any) -> tuple[_GraphConstraint, ...]:
    result = scope.execute(
        "MATCH (n:Constraint) "
        "RETURN n.id, n.source_artifact_ref, n.created_by_agent, "
        "n.revocation_reason, n.superseded_by, n.source_content_hash, "
        "n.title, n.content, n.context, n.justification"
    )
    owned: list[_GraphConstraint] = []
    seen_ids: set[str] = set()
    seen_refs: set[str] = set()
    for row in result.rows:
        node_id = str(row[0] or "")
        source_artifact_ref = str(row[1] or "")
        created_by_agent = str(row[2] or "")
        context = str(row[8] or "")
        looks_owned = (
            created_by_agent == POLICY_CONSTRAINT_ACTOR
            or source_artifact_ref.startswith("guideline-revision:")
            or node_id.startswith("guideline-revision:")
        )
        if not looks_owned:
            continue
        try:
            identity = json.loads(context)
            rule_identity = identity["rule"]
            guideline_id = _required_text(
                identity["guideline_id"],
                "policy_constraint_graph_identity_invalid",
            )
            revision_id = _required_text(
                identity["revision_id"],
                "policy_constraint_graph_identity_invalid",
            )
            rule_id = _required_text(
                rule_identity["rule_id"],
                "policy_constraint_graph_identity_invalid",
            )
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            PolicyConstraintProjectionConflict,
        ) as exc:
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_graph_identity_invalid"
            ) from exc
        node = _GraphConstraint(
            node_id=node_id,
            guideline_id=guideline_id,
            revision_id=revision_id,
            rule_id=rule_id,
            source_artifact_ref=source_artifact_ref,
            created_by_agent=created_by_agent,
            revocation_reason=str(row[3] or ""),
            superseded_by=str(row[4] or ""),
            source_content_hash=str(row[5] or ""),
            title=str(row[6] or ""),
            content=str(row[7] or ""),
            context=context,
            justification=str(row[9] or ""),
        )
        if (
            node.created_by_agent != POLICY_CONSTRAINT_ACTOR
            or node.node_id != node.source_artifact_ref
            or node.node_id
            != _node_id(
                revision_id=node.revision_id,
                rule_id=node.rule_id,
            )
        ):
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_graph_identity_conflict"
            )
        if node.node_id in seen_ids or node.source_artifact_ref in seen_refs:
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_graph_identity_duplicate"
            )
        seen_ids.add(node.node_id)
        seen_refs.add(node.source_artifact_ref)
        owned.append(node)
    return tuple(sorted(owned, key=lambda item: item.node_id))


def _root_node_id(scope: Any, *, board_id: str, source_session_id: str) -> str:
    source_ref = f"board:{board_id}"
    result = scope.execute(
        "MATCH (n:Entity) WHERE n.source_artifact_ref = $source_ref "
        "RETURN n.id",
        {"source_ref": source_ref},
    )
    root_ids = tuple(sorted({str(row[0]) for row in result.rows if row[0]}))
    if len(root_ids) > 1:
        raise PolicyConstraintProjectionConflict(
            "policy_constraint_board_root_ambiguous"
        )
    if root_ids:
        return root_ids[0]
    root_id = f"policy-board-root:{board_id}"
    scope.create_node(
        "Entity",
        root_id,
        {
            "title": f"Board {board_id}",
            "content": "Deterministic KG board root.",
            "source_artifact_ref": source_ref,
            "graph_layer": "canonical",
            "maturity_status": "canonical_eligible",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by_agent": POLICY_CONSTRAINT_ACTOR,
            "source_confidence": 1.0,
            "relevance_score": 0.5,
            "priority_boost": 0.0,
            "human_curated": False,
            "generation": 1,
        },
        source_session_id=source_session_id,
    )
    return root_id


def _constraint_semantics_match(
    current: _GraphConstraint,
    desired: _DesiredConstraint,
) -> bool:
    attrs = desired.attrs
    return (
        current.source_content_hash == desired.content_digest
        and current.title == attrs["title"]
        and current.content == attrs["content"]
        and current.context == attrs["context"]
        and current.justification == attrs["justification"]
    )


def _policy_lineage_edge_exists(
    scope: Any,
    *,
    successor_id: str,
    predecessor_id: str,
) -> bool:
    """Return only lineage owned by this deterministic projector.

    ``supersedes`` is a shared KG relation.  Endpoint-only checks would let an
    unrelated subsystem's edge suppress B14 lineage, and endpoint-only deletes
    would erase that subsystem's evidence during historical re-adoption.
    """

    result = scope.execute(
        "MATCH (new:Constraint {id: $successor_id})"
        "-[r:supersedes]->(old:Constraint {id: $predecessor_id}) "
        "WHERE r.rule_id = $rule_id RETURN count(r)",
        {
            "successor_id": successor_id,
            "predecessor_id": predecessor_id,
            "rule_id": POLICY_CONSTRAINT_LINEAGE_RULE,
        },
    )
    return bool(result.rows and int(result.rows[0][0] or 0) > 0)


class CommunitySqlAlchemyPolicyConstraintProjection:
    """Resolve exact relational authority and reconcile its graph derivative."""

    def __init__(
        self,
        *,
        graph_transaction_resolver: Callable[[], Any] = (
            resolve_graph_transaction
        ),
    ) -> None:
        self._graph_transaction_resolver = graph_transaction_resolver

    async def _validate_event_revision(
        self,
        context: Any,
        *,
        event: object,
        operation: str,
    ) -> None:
        guideline_id = _required_text(
            getattr(event, "guideline_id", None),
            "policy_constraint_event_guideline_required",
        )
        revision_id = _event_revision_id(event, operation=operation)
        row = (
            await context.execute(
                select(GuidelineRevisionRow).where(
                    GuidelineRevisionRow.guideline_id == guideline_id,
                    GuidelineRevisionRow.revision_id == revision_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_event_revision_missing"
            )
        if operation == "adopt" and hasattr(event, "to_revision_id"):
            event_semantic_version = getattr(
                event,
                "to_semantic_version",
                None,
            )
            event_revision_digest = getattr(
                event,
                "to_revision_digest",
                None,
            )
        elif operation == "unlink":
            event_semantic_version = getattr(
                event,
                "from_semantic_version",
                None,
            )
            event_revision_digest = getattr(
                event,
                "from_revision_digest",
                None,
            )
        else:
            event_semantic_version = getattr(
                event,
                "semantic_version",
                None,
            )
            event_revision_digest = getattr(
                event,
                "revision_digest",
                None,
            )
        if (
            event_semantic_version != row.semantic_version
            or event_revision_digest != row.content_digest
            or (
                operation == "retire"
                and getattr(event, "revision_number", None)
                != row.revision_number
            )
        ):
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_event_revision_evidence_mismatch"
            )
        if not isinstance(row.rules, list):
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_event_rules_invalid"
            )
        # Decode the complete exact revision even for unlink/retire.  Core
        # validates event semantics; Community proves the referenced physical
        # revision and every immutable rule are still resolvable.
        tuple(guideline_rule_from_payload(rule) for rule in row.rules)

        # Binding materialization has no B08 impact ledger; prove every
        # immutable binding field against the exact historical row before the
        # worker reconciles the latest board state.  Delayed events remain
        # valid because this lookup is not restricted to the current head.
        if hasattr(event, "source_kind"):
            binding = (
                await context.execute(
                    select(GuidelineBoardBindingRow).where(
                        GuidelineBoardBindingRow.board_id
                        == getattr(event, "board_id", None),
                        GuidelineBoardBindingRow.guideline_id
                        == guideline_id,
                        GuidelineBoardBindingRow.binding_id
                        == getattr(event, "binding_id", None),
                        GuidelineBoardBindingRow.binding_revision
                        == getattr(event, "binding_revision", None),
                    )
                )
            ).scalar_one_or_none()
            if (
                binding is None
                or binding.state != "active"
                or binding.revision_id != row.revision_id
                or binding.semantic_version != row.semantic_version
                or binding.revision_digest != row.content_digest
                or binding.source_kind
                != getattr(event, "source_kind", None)
                or binding.default_enforcement
                != getattr(event, "default_enforcement", None)
                or binding.priority != getattr(event, "priority", None)
                or binding.adopted_by != getattr(event, "actor_id", None)
                or _aware_utc(
                    binding.adopted_at,
                    "policy_constraint_binding_time_invalid",
                )
                != _aware_utc(
                    getattr(event, "occurred_at", None),
                    "policy_constraint_event_time_invalid",
                )
            ):
                raise PolicyConstraintProjectionConflict(
                    "policy_constraint_event_binding_evidence_mismatch"
                )

    async def _desired_for_board(
        self,
        context: Any,
        *,
        board_id: str,
        source_session_id: str,
        projected_at: datetime,
    ) -> tuple[_DesiredConstraint, ...]:
        latest = (
            select(
                GuidelineBoardBindingRow.board_id.label("board_id"),
                GuidelineBoardBindingRow.guideline_id.label("guideline_id"),
                func.max(GuidelineBoardBindingRow.binding_revision).label(
                    "binding_revision"
                ),
            )
            .where(GuidelineBoardBindingRow.board_id == board_id)
            .group_by(
                GuidelineBoardBindingRow.board_id,
                GuidelineBoardBindingRow.guideline_id,
            )
            .subquery()
        )
        rows = (
            await context.execute(
                select(GuidelineBoardBindingRow, GuidelineRevisionRow)
                .join(
                    latest,
                    and_(
                        GuidelineBoardBindingRow.board_id == latest.c.board_id,
                        GuidelineBoardBindingRow.guideline_id
                        == latest.c.guideline_id,
                        GuidelineBoardBindingRow.binding_revision
                        == latest.c.binding_revision,
                    ),
                )
                .join(
                    GuidelineRevisionRow,
                    and_(
                        GuidelineRevisionRow.guideline_id
                        == GuidelineBoardBindingRow.guideline_id,
                        GuidelineRevisionRow.revision_id
                        == GuidelineBoardBindingRow.revision_id,
                    ),
                )
                .outerjoin(
                    GuidelineRetirementRow,
                    GuidelineRetirementRow.guideline_id
                    == GuidelineBoardBindingRow.guideline_id,
                )
                .where(
                    GuidelineBoardBindingRow.state == "active",
                    GuidelineRetirementRow.guideline_id.is_(None),
                )
                .order_by(
                    GuidelineBoardBindingRow.guideline_id.asc(),
                    GuidelineRevisionRow.revision_id.asc(),
                )
            )
        ).all()

        desired: list[_DesiredConstraint] = []
        seen_ids: set[str] = set()
        for binding, revision in rows:
            if (
                binding.revision_id != revision.revision_id
                or binding.revision_digest != revision.content_digest
                or binding.semantic_version != revision.semantic_version
                or not isinstance(revision.rules, list)
            ):
                raise PolicyConstraintProjectionConflict(
                    "policy_constraint_binding_revision_mismatch"
                )
            for rule_payload in revision.rules:
                candidate = _desired_constraint(
                    binding=binding,
                    revision=revision,
                    rule_payload=rule_payload,
                    source_session_id=source_session_id,
                    projected_at=projected_at,
                )
                if candidate.node_id in seen_ids:
                    raise PolicyConstraintProjectionConflict(
                        "policy_constraint_desired_identity_duplicate"
                    )
                seen_ids.add(candidate.node_id)
                desired.append(candidate)
        return tuple(sorted(desired, key=lambda item: item.node_id))

    async def _reconcile(
        self,
        *,
        board_id: str,
        operation: str,
        event_id: str | None,
        desired: tuple[_DesiredConstraint, ...],
        projected_at: datetime,
        no_successor_reason: str,
    ) -> PolicyConstraintProjectionResult:
        graph = self._graph_transaction_resolver()
        if graph is None:
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_graph_transaction_missing"
            )
        scope = await graph.begin(board_id)
        desired_by_id = {item.node_id: item for item in desired}
        desired_successors = {
            (item.guideline_id, item.rule_id): item.node_id
            for item in desired
        }
        source_session_id = (
            f"policy-constraint:{event_id}"
            if event_id is not None
            else f"policy-constraint:rebuild:{board_id}"
        )
        activated_count = 0
        ended_count = 0

        async with scope:
            transaction_open = False
            try:
                scope.execute("BEGIN TRANSACTION")
                transaction_open = True
                current = _graph_constraints(scope)
                current_by_id = {item.node_id: item for item in current}
                current_by_ref = {
                    item.source_artifact_ref: item for item in current
                }
                for desired_node in desired:
                    # A historical revision may be adopted again.  Active
                    # nodes cannot remain the predecessor of a stale
                    # supersedence edge or downgrade/re-adoption would create
                    # a cycle.  Immutable relational events retain the full
                    # transition history; graph lineage is the reconciled
                    # current chain.
                    scope.execute(
                        "MATCH (:Constraint)-[r:supersedes]->"
                        "(old:Constraint {id: $node_id}) "
                        "WHERE r.rule_id = $rule_id DELETE r",
                        {
                            "node_id": desired_node.node_id,
                            "rule_id": POLICY_CONSTRAINT_LINEAGE_RULE,
                        },
                    )
                    conflicting = current_by_ref.get(desired_node.node_id)
                    if (
                        conflicting is not None
                        and conflicting.node_id != desired_node.node_id
                    ):
                        raise PolicyConstraintProjectionConflict(
                            "policy_constraint_source_ref_conflict"
                        )
                    existing = current_by_id.get(desired_node.node_id)
                    if existing is None:
                        scope.create_node(
                            POLICY_CONSTRAINT_NODE_TYPE,
                            desired_node.node_id,
                            dict(desired_node.attrs),
                            source_session_id=source_session_id,
                        )
                        activated_count += 1
                    elif not existing.active or not _constraint_semantics_match(
                        existing,
                        desired_node,
                    ):
                        # Preserve the node's original graph creation time and
                        # bind temporal values explicitly.  The Kuzu
                        # ``update_node`` helper intentionally treats values as
                        # plain parameters, so assigning an ISO string directly
                        # to a TIMESTAMP property is not portable.
                        update_attrs = {
                            key: value
                            for key, value in desired_node.attrs.items()
                            if key not in {"created_at", "superseded_at"}
                        }
                        if not existing.active:
                            scope.execute(
                                "MATCH (n:Constraint {id: $node_id}) "
                                "SET n.superseded_by = NULL, "
                                "n.superseded_at = NULL, "
                                "n.revocation_reason = NULL",
                                {"node_id": desired_node.node_id},
                            )
                        scope.update_node(
                            POLICY_CONSTRAINT_NODE_TYPE,
                            desired_node.node_id,
                            update_attrs,
                        )
                        activated_count += 1

                root_id = (
                    _root_node_id(
                        scope,
                        board_id=board_id,
                        source_session_id=source_session_id,
                    )
                    if desired
                    else None
                )
                if root_id is not None:
                    for desired_node in desired:
                        if not scope.edge_exists(
                            "belongs_to",
                            POLICY_CONSTRAINT_NODE_TYPE,
                            "Entity",
                            desired_node.node_id,
                            root_id,
                        ):
                            scope.create_edge(
                                "belongs_to",
                                POLICY_CONSTRAINT_NODE_TYPE,
                                "Entity",
                                desired_node.node_id,
                                root_id,
                                {
                                    "confidence": 1.0,
                                    "created_by_session_id": source_session_id,
                                    "created_at": projected_at.isoformat(),
                                    "rule_id": POLICY_CONSTRAINT_ROOT_RULE,
                                },
                            )

                for existing in current:
                    if existing.node_id in desired_by_id or not existing.active:
                        continue
                    successor_id = desired_successors.get(
                        (existing.guideline_id, existing.rule_id),
                    )
                    reason = (
                        POLICY_CONSTRAINT_REVISION_SUPERSEDED_REASON
                        if successor_id
                        else no_successor_reason
                    )
                    scope.execute(
                        "MATCH (n:Constraint {id: $node_id}) "
                        "SET n.superseded_by = $superseded_by, "
                        "n.superseded_at = timestamp($ended_at), "
                        "n.revocation_reason = $reason",
                        {
                            "node_id": existing.node_id,
                            "superseded_by": successor_id,
                            "ended_at": projected_at.isoformat(),
                            "reason": reason,
                        },
                    )
                    if successor_id and not _policy_lineage_edge_exists(
                        scope,
                        successor_id=successor_id,
                        predecessor_id=existing.node_id,
                    ):
                        scope.create_edge(
                            "supersedes",
                            POLICY_CONSTRAINT_NODE_TYPE,
                            POLICY_CONSTRAINT_NODE_TYPE,
                            successor_id,
                            existing.node_id,
                            {
                                "confidence": 1.0,
                                "created_by_session_id": (
                                    source_session_id
                                ),
                                "created_at": projected_at.isoformat(),
                                "rule_id": POLICY_CONSTRAINT_LINEAGE_RULE,
                            },
                        )
                    ended_count += 1

                verified = _graph_constraints(scope)
                active_ids = tuple(
                    sorted(item.node_id for item in verified if item.active)
                )
                desired_ids = tuple(sorted(desired_by_id))
                if active_ids != desired_ids:
                    raise PolicyConstraintProjectionConflict(
                        "policy_constraint_reconciliation_unconfirmed"
                    )
                scope.execute("COMMIT")
                transaction_open = False
            except BaseException:
                if transaction_open:
                    try:
                        scope.execute("ROLLBACK")
                    except BaseException as rollback_error:
                        raise PolicyConstraintProjectionConflict(
                            "policy_constraint_transaction_cleanup_unconfirmed"
                        ) from rollback_error
                raise

        return PolicyConstraintProjectionResult(
            board_id=board_id,
            operation=operation,
            event_id=event_id,
            activated_count=activated_count,
            ended_count=ended_count,
            active_count=len(desired),
            unadopted_active_count=0,
            node_ids=tuple(sorted(desired_by_id)),
            replayed=activated_count == 0 and ended_count == 0,
        )

    async def apply(
        self,
        context: Any,
        *,
        event: object,
    ) -> PolicyConstraintProjectionResult:
        operation = _required_text(
            getattr(event, "operation", None),
            "policy_constraint_event_operation_required",
        )
        if operation not in {"adopt", "unlink", "retire"}:
            raise PolicyConstraintProjectionConflict(
                "policy_constraint_event_operation_invalid"
            )
        board_id = _required_text(
            getattr(event, "board_id", None),
            "policy_constraint_event_board_required",
        )
        event_id = _required_text(
            getattr(event, "event_id", None),
            "policy_constraint_event_id_required",
        )
        projected_at = _aware_utc(
            getattr(event, "occurred_at", None),
            "policy_constraint_event_time_invalid",
        )
        await self._validate_event_revision(
            context,
            event=event,
            operation=operation,
        )
        desired = await self._desired_for_board(
            context,
            board_id=board_id,
            source_session_id=f"policy-constraint:{event_id}",
            projected_at=projected_at,
        )
        no_successor_reason = {
            "adopt": POLICY_CONSTRAINT_RULE_REMOVED_REASON,
            "unlink": POLICY_CONSTRAINT_UNLINKED_REASON,
            "retire": (
                POLICY_CONSTRAINT_GUIDELINE_SUPERSEDED_REASON
                if getattr(event, "retirement_status", None) == "superseded"
                else POLICY_CONSTRAINT_GUIDELINE_RETIRED_REASON
            ),
        }[operation]
        return await self._reconcile(
            board_id=board_id,
            operation=operation,
            event_id=event_id,
            desired=desired,
            projected_at=projected_at,
            no_successor_reason=no_successor_reason,
        )

    async def rebuild_board(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> PolicyConstraintProjectionResult:
        normalized_board_id = _required_text(
            board_id,
            "policy_constraint_rebuild_board_required",
        )
        projected_at = datetime.now(timezone.utc)
        desired = await self._desired_for_board(
            context,
            board_id=normalized_board_id,
            source_session_id=(
                f"policy-constraint:rebuild:{normalized_board_id}"
            ),
            projected_at=projected_at,
        )
        return await self._reconcile(
            board_id=normalized_board_id,
            operation="rebuild",
            event_id=None,
            desired=desired,
            projected_at=projected_at,
            no_successor_reason=(
                POLICY_CONSTRAINT_REBUILD_NOT_ADOPTED_REASON
            ),
        )


__all__ = [
    "CommunitySqlAlchemyPolicyConstraintProjection",
    "POLICY_CONSTRAINT_ACTOR",
    "POLICY_CONSTRAINT_CONTRACT",
    "POLICY_CONSTRAINT_GUIDELINE_SUPERSEDED_REASON",
    "POLICY_CONSTRAINT_LINEAGE_RULE",
    "POLICY_CONSTRAINT_NODE_TYPE",
    "POLICY_CONSTRAINT_REBUILD_NOT_ADOPTED_REASON",
    "POLICY_CONSTRAINT_REVISION_SUPERSEDED_REASON",
    "POLICY_CONSTRAINT_RULE_REMOVED_REASON",
    "POLICY_CONSTRAINT_UNLINKED_REASON",
    "PolicyConstraintProjectionConflict",
]
