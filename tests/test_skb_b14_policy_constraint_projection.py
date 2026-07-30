"""SK-B/B14 exact adopted-revision Constraint projection."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventDeliveryStore,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    guideline_revision_content_digest,
    guideline_rule_payload,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
    Guideline as GuidelineRow,
    GuidelineBoardBindingRow,
    GuidelineRetirementRow,
    GuidelineRevisionRow,
)
from okto_pulse.community.adapters.sqlalchemy_policy_constraint_projection import (
    CommunitySqlAlchemyPolicyConstraintProjection,
    POLICY_CONSTRAINT_ACTOR,
    POLICY_CONSTRAINT_LINEAGE_RULE,
    POLICY_CONSTRAINT_REVISION_SUPERSEDED_REASON,
    PolicyConstraintProjectionConflict,
)
from okto_pulse.core.domain.guideline_policy import (
    GuidelineEnforcement,
    GuidelinePredicate,
    GuidelineRule,
    PolicyEntityType,
)
from okto_pulse.core.application.domain_event_delivery import (
    DomainEventDeliveryProcessor,
)
from okto_pulse.core.events.types import (
    PolicyAdoptionChanged,
    PolicyBindingMaterialized,
)
from okto_pulse.core.kg.cypher_templates import is_visible_in_active_reads
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.ports.policy_constraint_projection import (
    POLICY_CONSTRAINT_GUIDELINE_SUPERSEDED_REASON,
    POLICY_CONSTRAINT_RULE_REMOVED_REASON,
    POLICY_CONSTRAINT_UNLINKED_REASON,
)


NOW = datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc)
BOARD_ID = "board-b14"
GUIDELINE_ID = "guideline-b14"
UNADOPTED_GUIDELINE_ID = "guideline-b14-unadopted"
REVISION_1 = "revision-b14-v1"
REVISION_2 = "revision-b14-v2"
UNADOPTED_REVISION = "revision-b14-unadopted"


class _FakeGraphScope:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, dict[str, Any]]] = {
            "Constraint": {},
            "Entity": {},
        }
        self.edges: dict[
            tuple[str, str, str, str, str],
            dict[str, Any],
        ] = {}
        self._before: tuple[
            dict[str, dict[str, dict[str, Any]]],
            dict[tuple[str, str, str, str, str], dict[str, Any]],
        ] | None = None

    async def __aenter__(self) -> "_FakeGraphScope":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        params = params or {}
        normalized = " ".join(statement.split())
        if normalized == "BEGIN TRANSACTION":
            self._before = (deepcopy(self.nodes), deepcopy(self.edges))
            return GraphStatementResult()
        if normalized == "COMMIT":
            self._before = None
            return GraphStatementResult()
        if normalized == "ROLLBACK":
            assert self._before is not None
            self.nodes, self.edges = self._before
            self._before = None
            return GraphStatementResult()
        if normalized.startswith("MATCH (n:Constraint) RETURN n.id"):
            rows = []
            for node_id, attrs in sorted(self.nodes["Constraint"].items()):
                rows.append(
                    (
                        node_id,
                        attrs.get("source_artifact_ref"),
                        attrs.get("created_by_agent"),
                        attrs.get("revocation_reason"),
                        attrs.get("superseded_by"),
                        attrs.get("source_content_hash"),
                        attrs.get("title"),
                        attrs.get("content"),
                        attrs.get("context"),
                        attrs.get("justification"),
                    )
                )
            return GraphStatementResult.from_rows(rows)
        if normalized.startswith(
            "MATCH (n:Entity) WHERE n.source_artifact_ref = $source_ref"
        ):
            rows = [
                (node_id,)
                for node_id, attrs in sorted(self.nodes["Entity"].items())
                if attrs.get("source_artifact_ref") == params["source_ref"]
            ]
            return GraphStatementResult.from_rows(rows)
        if normalized.startswith(
            "MATCH (:Constraint)-[r:supersedes]->"
            "(old:Constraint {id: $node_id}) "
            "WHERE r.rule_id = $rule_id DELETE r"
        ):
            stale = [
                key
                for key, attrs in self.edges.items()
                if (
                    key[0] == "supersedes"
                    and key[4] == params["node_id"]
                    and attrs.get("rule_id") == params["rule_id"]
                )
            ]
            for key in stale:
                self.edges.pop(key)
            return GraphStatementResult()
        if normalized.startswith(
            "MATCH (new:Constraint {id: $successor_id})"
            "-[r:supersedes]->(old:Constraint {id: $predecessor_id})"
        ):
            count = sum(
                1
                for key, attrs in self.edges.items()
                if (
                    key[0] == "supersedes"
                    and key[3] == params["successor_id"]
                    and key[4] == params["predecessor_id"]
                    and attrs.get("rule_id") == params["rule_id"]
                )
            )
            return GraphStatementResult.from_rows(((count,),))
        if normalized.startswith("MATCH (n:Constraint {id: $node_id}) SET"):
            node = self.nodes["Constraint"][str(params["node_id"])]
            if "ended_at" not in params:
                node["superseded_by"] = None
                node["superseded_at"] = None
                node["revocation_reason"] = None
                return GraphStatementResult()
            node["superseded_by"] = params["superseded_by"]
            node["superseded_at"] = params["ended_at"]
            node["revocation_reason"] = params["reason"]
            return GraphStatementResult()
        raise AssertionError(f"unexpected statement: {normalized}")

    def create_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
        *,
        source_session_id: str,
    ) -> None:
        assert node_id not in self.nodes[node_type]
        self.nodes[node_type][node_id] = {
            **deepcopy(attrs),
            "source_session_id": source_session_id,
        }

    def update_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        self.nodes[node_type][node_id].update(deepcopy(attrs))

    def edge_exists(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
    ) -> bool:
        return (
            edge_type,
            from_type,
            to_type,
            from_id,
            to_id,
        ) in self.edges

    def create_edge(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        attrs: dict[str, Any],
    ) -> bool:
        key = (edge_type, from_type, to_type, from_id, to_id)
        assert key not in self.edges
        self.edges[key] = deepcopy(attrs)
        return True


class _FakeGraph:
    def __init__(self) -> None:
        self.scope = _FakeGraphScope()
        self.requested_boards: list[str] = []

    async def begin(self, board_id: str) -> _FakeGraphScope:
        self.requested_boards.append(board_id)
        return self.scope


class _AlwaysFailingPolicyConstraintHandler:
    async def handle(
        self,
        _event: PolicyAdoptionChanged,
        _session: Any,
    ) -> None:
        raise RuntimeError("policy constraint graph unavailable")


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def _rule(rule_id: str, code: str, title: str) -> GuidelineRule:
    return GuidelineRule(
        rule_id=rule_id,
        code=code,
        title=title,
        description=f"{title} must hold.",
        target_entity_types=(PolicyEntityType.SPEC,),
        predicates=(
            GuidelinePredicate(
                predicate_code="field.exists",
                parameters=(("field", "description"),),
            ),
        ),
        enforcement=GuidelineEnforcement.BLOCKING,
    )


def _revision_row(
    *,
    guideline_id: str,
    revision_id: str,
    revision_number: int,
    rules: tuple[GuidelineRule, ...],
    parent_revision_id: str | None = None,
) -> GuidelineRevisionRow:
    title = f"{guideline_id} revision {revision_number}"
    content = f"Immutable policy revision {revision_number}."
    semantic_version = f"{revision_number}.0.0"
    return GuidelineRevisionRow(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=revision_number,
        semantic_version=semantic_version,
        title=title,
        content=content,
        content_digest=guideline_revision_content_digest(
            title=title,
            content=content,
            rules=rules,
        ),
        tags=[],
        rules=[guideline_rule_payload(rule) for rule in rules],
        created_by="agent-b14",
        created_at=NOW + timedelta(minutes=revision_number),
        published_head_revision=revision_number,
        published_head_updated_at=NOW + timedelta(minutes=revision_number),
        parent_revision_id=parent_revision_id,
        legacy_version=None,
        legacy_version_unresolvable=False,
        legacy_tags=None,
        idempotency_key=f"revision:{revision_id}",
        request_digest=str(revision_number) * 64,
    )


def _binding(
    *,
    revision: GuidelineRevisionRow,
    binding_revision: int,
    state: str,
) -> GuidelineBoardBindingRow:
    return GuidelineBoardBindingRow(
        binding_id="binding-b14",
        binding_revision=binding_revision,
        board_id=BOARD_ID,
        guideline_id=GUIDELINE_ID,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        priority=0,
        adopted_by="agent-b14",
        adopted_at=NOW + timedelta(minutes=10 + binding_revision),
        default_enforcement="blocking",
        source_kind="native",
        legacy_source_id=None,
        legacy_guideline_version=None,
        legacy_template_id=None,
        legacy_template_version=None,
        legacy_version_unresolvable=False,
        idempotency_key=f"binding:{binding_revision}",
        request_digest=str(binding_revision) * 64,
        state=state,
        binding_origin="native",
    )


def _event(
    *,
    operation: str,
    revision: GuidelineRevisionRow,
    ordinal: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        operation=operation,
        board_id=BOARD_ID,
        guideline_id=GUIDELINE_ID,
        exact_revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        to_revision_id=revision.revision_id,
        to_semantic_version=revision.semantic_version,
        to_revision_digest=revision.content_digest,
        from_revision_id=revision.revision_id,
        from_semantic_version=revision.semantic_version,
        from_revision_digest=revision.content_digest,
        event_id=f"event-b14-{ordinal}",
        occurred_at=NOW + timedelta(minutes=20 + ordinal),
    )


@pytest.mark.asyncio
async def test_b14_projects_only_latest_adopted_exact_revision_and_replays(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b14-projection.sqlite3")
    rules_v1 = (
        _rule("rule-a", "SPEC.description", "Description required"),
        _rule("rule-b", "SPEC.tests", "Tests required"),
    )
    # ``rule-a`` is semantically unchanged, but its v2 identity must still be
    # a new revision-scoped node; the v1 node is ended.
    rules_v2 = (
        rules_v1[0],
        _rule("rule-c", "SPEC.security", "Security review required"),
    )
    unadopted_rule = _rule(
        "rule-unadopted",
        "SPEC.unadopted",
        "Never active without a binding",
    )
    revision_v1 = _revision_row(
        guideline_id=GUIDELINE_ID,
        revision_id=REVISION_1,
        revision_number=1,
        rules=rules_v1,
    )
    revision_v2 = _revision_row(
        guideline_id=GUIDELINE_ID,
        revision_id=REVISION_2,
        revision_number=2,
        rules=rules_v2,
        parent_revision_id=REVISION_1,
    )
    unadopted_revision = _revision_row(
        guideline_id=UNADOPTED_GUIDELINE_ID,
        revision_id=UNADOPTED_REVISION,
        revision_number=1,
        rules=(unadopted_rule,),
    )
    async with get_session_factory()() as session:
        session.add(Board(id=BOARD_ID, name="B14", owner_id="owner-b14"))
        session.add_all(
            [
                GuidelineRow(
                    id=GUIDELINE_ID,
                    title="B14 adopted",
                    content="Adopted guideline.",
                    tags=[],
                    scope="global",
                    board_id=None,
                    owner_id="owner-b14",
                    version=2,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                GuidelineRow(
                    id=UNADOPTED_GUIDELINE_ID,
                    title="B14 unadopted",
                    content="Never adopted.",
                    tags=[],
                    scope="global",
                    board_id=None,
                    owner_id="owner-b14",
                    version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.add_all([revision_v1, revision_v2, unadopted_revision])
        session.add(
            _binding(
                revision=revision_v1,
                binding_revision=1,
                state="active",
            )
        )
        await session.commit()

    graph = _FakeGraph()
    projection = CommunitySqlAlchemyPolicyConstraintProjection(
        graph_transaction_resolver=lambda: graph
    )
    async with get_session_factory()() as session:
        first = await projection.apply(
            session,
            event=_event(
                operation="adopt",
                revision=revision_v1,
                ordinal=1,
            ),
        )
    expected_v1_ids = tuple(
        sorted(
            f"guideline-revision:{REVISION_1}:rule:{rule.rule_id}"
            for rule in rules_v1
        )
    )
    assert first.activated_count == 2
    assert first.ended_count == 0
    assert first.active_count == 2
    assert first.unadopted_active_count == 0
    assert first.node_ids == expected_v1_ids
    assert first.replayed is False
    assert all(
        node["created_by_agent"] == POLICY_CONSTRAINT_ACTOR
        for node in graph.scope.nodes["Constraint"].values()
    )
    assert not any(
        UNADOPTED_REVISION in node_id
        for node_id in graph.scope.nodes["Constraint"]
    )
    materialized = PolicyBindingMaterialized(
        event_id="event-b14-materialized",
        board_id=BOARD_ID,
        actor_id="agent-b14",
        actor_type="agent",
        occurred_at=NOW + timedelta(minutes=11),
        event_schema_version="policy-binding-materialized/v1",
        operation="adopt",
        guideline_id=GUIDELINE_ID,
        binding_id="binding-b14",
        binding_revision=1,
        revision_id=REVISION_1,
        semantic_version=revision_v1.semantic_version,
        revision_digest=revision_v1.content_digest,
        source_kind="native",
        default_enforcement="blocking",
        priority=0,
    )
    async with get_session_factory()() as session:
        exact_materialized = await projection.apply(
            session,
            event=materialized,
        )
    assert exact_materialized.replayed is True
    mismatched_materialized = materialized.model_copy(
        update={"event_id": "event-b14-materialized-tampered", "priority": 1}
    )
    async with get_session_factory()() as session:
        with pytest.raises(
            PolicyConstraintProjectionConflict,
            match="policy_constraint_event_binding_evidence_mismatch",
        ):
            await projection.apply(
                session,
                event=mismatched_materialized,
            )
    tampered = _event(
        operation="adopt",
        revision=revision_v1,
        ordinal=99,
    )
    tampered.to_revision_digest = "0" * 64
    async with get_session_factory()() as session:
        with pytest.raises(
            PolicyConstraintProjectionConflict,
            match="policy_constraint_event_revision_evidence_mismatch",
        ):
            await projection.apply(session, event=tampered)

    async with get_session_factory()() as session:
        session.add(
            _binding(
                revision=revision_v2,
                binding_revision=2,
                state="active",
            )
        )
        await session.commit()
    async with get_session_factory()() as session:
        switched = await projection.apply(
            session,
            event=_event(
                operation="adopt",
                revision=revision_v2,
                ordinal=2,
            ),
        )
    expected_v2_ids = tuple(
        sorted(
            f"guideline-revision:{REVISION_2}:rule:{rule.rule_id}"
            for rule in rules_v2
        )
    )
    assert switched.node_ids == expected_v2_ids
    assert switched.activated_count == 2
    assert switched.ended_count == 2
    v1_rule_a = f"guideline-revision:{REVISION_1}:rule:rule-a"
    v1_rule_b = f"guideline-revision:{REVISION_1}:rule:rule-b"
    v2_rule_a = f"guideline-revision:{REVISION_2}:rule:rule-a"
    assert graph.scope.nodes["Constraint"][v1_rule_a][
        "superseded_by"
    ] == v2_rule_a
    assert graph.scope.nodes["Constraint"][v1_rule_a][
        "revocation_reason"
    ] == POLICY_CONSTRAINT_REVISION_SUPERSEDED_REASON
    assert is_visible_in_active_reads(
        graph.scope.nodes["Constraint"][v1_rule_a][
            "revocation_reason"
        ]
    )
    assert (
        "supersedes",
        "Constraint",
        "Constraint",
        v2_rule_a,
        v1_rule_a,
    ) in graph.scope.edges
    assert graph.scope.nodes["Constraint"][v1_rule_b][
        "superseded_by"
    ] is None
    assert graph.scope.nodes["Constraint"][v1_rule_b][
        "revocation_reason"
    ] == POLICY_CONSTRAINT_RULE_REMOVED_REASON
    assert not is_visible_in_active_reads(
        graph.scope.nodes["Constraint"][v1_rule_b][
            "revocation_reason"
        ]
    )
    assert all(
        graph.scope.nodes["Constraint"][node_id]["superseded_at"]
        is not None
        for node_id in expected_v1_ids
    )
    assert len(graph.scope.nodes["Constraint"]) == 4
    # A delayed, still-authentic old-revision event is acknowledged by
    # reconciling current relational authority; it must not roll the graph
    # backward or be rejected merely because a newer binding now exists.
    async with get_session_factory()() as session:
        delayed = await projection.apply(
            session,
            event=_event(
                operation="adopt",
                revision=revision_v1,
                ordinal=98,
            ),
        )
    assert delayed.node_ids == expected_v2_ids
    assert delayed.replayed is True

    async with get_session_factory()() as session:
        session.add(
            _binding(
                revision=revision_v2,
                binding_revision=3,
                state="unlinked",
            )
        )
        await session.commit()
    async with get_session_factory()() as session:
        unlinked = await projection.apply(
            session,
            event=_event(
                operation="unlink",
                revision=revision_v2,
                ordinal=3,
            ),
        )
    assert unlinked.active_count == 0
    assert unlinked.node_ids == ()
    assert unlinked.activated_count == 0
    assert unlinked.ended_count == 2
    assert len(graph.scope.nodes["Constraint"]) == 4
    assert all(
        graph.scope.nodes["Constraint"][node_id]["superseded_by"] is None
        and graph.scope.nodes["Constraint"][node_id]["revocation_reason"]
        == POLICY_CONSTRAINT_UNLINKED_REASON
        for node_id in expected_v2_ids
    )
    assert all(
        not is_visible_in_active_reads(
            graph.scope.nodes["Constraint"][node_id][
                "revocation_reason"
            ]
        )
        for node_id in expected_v2_ids
    )

    # Re-adoption followed by two full rebuilds proves stable identities,
    # exact counts and replay/noop behavior.
    unrelated_lineage = (
        "supersedes",
        "Constraint",
        "Constraint",
        "external-constraint",
        v1_rule_a,
    )
    graph.scope.edges[unrelated_lineage] = {
        "rule_id": "supersedes/unrelated-subsystem@v1"
    }
    async with get_session_factory()() as session:
        session.add(
            _binding(
                revision=revision_v1,
                binding_revision=4,
                state="active",
            )
        )
        await session.commit()
    async with get_session_factory()() as session:
        rebuilt = await projection.rebuild_board(
            session,
            board_id=BOARD_ID,
        )
        replay = await projection.rebuild_board(
            session,
            board_id=BOARD_ID,
        )
    assert rebuilt.node_ids == expected_v1_ids
    assert rebuilt.active_count == 2
    assert rebuilt.activated_count == 2
    assert rebuilt.ended_count == 0
    assert rebuilt.unadopted_active_count == 0
    assert replay.node_ids == expected_v1_ids
    assert replay.active_count == 2
    assert replay.activated_count == 0
    assert replay.ended_count == 0
    assert replay.replayed is True
    assert graph.requested_boards == [BOARD_ID] * 7
    assert all(
        graph.scope.nodes["Constraint"][node_id]["superseded_by"] is None
        and graph.scope.nodes["Constraint"][node_id]["revocation_reason"]
        is None
        for node_id in expected_v1_ids
    )
    assert all(
        is_visible_in_active_reads(
            graph.scope.nodes["Constraint"][node_id][
                "revocation_reason"
            ]
        )
        for node_id in expected_v1_ids
    )
    assert not any(
        key[0] == "supersedes"
        and key[4] in expected_v1_ids
        and attrs.get("rule_id") == POLICY_CONSTRAINT_LINEAGE_RULE
        for key, attrs in graph.scope.edges.items()
    )
    assert unrelated_lineage in graph.scope.edges

    # Retirement is terminal when no adopted successor Constraint exists.
    async with get_session_factory()() as session:
        session.add(
            GuidelineRetirementRow(
                retirement_id="retirement-b14",
                guideline_id=GUIDELINE_ID,
                status="superseded",
                retired_revision_id=REVISION_2,
                retired_revision_number=2,
                retired_semantic_version="2.0.0",
                retired_revision_digest=revision_v2.content_digest,
                retired_head_revision=2,
                reason="Policy no longer applies.",
                retired_by="agent-b14",
                retired_at=NOW + timedelta(minutes=40),
                superseded_by_guideline_id=UNADOPTED_GUIDELINE_ID,
                idempotency_key="retirement:b14",
                request_digest="9" * 64,
            )
        )
        await session.commit()
    async with get_session_factory()() as session:
        retired = await projection.apply(
            session,
            event=SimpleNamespace(
                operation="retire",
                board_id=BOARD_ID,
                guideline_id=GUIDELINE_ID,
                exact_revision_id=REVISION_2,
                retirement_status="superseded",
                revision_number=2,
                semantic_version="2.0.0",
                revision_digest=revision_v2.content_digest,
                event_id="event-b14-retire",
                occurred_at=NOW + timedelta(minutes=41),
            ),
        )
    assert retired.active_count == 0
    assert retired.ended_count == 2
    assert all(
        graph.scope.nodes["Constraint"][node_id]["superseded_by"] is None
        and graph.scope.nodes["Constraint"][node_id]["revocation_reason"]
        == POLICY_CONSTRAINT_GUIDELINE_SUPERSEDED_REASON
        for node_id in expected_v1_ids
    )
    assert all(
        not is_visible_in_active_reads(
            graph.scope.nodes["Constraint"][node_id][
                "revocation_reason"
            ]
        )
        for node_id in expected_v1_ids
    )
    assert graph.requested_boards == [BOARD_ID] * 8


@pytest.mark.asyncio
async def test_b14_delivery_retries_then_records_terminal_dlq(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b14-delivery.sqlite3")
    event = PolicyAdoptionChanged(
        event_id="event-b14-delivery",
        board_id=BOARD_ID,
        actor_id="agent-b14",
        actor_type="agent",
        occurred_at=NOW,
        event_schema_version="guideline-impact/v1",
        operation="adopt",
        guideline_id=GUIDELINE_ID,
        binding_id="binding-b14",
        previous_binding_revision=None,
        binding_revision=1,
        from_revision_id=None,
        from_semantic_version=None,
        from_revision_digest=None,
        to_revision_id=REVISION_1,
        to_semantic_version="1.0.0",
        to_revision_digest="a" * 64,
        impact_receipt_id="receipt-b14",
        impact_digest="b" * 64,
        binding_digest_before="c" * 64,
        binding_head_digest_before="d" * 64,
        binding_head_digest_after="e" * 64,
        policy_set_digest_before="f" * 64,
        policy_set_digest_after="1" * 64,
        policy_set_digest="1" * 64,
        added_rule_ids=("rule-a",),
    )
    async with get_session_factory()() as session:
        session.add(Board(id=BOARD_ID, name="B14", owner_id="owner-b14"))
        session.add(
            DomainEventRow(
                id=event.event_id,
                event_type=event.event_type,
                board_id=event.board_id,
                actor_id=event.actor_id,
                actor_type=event.actor_type,
                payload_json=event.payload_for_storage(),
                occurred_at=event.occurred_at,
            )
        )
        await session.flush()
        session.add(
            DomainEventHandlerExecution(
                id="execution-b14-delivery",
                event_id=event.event_id,
                handler_name="PolicyConstraintProjectionHandler",
                status="pending",
                attempts=0,
            )
        )
        await session.commit()

    current_time = NOW

    def clock() -> datetime:
        return current_time

    processor = DomainEventDeliveryProcessor(
        CommunitySqlAlchemyDomainEventDeliveryStore(get_session_factory()),
        handler_resolver=lambda _name, _event: (
            _AlwaysFailingPolicyConstraintHandler
        ),
        clock=clock,
    )
    for attempt in range(1, 6):
        assert await processor.process_batch() == 1
        async with get_session_factory()() as session:
            execution = await session.get(
                DomainEventHandlerExecution,
                "execution-b14-delivery",
            )
            assert execution is not None
            assert execution.attempts == attempt
            assert (
                execution.last_error
                == "policy constraint graph unavailable"
            )
            if attempt < 5:
                assert execution.status == "pending"
                assert execution.processed_at is None
                assert execution.next_attempt_at is not None
                current_time = execution.next_attempt_at.replace(
                    tzinfo=timezone.utc
                ) + timedelta(microseconds=1)
            else:
                assert execution.status == "dlq"
                assert execution.next_attempt_at is None
                assert execution.processed_at is not None
                assert (
                    execution.processed_at.replace(tzinfo=timezone.utc)
                    == current_time
                )
