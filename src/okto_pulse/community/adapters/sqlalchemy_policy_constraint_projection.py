"""Community relational-to-KG projection for semantic guideline evidence.

Core owns the closed event envelopes.  Community resolves the exact
relational revision, metric, binding, assessment and exception authority and
reconciles stable physical ``Entity`` subtypes into the graph.  At-least-once
delivery is safe because every node uses a stable public identity and every
replay derives the full board state again.

The adapter never creates historical rule ``Constraint`` nodes and terminates
any that are still active.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import and_, select

from okto_pulse.core.application.kg_runtime_access import (
    resolve_graph_transaction,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.events.types import SemanticGuidelineProjectionChanged
from okto_pulse.core.ports.policy_constraint_projection import (
    PolicyConstraintProjectionResult,
)
from .sqlalchemy_models import (
    GuidelineBoardBindingRow,
    GuidelineRetirementRow,
    GuidelineRevisionRow,
    SemanticGuidelineAssessmentReceiptRow,
    SemanticGuidelineBindingConfigurationRow,
    SemanticGuidelineMetricResultRow,
    SemanticGuidelineRevisionRow,
    SemanticGuidelineSkipRow,
    SemanticGuidelineWaiverEventRow,
    SemanticGuidelineWaiverRow,
)


POLICY_CONSTRAINT_ACTOR = "policy-constraint-projector"


class PolicyConstraintProjectionConflict(RuntimeError):
    """The relational authority or its graph derivative is inconsistent."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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


def _event_revision_id(event: object) -> str:
    value = getattr(event, "exact_revision_id", None)
    return _required_text(value, "semantic_guideline_event_revision_required")


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


SEMANTIC_GUIDELINE_KG_CONTRACT = "semantic-guideline-kg/v1"
SEMANTIC_GUIDELINE_KG_ACTOR = "semantic-guideline-projector"
SEMANTIC_GUIDELINE_KG_NODE_TYPE = "Entity"
SEMANTIC_GUIDELINE_KG_ROOT_RULE = (
    "belongs_to/semantic_guideline_to_board@semantic-guideline-kg/v1"
)
SEMANTIC_GUIDELINE_KG_LINEAGE_RULE = (
    "supersedes/semantic_guideline_lineage@semantic-guideline-kg/v1"
)
SEMANTIC_GUIDELINE_LEGACY_TERMINATED_REASON = (
    "semantic_guideline_legacy_rule_projection_retired"
)
SEMANTIC_GUIDELINE_SOURCE_REMOVED_REASON = (
    "semantic_guideline_relational_source_removed"
)
_SEMANTIC_SOURCE_PREFIX = "semantic-guideline:"


@dataclass(frozen=True, slots=True)
class _SemanticDesiredNode:
    node_id: str
    kind: str
    digest: str
    generation: int
    active: bool
    successor_id: str | None
    reason: str | None
    lineage_ids: tuple[str, ...]
    attrs: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _SemanticGraphNode:
    node_id: str
    source_artifact_ref: str
    created_by_agent: str
    source_content_hash: str
    title: str
    content: str
    context: str
    justification: str
    generation: int
    kind_of: str
    superseded_by: str
    revocation_reason: str

    @property
    def active(self) -> bool:
        return not self.superseded_by and not self.revocation_reason


@dataclass(frozen=True, slots=True)
class _LegacyRuleNode:
    node_id: str
    source_artifact_ref: str
    created_by_agent: str
    revocation_reason: str
    superseded_by: str

    @property
    def active(self) -> bool:
        return not self.revocation_reason and not self.superseded_by


def _db_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _semantic_node_id(kind: str, identity: str) -> str:
    return f"{_SEMANTIC_SOURCE_PREFIX}{kind}:{identity}"


def _waiver_projection_state(
    *,
    status: str,
    expires_at: datetime | None,
    binding_id: str,
    binding_revision: int,
    active_binding_keys: set[tuple[str, int]],
    projected_at: datetime,
) -> tuple[bool, str | None]:
    """Resolve active/tombstone state with explicit precedence."""

    if status not in {"requested", "approved"}:
        return False, f"semantic_guideline_waiver_{status}"
    if expires_at is not None and _db_utc(expires_at) <= projected_at:
        return False, "semantic_guideline_waiver_scheduled_expiry"
    if (binding_id, binding_revision) not in active_binding_keys:
        return False, "semantic_guideline_waiver_binding_inactive"
    return True, None


def _semantic_context(kind: str, payload: dict[str, Any]) -> str:
    return json.dumps(
        {
            "contract": SEMANTIC_GUIDELINE_KG_CONTRACT,
            "kind": kind,
            **payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _desired_semantic_node(
    *,
    kind: str,
    identity: str,
    digest: str,
    generation: int,
    active: bool,
    reason: str | None,
    successor_id: str | None,
    lineage_ids: tuple[str, ...] = (),
    title: str,
    content: str,
    payload: dict[str, Any],
    created_at: datetime,
    projected_at: datetime,
) -> _SemanticDesiredNode:
    node_id = _semantic_node_id(kind, identity)
    context = _semantic_context(kind, payload)
    terminal_reason = None if active else (reason or "semantic_guideline_ended")
    attrs = {
        "title": title,
        "content": content,
        "context": context,
        "justification": (
            "Relational semantic-guideline authority projected after commit."
        ),
        "source_artifact_ref": node_id,
        "graph_layer": "canonical",
        "maturity_status": "canonical_eligible",
        "created_at": _db_utc(created_at).isoformat(),
        "created_by_agent": SEMANTIC_GUIDELINE_KG_ACTOR,
        "source_confidence": 1.0,
        "relevance_score": 0.5,
        "priority_boost": 0.0,
        "superseded_by": successor_id,
        "superseded_at": (
            None if active else projected_at.isoformat()
        ),
        "revocation_reason": terminal_reason,
        "human_curated": False,
        "generation": generation,
        "source_content_hash": digest,
        "kind_of": f"SemanticGuideline{kind.title().replace('_', '')}",
    }
    return _SemanticDesiredNode(
        node_id=node_id,
        kind=kind,
        digest=digest,
        generation=generation,
        active=active,
        successor_id=successor_id,
        reason=terminal_reason,
        lineage_ids=tuple(sorted(set(lineage_ids))),
        attrs=attrs,
    )


def _semantic_graph_nodes(scope: Any) -> tuple[_SemanticGraphNode, ...]:
    result = scope.execute(
        "MATCH (n:Entity) RETURN n.id, n.source_artifact_ref, "
        "n.created_by_agent, n.source_content_hash, n.title, n.content, "
        "n.context, n.justification, n.generation, n.superseded_by, "
        "n.revocation_reason, n.kind_of"
    )
    nodes: list[_SemanticGraphNode] = []
    seen: set[str] = set()
    for row in result.rows:
        node_id = str(row[0] or "")
        source_ref = str(row[1] or "")
        actor = str(row[2] or "")
        if not (
            actor == SEMANTIC_GUIDELINE_KG_ACTOR
            or source_ref.startswith(_SEMANTIC_SOURCE_PREFIX)
            or node_id.startswith(_SEMANTIC_SOURCE_PREFIX)
        ):
            continue
        if (
            not node_id
            or node_id != source_ref
            or not node_id.startswith(_SEMANTIC_SOURCE_PREFIX)
            or node_id in seen
        ):
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_graph_identity_invalid"
            )
        try:
            context = json.loads(str(row[6] or ""))
        except (TypeError, json.JSONDecodeError) as exc:
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_graph_context_invalid"
            ) from exc
        if (
            not isinstance(context, dict)
            or context.get("contract") != SEMANTIC_GUIDELINE_KG_CONTRACT
            or not isinstance(context.get("kind"), str)
        ):
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_graph_context_invalid"
            )
        seen.add(node_id)
        nodes.append(
            _SemanticGraphNode(
                node_id=node_id,
                source_artifact_ref=source_ref,
                created_by_agent=actor,
                source_content_hash=str(row[3] or ""),
                title=str(row[4] or ""),
                content=str(row[5] or ""),
                context=str(row[6] or ""),
                justification=str(row[7] or ""),
                generation=int(row[8] or 0),
                kind_of=str(row[11] or ""),
                superseded_by=str(row[9] or ""),
                revocation_reason=str(row[10] or ""),
            )
        )
    return tuple(sorted(nodes, key=lambda item: item.node_id))


def _legacy_rule_nodes(scope: Any) -> tuple[_LegacyRuleNode, ...]:
    result = scope.execute(
        "MATCH (n:Constraint) RETURN n.id, n.source_artifact_ref, "
        "n.created_by_agent, n.revocation_reason, n.superseded_by"
    )
    nodes = []
    for row in result.rows:
        node_id = str(row[0] or "")
        source_ref = str(row[1] or "")
        actor = str(row[2] or "")
        if not (
            actor == POLICY_CONSTRAINT_ACTOR
            or source_ref.startswith("guideline-revision:")
            or node_id.startswith("guideline-revision:")
        ):
            continue
        nodes.append(
            _LegacyRuleNode(
                node_id=node_id,
                source_artifact_ref=source_ref,
                created_by_agent=actor,
                revocation_reason=str(row[3] or ""),
                superseded_by=str(row[4] or ""),
            )
        )
    return tuple(nodes)


def _semantic_node_matches(
    current: _SemanticGraphNode,
    desired: _SemanticDesiredNode,
) -> bool:
    attrs = desired.attrs
    return (
        current.created_by_agent == SEMANTIC_GUIDELINE_KG_ACTOR
        and current.source_content_hash == desired.digest
        and current.title == attrs["title"]
        and current.content == attrs["content"]
        and current.context == attrs["context"]
        and current.justification == attrs["justification"]
        and current.generation == desired.generation
        and current.kind_of == attrs["kind_of"]
        and current.superseded_by == (desired.successor_id or "")
        and current.revocation_reason == (desired.reason or "")
    )


class CommunitySqlAlchemyPolicyConstraintProjection:
    """Compatibility-named projector for semantic guideline KG lineage.

    The former deterministic Rule -> Constraint materializer is deliberately
    not invoked.  This adapter projects relational semantic evidence as
    ``Entity`` subtypes, and only retains old ``Constraint`` nodes as terminal
    audit history.
    """

    def __init__(
        self,
        *,
        graph_transaction_resolver: Callable[[], Any] = (
            resolve_graph_transaction
        ),
    ) -> None:
        self._graph_transaction_resolver = graph_transaction_resolver

    async def _validate_generic_event(
        self,
        context: Any,
        *,
        event: SemanticGuidelineProjectionChanged,
    ) -> None:
        kind = event.entity_kind
        row: Any = None
        if kind == "revision":
            row = await context.get(
                SemanticGuidelineRevisionRow,
                event.entity_id,
            )
            digest = None if row is None else row.revision_digest
        elif kind == "metric_definition":
            revision_id, separator, metric_id = event.entity_id.partition(":")
            row = await context.get(SemanticGuidelineRevisionRow, revision_id)
            metric = next(
                (
                    item
                    for item in (row.metrics if row is not None else ())
                    if isinstance(item, dict)
                    and item.get("metric_id") == metric_id
                ),
                None,
            )
            digest = (
                canonical_sha256(metric)
                if separator and metric is not None
                else None
            )
        elif kind == "binding_configuration":
            binding_id, separator, revision_text = event.entity_id.rpartition(":")
            parsed_revision = (
                int(revision_text)
                if separator and revision_text.isdigit()
                else -1
            )
            row = (
                await context.execute(
                    select(SemanticGuidelineBindingConfigurationRow).where(
                        SemanticGuidelineBindingConfigurationRow.board_id
                        == event.board_id,
                        SemanticGuidelineBindingConfigurationRow.binding_id
                        == binding_id,
                        SemanticGuidelineBindingConfigurationRow.binding_revision
                        == parsed_revision,
                    )
                )
            ).scalar_one_or_none()
            digest = None if row is None else row.configuration_digest
        elif kind == "assessment_receipt":
            row = (
                await context.execute(
                    select(SemanticGuidelineAssessmentReceiptRow).where(
                        SemanticGuidelineAssessmentReceiptRow.board_id
                        == event.board_id,
                        SemanticGuidelineAssessmentReceiptRow.receipt_id
                        == event.entity_id,
                        SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                    )
                )
            ).scalar_one_or_none()
            digest = None if row is None else row.receipt_digest
        elif kind == "metric_result":
            row = (
                await context.execute(
                    select(SemanticGuidelineMetricResultRow).where(
                        SemanticGuidelineMetricResultRow.board_id
                        == event.board_id,
                        SemanticGuidelineMetricResultRow.result_id
                        == event.entity_id,
                    )
                )
            ).scalar_one_or_none()
            digest = None if row is None else row.result_digest
        elif kind == "waiver":
            row = (
                await context.execute(
                    select(SemanticGuidelineWaiverEventRow).where(
                        SemanticGuidelineWaiverEventRow.board_id
                        == event.board_id,
                        SemanticGuidelineWaiverEventRow.waiver_id
                        == event.entity_id,
                        SemanticGuidelineWaiverEventRow.waiver_digest
                        == event.entity_digest,
                    )
                )
            ).scalar_one_or_none()
            digest = None if row is None else row.waiver_digest
        else:
            row = (
                await context.execute(
                    select(SemanticGuidelineSkipRow).where(
                        SemanticGuidelineSkipRow.board_id == event.board_id,
                        SemanticGuidelineSkipRow.skip_id == event.entity_id,
                        SemanticGuidelineSkipRow.skip_digest
                        == event.entity_digest,
                    )
                )
            ).scalar_one_or_none()
            digest = None if row is None else row.skip_digest
        if row is None or digest != event.entity_digest:
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_projection_authority_mismatch"
            )

    async def _validate_binding_event(
        self,
        context: Any,
        *,
        event: object,
    ) -> None:
        guideline_id = _required_text(
            getattr(event, "guideline_id", None),
            "semantic_guideline_event_guideline_required",
        )
        revision_id = _event_revision_id(event)
        semantic = (
            await context.execute(
                select(SemanticGuidelineRevisionRow).where(
                    SemanticGuidelineRevisionRow.guideline_id == guideline_id,
                    SemanticGuidelineRevisionRow.revision_id == revision_id,
                )
            )
        ).scalar_one_or_none()
        if semantic is None:
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_event_revision_missing"
            )
        binding_id = _required_text(
            getattr(event, "binding_id", None),
            "semantic_guideline_event_binding_required",
        )
        binding_revision = getattr(event, "binding_revision", None)
        configuration = (
            await context.execute(
                select(SemanticGuidelineBindingConfigurationRow).where(
                    SemanticGuidelineBindingConfigurationRow.board_id
                    == getattr(event, "board_id", None),
                    SemanticGuidelineBindingConfigurationRow.binding_id
                    == binding_id,
                    SemanticGuidelineBindingConfigurationRow.binding_revision
                    == binding_revision,
                )
            )
        ).scalar_one_or_none()
        if (
            configuration is None
            or configuration.guideline_id != guideline_id
            or configuration.revision_id != revision_id
            or configuration.revision_digest != semantic.revision_digest
        ):
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_event_binding_mismatch"
            )

    async def _desired_for_board(
        self,
        context: Any,
        *,
        board_id: str,
        projected_at: datetime,
    ) -> tuple[_SemanticDesiredNode, ...]:
        binding_pairs = (
            await context.execute(
                select(
                    SemanticGuidelineBindingConfigurationRow,
                    GuidelineBoardBindingRow,
                )
                .join(
                    GuidelineBoardBindingRow,
                    and_(
                        GuidelineBoardBindingRow.binding_id
                        == SemanticGuidelineBindingConfigurationRow.binding_id,
                        GuidelineBoardBindingRow.binding_revision
                        == SemanticGuidelineBindingConfigurationRow.binding_revision,
                        GuidelineBoardBindingRow.board_id
                        == SemanticGuidelineBindingConfigurationRow.board_id,
                    ),
                )
                .where(
                    SemanticGuidelineBindingConfigurationRow.board_id
                    == board_id
                )
                .order_by(
                    GuidelineBoardBindingRow.guideline_id.asc(),
                    GuidelineBoardBindingRow.binding_revision.asc(),
                )
            )
        ).all()
        retired_guidelines = set(
            (
                await context.execute(
                    select(GuidelineRetirementRow.guideline_id)
                )
            ).scalars()
        )
        latest_binding: dict[str, GuidelineBoardBindingRow] = {}
        for _, legacy in binding_pairs:
            latest_binding[legacy.guideline_id] = legacy
        active_binding_keys = {
            (row.binding_id, row.binding_revision)
            for row in latest_binding.values()
            if row.state == "active" and row.guideline_id not in retired_guidelines
        }
        active_revision_ids = {
            configuration.revision_id
            for configuration, legacy in binding_pairs
            if (legacy.binding_id, legacy.binding_revision) in active_binding_keys
        }

        referenced_revision_ids = {
            configuration.revision_id for configuration, _ in binding_pairs
        }
        revision_pairs = []
        if referenced_revision_ids:
            revision_pairs = (
                await context.execute(
                    select(SemanticGuidelineRevisionRow, GuidelineRevisionRow)
                    .join(
                        GuidelineRevisionRow,
                        GuidelineRevisionRow.revision_id
                        == SemanticGuidelineRevisionRow.revision_id,
                    )
                    .where(
                        SemanticGuidelineRevisionRow.revision_id.in_(
                            referenced_revision_ids
                        )
                    )
                    .order_by(
                        GuidelineRevisionRow.guideline_id.asc(),
                        GuidelineRevisionRow.revision_number.asc(),
                    )
                )
            ).all()
        if {semantic.revision_id for semantic, _ in revision_pairs} != (
            referenced_revision_ids
        ):
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_revision_authority_incomplete"
            )

        desired: list[_SemanticDesiredNode] = []
        binding_successors: dict[tuple[str, int], str] = {}
        by_guideline: dict[str, list[tuple[Any, Any]]] = {}
        for pair in binding_pairs:
            by_guideline.setdefault(pair[1].guideline_id, []).append(pair)
        for pairs in by_guideline.values():
            for index, (configuration, legacy) in enumerate(pairs[:-1]):
                next_configuration, _ = pairs[index + 1]
                binding_successors[(legacy.binding_id, legacy.binding_revision)] = (
                    _semantic_node_id(
                        "binding_configuration",
                        f"{next_configuration.binding_id}:"
                        f"{next_configuration.binding_revision}",
                    )
                )
        for configuration, legacy in binding_pairs:
            active = (
                legacy.binding_id,
                legacy.binding_revision,
            ) in active_binding_keys
            successor = binding_successors.get(
                (legacy.binding_id, legacy.binding_revision)
            )
            reason = None
            if not active:
                reason = (
                    "semantic_guideline_binding_unlinked"
                    if legacy.state == "unlinked" and successor is None
                    else "semantic_guideline_binding_superseded"
                )
            desired.append(
                _desired_semantic_node(
                    kind="binding_configuration",
                    identity=f"{configuration.binding_id}:"
                    f"{configuration.binding_revision}",
                    digest=configuration.configuration_digest,
                    generation=configuration.binding_revision,
                    active=active,
                    reason=reason,
                    successor_id=successor,
                    lineage_ids=(
                        _semantic_node_id(
                            "revision", configuration.revision_id
                        ),
                    ),
                    title=f"Guideline binding {configuration.guideline_id}",
                    content=(
                        f"{configuration.enforcement}; minimum confidence "
                        f"{configuration.minimum_confidence}"
                    ),
                    payload={
                        "binding_id": configuration.binding_id,
                        "binding_revision": configuration.binding_revision,
                        "board_id": board_id,
                        "guideline_id": configuration.guideline_id,
                        "revision_id": configuration.revision_id,
                        "revision_digest": configuration.revision_digest,
                        "configuration_digest": configuration.configuration_digest,
                        "enforcement": configuration.enforcement,
                        "minimum_confidence": configuration.minimum_confidence,
                        "metric_threshold_overrides": configuration.metric_threshold_overrides,
                        "state": legacy.state,
                    },
                    created_at=configuration.configured_at,
                    projected_at=projected_at,
                )
            )

        revision_successor: dict[str, str] = {}
        revision_groups: dict[str, list[tuple[Any, Any]]] = {}
        for pair in revision_pairs:
            revision_groups.setdefault(pair[1].guideline_id, []).append(pair)
        for pairs in revision_groups.values():
            for index, (semantic, _) in enumerate(pairs[:-1]):
                next_semantic, _ = pairs[index + 1]
                revision_successor[semantic.revision_id] = _semantic_node_id(
                    "revision", next_semantic.revision_id
                )
        for semantic, legacy in revision_pairs:
            active = semantic.revision_id in active_revision_ids
            if semantic.authority_state == "legacy_incompatible" and active:
                raise PolicyConstraintProjectionConflict(
                    "semantic_guideline_legacy_revision_active"
                )
            successor = revision_successor.get(semantic.revision_id)
            desired.append(
                _desired_semantic_node(
                    kind="revision",
                    identity=semantic.revision_id,
                    digest=semantic.revision_digest,
                    generation=legacy.revision_number,
                    active=active,
                    reason=(
                        None
                        if active
                        else "semantic_guideline_revision_legacy_incompatible"
                        if semantic.authority_state == "legacy_incompatible"
                        else "semantic_guideline_revision_superseded"
                        if successor
                        else "semantic_guideline_revision_unbound"
                    ),
                    successor_id=successor,
                    title=legacy.title,
                    content=legacy.content,
                    payload={
                        "guideline_id": semantic.guideline_id,
                        "revision_id": semantic.revision_id,
                        "revision_number": legacy.revision_number,
                        "semantic_version": legacy.semantic_version,
                        "revision_digest": semantic.revision_digest,
                        "authority_state": semantic.authority_state,
                    },
                    created_at=semantic.created_at,
                    projected_at=projected_at,
                )
            )
            if semantic.authority_state == "legacy_incompatible":
                continue
            if not isinstance(semantic.metrics, list):
                raise PolicyConstraintProjectionConflict(
                    "semantic_guideline_metrics_invalid"
                )
            for metric in semantic.metrics:
                if not isinstance(metric, dict) or not isinstance(
                    metric.get("metric_id"), str
                ):
                    raise PolicyConstraintProjectionConflict(
                        "semantic_guideline_metric_invalid"
                    )
                metric_id = metric["metric_id"]
                metric_successor = None
                if successor is not None:
                    successor_revision_id = successor.rsplit(":", 1)[-1]
                    successor_pair = next(
                        (
                            item
                            for item in revision_pairs
                            if item[0].revision_id == successor_revision_id
                        ),
                        None,
                    )
                    if successor_pair is not None:
                        successor_metric = next(
                            (
                                item
                                for item in successor_pair[0].metrics
                                if isinstance(item, dict)
                                and item.get("code") == metric.get("code")
                            ),
                            None,
                        )
                        if successor_metric is not None:
                            metric_successor = _semantic_node_id(
                                "metric_definition",
                                f"{successor_revision_id}:"
                                f"{successor_metric['metric_id']}",
                            )
                desired.append(
                    _desired_semantic_node(
                        kind="metric_definition",
                        identity=f"{semantic.revision_id}:{metric_id}",
                        digest=canonical_sha256(metric),
                        generation=legacy.revision_number,
                        active=active,
                        reason=(
                            None
                            if active
                            else "semantic_guideline_metric_superseded"
                            if metric_successor
                            else "semantic_guideline_metric_unbound"
                        ),
                        successor_id=metric_successor,
                        lineage_ids=(
                            _semantic_node_id(
                                "revision", semantic.revision_id
                            ),
                        ),
                        title=str(metric.get("title") or metric.get("code")),
                        content=str(metric.get("description") or ""),
                        payload={
                            "guideline_id": semantic.guideline_id,
                            "revision_id": semantic.revision_id,
                            "revision_digest": semantic.revision_digest,
                            "metric": metric,
                        },
                        created_at=semantic.created_at,
                        projected_at=projected_at,
                    )
                )

        receipts = tuple(
            (
                await context.execute(
                    select(SemanticGuidelineAssessmentReceiptRow)
                    .where(
                        SemanticGuidelineAssessmentReceiptRow.board_id
                        == board_id,
                        SemanticGuidelineAssessmentReceiptRow.sealed.is_(True),
                    )
                    .order_by(
                        SemanticGuidelineAssessmentReceiptRow.assessed_at.asc(),
                        SemanticGuidelineAssessmentReceiptRow.receipt_id.asc(),
                    )
                )
            ).scalars()
        )
        latest_receipt: dict[tuple[str, str, str], Any] = {}
        for receipt in receipts:
            latest_receipt[
                (receipt.subject_type, receipt.subject_id, receipt.binding_id)
            ] = receipt
        active_receipt_ids = {
            receipt.receipt_id
            for receipt in latest_receipt.values()
            if (receipt.binding_id, receipt.binding_revision)
            in active_binding_keys
        }
        receipt_successors: dict[str, str] = {}
        receipt_groups: dict[tuple[str, str, str], list[Any]] = {}
        for receipt in receipts:
            receipt_groups.setdefault(
                (receipt.subject_type, receipt.subject_id, receipt.binding_id), []
            ).append(receipt)
        for group in receipt_groups.values():
            for index, receipt in enumerate(group[:-1]):
                receipt_successors[receipt.receipt_id] = _semantic_node_id(
                    "assessment_receipt", group[index + 1].receipt_id
                )
        for receipt in receipts:
            active = receipt.receipt_id in active_receipt_ids
            successor = receipt_successors.get(receipt.receipt_id)
            desired.append(
                _desired_semantic_node(
                    kind="assessment_receipt",
                    identity=receipt.receipt_id,
                    digest=receipt.receipt_digest,
                    generation=receipt.subject_version,
                    active=active,
                    reason=(
                        None
                        if active
                        else "semantic_guideline_assessment_superseded"
                        if successor
                        else "semantic_guideline_assessment_binding_inactive"
                    ),
                    successor_id=successor,
                    lineage_ids=(
                        _semantic_node_id(
                            "binding_configuration",
                            f"{receipt.binding_id}:{receipt.binding_revision}",
                        ),
                        _semantic_node_id("revision", receipt.revision_id),
                    ),
                    title=f"{receipt.subject_type} guideline assessment",
                    content=(
                        f"{receipt.state}; confidence {receipt.confidence}; "
                        f"failed metrics {receipt.failed_metric_count}"
                    ),
                    payload={
                        "receipt_id": receipt.receipt_id,
                        "receipt_digest": receipt.receipt_digest,
                        "subject_type": receipt.subject_type,
                        "subject_id": receipt.subject_id,
                        "subject_version": receipt.subject_version,
                        "guideline_id": receipt.guideline_id,
                        "revision_id": receipt.revision_id,
                        "binding_id": receipt.binding_id,
                        "binding_revision": receipt.binding_revision,
                        "configuration_digest": receipt.configuration_digest,
                        "state": receipt.state,
                        "confidence": receipt.confidence,
                    },
                    created_at=receipt.assessed_at,
                    projected_at=projected_at,
                )
            )

        metric_results = tuple(
            (
                await context.execute(
                    select(SemanticGuidelineMetricResultRow)
                    .where(
                        SemanticGuidelineMetricResultRow.board_id == board_id
                    )
                    .order_by(
                        SemanticGuidelineMetricResultRow.created_at.asc(),
                        SemanticGuidelineMetricResultRow.result_id.asc(),
                    )
                )
            ).scalars()
        )
        receipt_by_id = {receipt.receipt_id: receipt for receipt in receipts}
        for result in metric_results:
            receipt = receipt_by_id.get(result.receipt_id)
            if receipt is None:
                raise PolicyConstraintProjectionConflict(
                    "semantic_guideline_metric_receipt_missing"
                )
            active = result.receipt_id in active_receipt_ids
            desired.append(
                _desired_semantic_node(
                    kind="metric_result",
                    identity=result.result_id,
                    digest=result.result_digest,
                    generation=result.subject_version,
                    active=active,
                    reason=(
                        None
                        if active
                        else "semantic_guideline_metric_result_superseded"
                    ),
                    successor_id=None,
                    lineage_ids=(
                        _semantic_node_id(
                            "assessment_receipt", result.receipt_id
                        ),
                        _semantic_node_id(
                            "metric_definition",
                            f"{result.revision_id}:{result.metric_id}",
                        ),
                    ),
                    title=f"{result.metric_code} result",
                    content=f"score {result.score}; outcome {result.outcome}",
                    payload={
                        "result_id": result.result_id,
                        "receipt_id": result.receipt_id,
                        "result_digest": result.result_digest,
                        "metric_id": result.metric_id,
                        "metric_code": result.metric_code,
                        "metric_definition_digest": result.metric_definition_digest,
                        "score": result.score,
                        "outcome": result.outcome,
                        "rationale": result.rationale,
                    },
                    created_at=result.created_at,
                    projected_at=projected_at,
                )
            )

        waivers = tuple(
            (
                await context.execute(
                    select(SemanticGuidelineWaiverRow).where(
                        SemanticGuidelineWaiverRow.board_id == board_id
                    )
                )
            ).scalars()
        )
        for waiver in waivers:
            active, reason = _waiver_projection_state(
                status=waiver.status,
                expires_at=waiver.expires_at,
                binding_id=waiver.binding_id,
                binding_revision=waiver.binding_revision,
                active_binding_keys=active_binding_keys,
                projected_at=projected_at,
            )
            desired.append(
                _desired_semantic_node(
                    kind="waiver",
                    identity=waiver.waiver_id,
                    digest=waiver.head_digest,
                    generation=waiver.waiver_revision,
                    active=active,
                    reason=reason,
                    successor_id=None,
                    lineage_ids=(
                        _semantic_node_id(
                            "metric_result", waiver.metric_result_id
                        ),
                        _semantic_node_id(
                            "assessment_receipt", waiver.receipt_id
                        ),
                    ),
                    title=f"Waiver for {waiver.metric_code}",
                    content=waiver.justification,
                    payload={
                        "waiver_id": waiver.waiver_id,
                        "status": waiver.status,
                        "waiver_revision": waiver.waiver_revision,
                        "head_digest": waiver.head_digest,
                        "receipt_id": waiver.receipt_id,
                        "metric_result_id": waiver.metric_result_id,
                        "finding_id": waiver.finding_id,
                        "binding_id": waiver.binding_id,
                    },
                    created_at=waiver.requested_at,
                    projected_at=projected_at,
                )
            )

        skip_rows = tuple(
            (
                await context.execute(
                    select(SemanticGuidelineSkipRow)
                    .where(SemanticGuidelineSkipRow.board_id == board_id)
                    .order_by(
                        SemanticGuidelineSkipRow.skip_id.asc(),
                        SemanticGuidelineSkipRow.skip_revision.asc(),
                    )
                )
            ).scalars()
        )
        skip_heads: dict[str, Any] = {}
        for row in skip_rows:
            skip_heads[row.skip_id] = row
        for skip in skip_heads.values():
            active = (
                skip.status == "active"
                and (skip.binding_id, skip.binding_revision)
                in active_binding_keys
            )
            desired.append(
                _desired_semantic_node(
                    kind="skip",
                    identity=skip.skip_id,
                    digest=skip.skip_digest,
                    generation=skip.skip_revision,
                    active=active,
                    reason=(
                        None
                        if active
                        else "semantic_guideline_skip_revoked"
                        if skip.status == "revoked"
                        else "semantic_guideline_skip_binding_inactive"
                    ),
                    successor_id=None,
                    lineage_ids=(
                        _semantic_node_id(
                            "binding_configuration",
                            f"{skip.binding_id}:{skip.binding_revision}",
                        ),
                    ),
                    title="Human guideline gate skip",
                    content=skip.reason,
                    payload={
                        "skip_id": skip.skip_id,
                        "skip_revision": skip.skip_revision,
                        "status": skip.status,
                        "scope_digest": skip.scope_digest,
                        "skip_digest": skip.skip_digest,
                        "subject_type": skip.subject_type,
                        "subject_id": skip.subject_id,
                        "binding_id": skip.binding_id,
                    },
                    created_at=skip.created_at,
                    projected_at=projected_at,
                )
            )

        node_ids = [node.node_id for node in desired]
        if len(node_ids) != len(set(node_ids)):
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_desired_identity_duplicate"
            )
        return tuple(sorted(desired, key=lambda item: item.node_id))

    async def _reconcile(
        self,
        *,
        board_id: str,
        operation: str,
        event_id: str | None,
        desired: tuple[_SemanticDesiredNode, ...],
        projected_at: datetime,
    ) -> PolicyConstraintProjectionResult:
        graph = self._graph_transaction_resolver()
        if graph is None:
            raise PolicyConstraintProjectionConflict(
                "semantic_guideline_graph_transaction_missing"
            )
        scope = await graph.begin(board_id)
        desired_by_id = {node.node_id: node for node in desired}
        source_session_id = (
            f"semantic-guideline:{event_id}"
            if event_id is not None
            else f"semantic-guideline:rebuild:{board_id}"
        )
        activated = 0
        ended = 0
        async with scope:
            transaction_open = False
            try:
                scope.execute("BEGIN TRANSACTION")
                transaction_open = True
                current = _semantic_graph_nodes(scope)
                current_by_id = {node.node_id: node for node in current}
                for node in desired:
                    existing = current_by_id.get(node.node_id)
                    if existing is None:
                        scope.create_node(
                            SEMANTIC_GUIDELINE_KG_NODE_TYPE,
                            node.node_id,
                            dict(node.attrs),
                            source_session_id=source_session_id,
                        )
                        if node.active:
                            activated += 1
                    elif not _semantic_node_matches(existing, node):
                        was_active = existing.active
                        scope.update_node(
                            SEMANTIC_GUIDELINE_KG_NODE_TYPE,
                            node.node_id,
                            {
                                key: value
                                for key, value in node.attrs.items()
                                if key not in {"created_at", "superseded_at"}
                            },
                        )
                        if node.active:
                            scope.execute(
                                "MATCH (n:Entity {id: $node_id}) SET "
                                "n.superseded_at = NULL",
                                {"node_id": node.node_id},
                            )
                            if not was_active:
                                activated += 1
                        else:
                            scope.execute(
                                "MATCH (n:Entity {id: $node_id}) SET "
                                "n.superseded_at = timestamp($ended_at)",
                                {
                                    "node_id": node.node_id,
                                    "ended_at": projected_at.isoformat(),
                                },
                            )
                            if was_active:
                                ended += 1

                for existing in current:
                    if existing.node_id in desired_by_id or not existing.active:
                        continue
                    scope.execute(
                        "MATCH (n:Entity {id: $node_id}) SET "
                        "n.superseded_by = NULL, "
                        "n.superseded_at = timestamp($ended_at), "
                        "n.revocation_reason = $reason",
                        {
                            "node_id": existing.node_id,
                            "ended_at": projected_at.isoformat(),
                            "reason": SEMANTIC_GUIDELINE_SOURCE_REMOVED_REASON,
                        },
                    )
                    ended += 1

                legacy = _legacy_rule_nodes(scope)
                for node in legacy:
                    if not node.active:
                        continue
                    scope.execute(
                        "MATCH (n:Constraint {id: $node_id}) SET "
                        "n.superseded_by = NULL, "
                        "n.superseded_at = timestamp($ended_at), "
                        "n.revocation_reason = $reason",
                        {
                            "node_id": node.node_id,
                            "ended_at": projected_at.isoformat(),
                            "reason": SEMANTIC_GUIDELINE_LEGACY_TERMINATED_REASON,
                        },
                    )
                    ended += 1

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
                    for node in desired:
                        if not scope.edge_exists(
                            "belongs_to",
                            "Entity",
                            "Entity",
                            node.node_id,
                            root_id,
                        ):
                            scope.create_edge(
                                "belongs_to",
                                "Entity",
                                "Entity",
                                node.node_id,
                                root_id,
                                {
                                    "confidence": 1.0,
                                    "created_by_session_id": source_session_id,
                                    "created_at": projected_at.isoformat(),
                                    "rule_id": SEMANTIC_GUIDELINE_KG_ROOT_RULE,
                                },
                            )
                for node in desired:
                    if node.successor_id is None:
                        pass
                    elif not scope.edge_exists(
                        "supersedes",
                        "Entity",
                        "Entity",
                        node.successor_id,
                        node.node_id,
                    ):
                        scope.create_edge(
                            "supersedes",
                            "Entity",
                            "Entity",
                            node.successor_id,
                            node.node_id,
                            {
                                "confidence": 1.0,
                                "created_by_session_id": source_session_id,
                                "created_at": projected_at.isoformat(),
                                "rule_id": SEMANTIC_GUIDELINE_KG_LINEAGE_RULE,
                            },
                        )
                    for target_id in node.lineage_ids:
                        if target_id not in desired_by_id:
                            raise PolicyConstraintProjectionConflict(
                                "semantic_guideline_lineage_target_missing"
                            )
                        if scope.edge_exists(
                            "belongs_to",
                            "Entity",
                            "Entity",
                            node.node_id,
                            target_id,
                        ):
                            continue
                        scope.create_edge(
                            "belongs_to",
                            "Entity",
                            "Entity",
                            node.node_id,
                            target_id,
                            {
                                "confidence": 1.0,
                                "created_by_session_id": source_session_id,
                                "created_at": projected_at.isoformat(),
                                "rule_id": (
                                    "belongs_to/semantic_guideline_lineage@"
                                    "semantic-guideline-kg/v1"
                                ),
                            },
                        )

                verified = _semantic_graph_nodes(scope)
                active_ids = tuple(
                    sorted(node.node_id for node in verified if node.active)
                )
                desired_active_ids = tuple(
                    sorted(node.node_id for node in desired if node.active)
                )
                if active_ids != desired_active_ids:
                    raise PolicyConstraintProjectionConflict(
                        "semantic_guideline_reconciliation_unconfirmed"
                    )
                if any(node.active for node in _legacy_rule_nodes(scope)):
                    raise PolicyConstraintProjectionConflict(
                        "semantic_guideline_legacy_constraint_active"
                    )
                scope.execute("COMMIT")
                transaction_open = False
            except BaseException:
                if transaction_open:
                    try:
                        scope.execute("ROLLBACK")
                    except BaseException as rollback_error:
                        raise PolicyConstraintProjectionConflict(
                            "semantic_guideline_transaction_cleanup_unconfirmed"
                        ) from rollback_error
                raise
        active_node_ids = tuple(
            sorted(node.node_id for node in desired if node.active)
        )
        return PolicyConstraintProjectionResult(
            board_id=board_id,
            operation=operation,
            event_id=event_id,
            activated_count=activated,
            ended_count=ended,
            active_count=len(active_node_ids),
            unadopted_active_count=0,
            node_ids=active_node_ids,
            replayed=activated == 0 and ended == 0,
        )

    async def apply(
        self,
        context: Any,
        *,
        event: object,
    ) -> PolicyConstraintProjectionResult:
        board_id = _required_text(
            getattr(event, "board_id", None),
            "semantic_guideline_event_board_required",
        )
        event_id = _required_text(
            getattr(event, "event_id", None),
            "semantic_guideline_event_id_required",
        )
        projected_at = _aware_utc(
            getattr(event, "occurred_at", None),
            "semantic_guideline_event_time_invalid",
        )
        if isinstance(event, SemanticGuidelineProjectionChanged):
            await self._validate_generic_event(context, event=event)
            operation = "sync"
        else:
            await self._validate_binding_event(context, event=event)
            operation = _required_text(
                getattr(event, "operation", None),
                "semantic_guideline_event_operation_required",
            )
        desired = await self._desired_for_board(
            context,
            board_id=board_id,
            projected_at=projected_at,
        )
        return await self._reconcile(
            board_id=board_id,
            operation=operation,
            event_id=event_id,
            desired=desired,
            projected_at=projected_at,
        )

    async def rebuild_board(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> PolicyConstraintProjectionResult:
        normalized_board_id = _required_text(
            board_id,
            "semantic_guideline_rebuild_board_required",
        )
        projected_at = datetime.now(timezone.utc)
        desired = await self._desired_for_board(
            context,
            board_id=normalized_board_id,
            projected_at=projected_at,
        )
        return await self._reconcile(
            board_id=normalized_board_id,
            operation="rebuild",
            event_id=None,
            desired=desired,
            projected_at=projected_at,
        )


__all__ = [
    "CommunitySqlAlchemyPolicyConstraintProjection",
    "POLICY_CONSTRAINT_ACTOR",
    "PolicyConstraintProjectionConflict",
    "SEMANTIC_GUIDELINE_KG_ACTOR",
    "SEMANTIC_GUIDELINE_KG_CONTRACT",
    "SEMANTIC_GUIDELINE_KG_LINEAGE_RULE",
    "SEMANTIC_GUIDELINE_KG_NODE_TYPE",
    "SEMANTIC_GUIDELINE_KG_ROOT_RULE",
    "SEMANTIC_GUIDELINE_LEGACY_TERMINATED_REASON",
]
