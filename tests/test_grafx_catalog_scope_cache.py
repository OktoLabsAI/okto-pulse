"""Frente C-community: one public catalog snapshot per Grafx transaction scope.

``Database.catalog`` builds a complete, linearized snapshot on every access and its view
answers ``table()``/``space()`` by linear scan.  The scope now captures that public view
once, indexes it by name, memoizes resolved relationship layouts, and drops everything the
moment the scope itself emits a statement that can change the catalog.  These tests count
the public accesses and prove the invalidation; the existing transaction, projection and
lineage suites pin that statement text, order and semantics did not move.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from okto_grafx import Database
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from test_grafx_graph_transaction import (
    BOARD_ID,
    NODE_TYPES,
    RELATIONSHIP_PAIRS,
    _DeterministicFence,
    _provider,
)
from test_grafx_graph_transaction import fence as _fence_fixture
from test_grafx_graph_transaction import grafx_database as _grafx_database_fixture

from okto_pulse.community.adapters.grafx_graph_transaction import (
    CommunityGrafxGraphTransaction,
    _GrafxTransactionScope,
)

# pytest registers a fixture under the attribute name that holds it, so the contract
# module's fixtures are re-exposed here under their own names through thin wrappers; the
# aliases keep the parameter names below from shadowing an import.
contract_fence = _fence_fixture
contract_grafx_database = _grafx_database_fixture


@pytest.fixture(name="fence")
def _fence(contract_fence: _DeterministicFence) -> _DeterministicFence:
    return contract_fence


@pytest.fixture(name="grafx_database")
def _grafx_database(contract_grafx_database: Any) -> Any:
    return contract_grafx_database


EXTRA_TABLE_DDL = (
    "CREATE NODE TABLE Extra(id STRING, source_session_id STRING, title STRING, "
    "PRIMARY KEY(id))"
)


@pytest.fixture
def catalog_reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Count every access to the PUBLIC ``Database.catalog`` property."""

    original = Database.catalog
    reads: list[str] = []

    def counting(self: Any) -> Any:
        reads.append("catalog")
        return original.fget(self)

    monkeypatch.setattr(Database, "catalog", property(counting))
    yield reads


def _seed_two_entities(scope: Any) -> None:
    scope.create_node("Entity", "cache-a", {"title": "a"}, source_session_id="s-cache")
    scope.create_node("Entity", "cache-b", {"title": "b"}, source_session_id="s-cache")


@pytest.mark.asyncio
async def test_one_scope_reads_the_public_catalog_once_for_many_resolutions(
    grafx_database: Any,
    fence: _DeterministicFence,
    catalog_reads: list[str],
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        _seed_two_entities(scope)
        assert scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "cache-a",
            "cache-b",
            {"confidence": 0.5, "created_by_session_id": "s-cache", "rule_id": "r/1"},
        )
        scope.delete_edges_by_session("s-none")
        scope.delete_nodes_by_session("s-none", ("Entity", "Decision"))
    # Two node types, one relationship pair and the session deletes resolved at least six
    # definitions; before this change every resolution rebuilt the snapshot.
    assert catalog_reads == ["catalog"]


@pytest.mark.asyncio
async def test_a_ddl_statement_inside_the_scope_invalidates_the_cached_view(
    grafx_database: Any,
    fence: _DeterministicFence,
    catalog_reads: list[str],
) -> None:
    provider = _provider(grafx_database, fence)
    scope = await provider.begin(BOARD_ID)
    try:
        _seed_two_entities(scope)  # captures the snapshot
        assert len(catalog_reads) == 1
        scope.execute(EXTRA_TABLE_DDL)
        # The public ``Database.catalog`` is the COMMITTED catalog, so the table staged by
        # this scope is not visible to the adapter yet -- exactly as before this change.
        # What the change owes is that the resolution after a DDL goes back to the public
        # view instead of answering from the snapshot taken before the DDL: mutant "cache
        # without invalidation" answers from memory and never reads the catalog again.
        with pytest.raises((GraphCapabilityUnavailable, GraphError)):
            scope.create_node(
                "Extra", "extra-1", {"title": "t"}, source_session_id="s-extra"
            )
        assert len(catalog_reads) == 2
        # And a plain resolution afterwards reuses the fresh snapshot, not a third read.
        scope.create_node(
            "Entity", "cache-c", {"title": "c"}, source_session_id="s-cache"
        )
        assert len(catalog_reads) == 2
    finally:
        await scope.rollback()


@pytest.mark.asyncio
async def test_a_row_level_write_statement_keeps_the_cached_view(
    grafx_database: Any,
    fence: _DeterministicFence,
    catalog_reads: list[str],
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        _seed_two_entities(scope)
        scope.execute(
            "MATCH (n:Entity) WHERE n.id = $id SET n.title = $title RETURN n.id",
            {"id": "cache-a", "title": "renamed"},
        )
        scope.create_node(
            "Entity", "cache-c", {"title": "c"}, source_session_id="s-cache"
        )
    assert catalog_reads == ["catalog"]


@pytest.mark.asyncio
async def test_a_new_scope_sees_a_table_created_by_an_earlier_scope(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        _seed_two_entities(scope)  # this scope's snapshot predates the table
    async with await provider.begin(BOARD_ID) as scope:
        scope.execute(EXTRA_TABLE_DDL)
    # Mutant "cache shared across scopes" would still hold the first snapshot here.
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Extra", "extra-2", {"title": "t"}, source_session_id="s-extra"
        )


@pytest.mark.asyncio
async def test_the_cached_view_still_sees_tables_outside_the_configured_pairs(
    grafx_database: Any,
    fence: _DeterministicFence,
    catalog_reads: list[str],
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        _seed_two_entities(scope)
        incident = scope._incident_relationship_definitions("Entity")
        physical = {entry[0] for entry in incident}
    assert (
        "hidden_supports" in physical
    )  # present in the catalog, absent from the pairs
    assert "supports" in physical
    assert catalog_reads == ["catalog"]


@pytest.mark.asyncio
async def test_an_unknown_table_is_refused_the_same_way_from_the_snapshot(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        _seed_two_entities(scope)
        with pytest.raises((GraphCapabilityUnavailable, GraphError)):
            scope.create_node("Nowhere", "x", {"title": "t"}, source_session_id="s")
        await scope.rollback()


@pytest.mark.asyncio
async def test_relationship_layouts_are_resolved_once_per_scope(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def counting_resolver(edge_type: str, from_type: str, to_type: str) -> str:
        # The contract schema stores the pair under the logical name itself.
        calls.append((edge_type, from_type, to_type))
        return edge_type

    def resolve_database(board_id: str) -> Any:
        if board_id != BOARD_ID:
            raise KeyError(board_id)
        return grafx_database

    provider = CommunityGrafxGraphTransaction(
        database_resolver=resolve_database,
        revalidate_fence=fence,
        node_types=NODE_TYPES,
        relationship_pairs=RELATIONSHIP_PAIRS,
        relationship_table_resolver=counting_resolver,
    )
    async with await provider.begin(BOARD_ID) as scope:
        _seed_two_entities(scope)
        for index in range(3):
            assert scope.create_edge(
                "supports",
                "Entity",
                "Entity",
                "cache-a",
                "cache-b",
                {
                    "confidence": 0.5,
                    "created_by_session_id": f"s-{index}",
                    "rule_id": "r",
                },
            )
        scope.delete_edges_by_session("s-none")
    # Four operations over the one configured pair: one resolution, the rest memoized.
    assert calls == [("supports", "Entity", "Entity")]


def test_catalog_changing_statements_are_recognised_fail_safe() -> None:
    changes = _GrafxTransactionScope._statement_changes_catalog
    assert changes(EXTRA_TABLE_DDL)
    assert changes("CREATE REL TABLE x(FROM Entity TO Entity)")
    assert changes("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
    assert changes("ALTER TABLE Entity ADD note STRING")
    assert changes("DROP TABLE Extra")
    assert changes("CALL CREATE_VECTOR_INDEX('Entity', 'embedding')")
    assert changes("MATCH (n:Entity) RETURN n.id; DROP TABLE Extra")
    assert changes("SOMETHING NEW")
    assert changes("EXPLAIN DROP TABLE Extra")
    # Every CREATE that is not the row form is catalog-changing, known today or not.
    assert changes("CREATE INDEX idx ON Entity(id)")
    assert changes("CREATE VECTOR INDEX idx ON Entity(embedding)")
    assert changes("CREATE SEQUENCE seq")
    assert changes("CREATE FOO bar")
    assert changes("MATCH (n:Entity) CREATE INDEX idx ON Entity(id)")
    assert changes("MERGE INTO something")
    # Row-level statements leave the catalog alone even when they look like DDL inside text.
    assert not changes("MATCH (n:Entity) WHERE n.id = $id SET n.title = $title")
    assert not changes(
        "MATCH (a:Entity), (b:Entity) WHERE a.id = $a AND b.id = $b "
        "CREATE (a)-[:supports {rule_id: 'CREATE INDEX'}]->(b)"
    )
    assert not changes("CREATE(n:Entity {id: $id})")
    assert not changes("CREATE (n:Entity {id: $id, title: 'CREATE NODE TABLE'})")
    assert not changes("MATCH (a:Entity)-[r:supports]->(b:Entity) DELETE r")
    assert not changes("MERGE (n:Entity {id: $id})")


@pytest.fixture
def broken_public_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every access to the PUBLIC ``Database.catalog`` property fail."""

    def exploding(self: Any) -> Any:
        raise RuntimeError("catalog property failed")

    monkeypatch.setattr(Database, "catalog", property(exploding))


@pytest.mark.asyncio
async def test_a_public_catalog_failure_keeps_each_callers_operation(
    grafx_database: Any,
    fence: _DeterministicFence,
    broken_public_catalog: None,
) -> None:
    """The snapshot must not rename the failure.

    Before the snapshot every resolver mapped a failing ``Database.catalog`` under its
    own operation and ``_catalog_space`` let it through unmapped; mutant "map inside
    ``_catalog()``" reports ``catalog_snapshot`` for all of them.
    """

    provider = _provider(grafx_database, fence)
    scope = await provider.begin(BOARD_ID)
    try:
        with pytest.raises(GraphError) as node_failure:
            scope.create_node("Entity", "x", {"title": "t"}, source_session_id="s")
        assert node_failure.value.details["operation"] == "node_schema"
        assert node_failure.value.details["backend_error_type"] == "RuntimeError"
        with pytest.raises(GraphError) as relationship_failure:
            scope._relationship_definition("supports", "Entity", "Entity")
        assert relationship_failure.value.details["operation"] == "relationship_schema"
        with pytest.raises(GraphError) as incident_failure:
            scope._incident_relationship_definitions("Entity")
        assert incident_failure.value.details["operation"] == "snapshot_incident_schema"
        with pytest.raises(RuntimeError, match="catalog property failed"):
            scope._catalog_space("anything")
    finally:
        await scope.rollback()
