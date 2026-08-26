"""Atomic-failure contract for Grafx ``replace_node_payload``.

These tests exercise a real Grafx transaction.  In particular, they prove
that an application-level caller cannot catch an uncertain replacement and
then accidentally publish its staged residue by committing the scope.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import okto_grafx
import pytest
from okto_grafx import Transaction
from okto_pulse.core.kg.interfaces.graph_errors import GraphError

BOARD_ID = "grafx-replace-payload-atomic-failure"


class _InjectedProcessSignal(BaseException):
    """Non-``Exception`` control-flow signal raised after Grafx applies SET."""


class _InjectedCleanupFailure(RuntimeError):
    """Deterministic refusal injected into the raw transaction rollback."""


@pytest.fixture
def grafx_database(tmp_path: Path) -> Any:
    database = okto_grafx.connect(tmp_path / "grafx-replace-payload")
    with database.begin("write") as schema:
        schema.execute(
            "CREATE VECTOR SPACE replace_payload_test {dimension: 4, metric: 'cosine'}"
        )
        schema.execute(
            "CREATE NODE TABLE Entity("
            "id STRING, source_session_id STRING, title STRING, "
            "content STRING, score DOUBLE, "
            "embedding VECTOR(replace_payload_test), PRIMARY KEY(id))"
        )
        schema.execute(
            "CREATE REL TABLE supports("
            "FROM Entity TO Entity, confidence DOUBLE, "
            "created_by_session_id STRING, rule_id STRING)"
        )
        schema.execute(
            "CREATE REL TABLE hidden_supports(FROM Entity TO Entity, note STRING)"
        )
    try:
        yield database
    finally:
        database.close()


def _provider(database: Any) -> Any:
    from okto_pulse.community.adapters.grafx_graph_transaction import (
        CommunityGrafxGraphTransaction,
    )

    def resolve_database(board_id: str) -> Any:
        if board_id != BOARD_ID:
            raise KeyError(board_id)
        return database

    return CommunityGrafxGraphTransaction(
        database_resolver=resolve_database,
        revalidate_fence=lambda _board_id, _phase: None,
        node_types=("Entity",),
        relationship_pairs=(("supports", "Entity", "Entity"),),
    )


def _normalize(value: Any) -> Any:
    if type(value).__name__ == "VectorValue":
        return tuple(value.values)
    return value


def _node(database: Any) -> tuple[Any, ...]:
    rows = database.execute(
        "MATCH (n:Entity {id: $node_id}) "
        "RETURN n.id, n.source_session_id, n.title, n.content, "
        "n.score, n.embedding",
        {"node_id": "target"},
    ).rows
    assert len(rows) == 1
    return tuple(_normalize(value) for value in rows[0])


def _incident_edges(database: Any) -> Counter[tuple[Any, ...]]:
    edges: Counter[tuple[Any, ...]] = Counter()
    for row in database.execute(
        "MATCH (a:Entity)-[r:supports]->(b:Entity) "
        "WHERE a.id = $node_id OR b.id = $node_id "
        "RETURN a.id, b.id, r.confidence, "
        "r.created_by_session_id, r.rule_id",
        {"node_id": "target"},
    ).rows:
        edges[("supports", *(_normalize(value) for value in row))] += 1
    for row in database.execute(
        "MATCH (a:Entity)-[r:hidden_supports]->(b:Entity) "
        "WHERE a.id = $node_id OR b.id = $node_id "
        "RETURN a.id, b.id, r.note",
        {"node_id": "target"},
    ).rows:
        edges[("hidden_supports", *(_normalize(value) for value in row))] += 1
    return edges


def _state(database: Any) -> tuple[tuple[Any, ...], Counter[tuple[Any, ...]]]:
    return _node(database), _incident_edges(database)


async def _seed_graph(provider: Any) -> None:
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "target",
            {
                "title": "before",
                "content": "private payload",
                "score": 1.25,
                "embedding": [0.0, 1.0, 0.0, 0.0],
            },
            source_session_id="original-session",
        )
        for node_id in ("peer", "incoming"):
            scope.create_node(
                "Entity",
                node_id,
                {"title": node_id},
                source_session_id="control-session",
            )
        for from_id, to_id, confidence, rule_id in (
            ("target", "peer", 0.7, "rule/outgoing/1"),
            ("target", "peer", 0.9, "rule/outgoing/2"),
            ("incoming", "target", 0.8, "rule/incoming"),
            ("target", "target", 1.0, "rule/self-loop"),
        ):
            assert scope.create_edge(
                "supports",
                "Entity",
                "Entity",
                from_id,
                to_id,
                {
                    "confidence": confidence,
                    "created_by_session_id": "edge-session",
                    "rule_id": rule_id,
                },
            )
        assert scope.create_edge(
            "hidden_supports",
            "Entity",
            "Entity",
            "target",
            "peer",
            {"note": "catalog-only incident edge"},
        )


def _is_replacement_set(statement: str) -> bool:
    return statement.startswith(
        "MATCH (n:Entity) WHERE n.id = $node_id SET "
    ) and statement.endswith(" RETURN n.id")


def _replacement_attrs() -> dict[str, Any]:
    return {
        "title": "after",
        "content": "replacement payload",
        "score": 9.5,
        "embedding": None,
    }


@pytest.mark.asyncio
async def test_apply_then_baseexception_poisons_scope_before_caller_commit(
    grafx_database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database)
    await _seed_graph(provider)
    before = _state(grafx_database)
    scope = await provider.begin(BOARD_ID)
    original_execute = Transaction.execute
    primary_signal = _InjectedProcessSignal()
    applied_sets = 0

    def apply_then_signal(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal applied_sets
        result = original_execute(transaction, statement, params)
        if _is_replacement_set(statement):
            applied_sets += 1
            raise primary_signal
        return result

    monkeypatch.setattr(Transaction, "execute", apply_then_signal)
    with pytest.raises(_InjectedProcessSignal) as escaped:
        scope.replace_node_payload(
            "Entity",
            "target",
            _replacement_attrs(),
            source_session_id="replacement-session",
        )

    assert escaped.value is primary_signal
    assert applied_sets == 1
    await scope.commit()
    assert _state(grafx_database) == before
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage_kind", ("payload", "incident_edge"))
async def test_confirmation_mismatch_poison_prevents_later_commit(
    grafx_database: Any,
    monkeypatch: pytest.MonkeyPatch,
    damage_kind: str,
) -> None:
    provider = _provider(grafx_database)
    await _seed_graph(provider)
    before = _state(grafx_database)
    scope = await provider.begin(BOARD_ID)
    original_execute = Transaction.execute
    applied_damage = 0

    def damage_after_set(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal applied_damage
        result = original_execute(transaction, statement, params)
        if not _is_replacement_set(statement):
            return result
        applied_damage += 1
        if damage_kind == "payload":
            original_execute(
                transaction,
                "MATCH (n:Entity) WHERE n.id = $node_id SET n.title = $title",
                {"node_id": "target", "title": "divergent payload"},
            )
        else:
            original_execute(
                transaction,
                "MATCH (a:Entity)-[r:supports]->(b:Entity) "
                "WHERE a.id = $source_id AND b.id = $target_id "
                "AND r.rule_id = $rule_id DELETE r",
                {
                    "source_id": "target",
                    "target_id": "peer",
                    "rule_id": "rule/outgoing/1",
                },
            )
        return result

    monkeypatch.setattr(Transaction, "execute", damage_after_set)
    with pytest.raises(GraphError) as refused:
        scope.replace_node_payload(
            "Entity",
            "target",
            _replacement_attrs(),
            source_session_id="replacement-session",
        )

    assert applied_damage == 1
    assert refused.value.details["payload_confirmed"] is (damage_kind != "payload")
    assert refused.value.details["edges_confirmed"] is (damage_kind != "incident_edge")
    await scope.commit()
    assert _state(grafx_database) == before
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_cleanup_failure_preserves_primary_signal_identity_and_diagnostic(
    grafx_database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database)
    await _seed_graph(provider)
    before = _state(grafx_database)
    scope = await provider.begin(BOARD_ID)
    raw_transaction = scope._transaction
    original_execute = Transaction.execute
    original_rollback = Transaction.rollback
    primary_signal = _InjectedProcessSignal()
    cleanup_failure = _InjectedCleanupFailure("injected rollback refusal")

    def apply_then_signal(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        result = original_execute(transaction, statement, params)
        if _is_replacement_set(statement):
            raise primary_signal
        return result

    def refuse_cleanup(transaction: Any) -> None:
        if transaction is raw_transaction:
            raise cleanup_failure
        original_rollback(transaction)

    monkeypatch.setattr(Transaction, "execute", apply_then_signal)
    monkeypatch.setattr(Transaction, "rollback", refuse_cleanup)
    try:
        with pytest.raises(_InjectedProcessSignal) as escaped:
            scope.replace_node_payload(
                "Entity",
                "target",
                _replacement_attrs(),
                source_session_id="replacement-session",
            )
        assert escaped.value is primary_signal
        assert escaped.value.__cause__ is cleanup_failure
        notes = tuple(getattr(escaped.value, "__notes__", ()))
        assert any("rollback also failed" in note for note in notes)
    finally:
        monkeypatch.setattr(Transaction, "rollback", original_rollback)
        if raw_transaction.active:
            original_rollback(raw_transaction)

    assert _state(grafx_database) == before
    assert grafx_database.verify("all").findings == ()
