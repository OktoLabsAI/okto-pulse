"""Directed Sprint projection removal on a real Grafx Board generation."""

from dataclasses import replace

import pytest

from okto_pulse.core.ports.retirement_graph import graph_retirement_fingerprint
from okto_pulse.community.adapters import grafx_sprint_retirement as retirement
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from logical_transfer_matrix_support import (
    Corpus, complete_node, complete_relation, open_generation_database, schema_for, seed_generation,
)


def corpus():
    schema = schema_for("board")
    def node(kind, key, source):
        # The surviving Spec carries all property types, including vectors,
        # timestamps and scores. Its whole logical payload must stay identical.
        original = complete_node(schema, kind, key, 1, null_nullable=(key != "spec"))
        return replace(original, properties={**original.properties, "source_artifact_ref": source,
            "created_by_agent": "system:historical_consolidation", "source_session_id": "shared-card-session"})
    root = node("Entity", "same-id", "sprint:origin")
    outcome = node("Criterion", "same-id", "sprint:origin")
    card, spec = node("Entity", "card", "card:card"), node("Entity", "spec", "spec:spec")
    def edge(source, target, rule):
        layout = next(layout for layout in schema.relation_layouts if layout.identity == (
            "belongs_to", source.type_name, target.type_name))
        original = complete_relation(schema, layout, source.key, target.key, 1)
        return replace(original, properties={**original.properties, "layer": "deterministic", "rule_id": rule + "@1.0"})
    parent = edge(root, spec, "belongs_to/sprint_to_spec")
    kept = edge(card, spec, "belongs_to/card_to_spec")
    return Corpus(schema, (root, outcome, card, spec), (parent, parent,
        edge(outcome, root, "belongs_to/sprint_outcome"), edge(card, root, "belongs_to/card_to_sprint"), kept, kept))


@pytest.fixture
def graph(tmp_path):
    original = corpus()
    path = tmp_path / "board.grafx"
    seed_generation("grafx", path, original)
    database = open_generation_database("grafx", path, "board", read_only=False)
    try:
        yield database, path, original
    finally:
        database.close()


def fingerprint(database):
    snapshot = make_grafx_logical_source(database, scope="board").open_snapshot()
    try:
        return graph_retirement_fingerprint(snapshot)
    finally:
        snapshot.close()


def plan(database, origins=frozenset({"origin"})):
    return retirement.prepare_sprint_graph_retirement(database, board_id="board", archived_origin_ids=origins)


def test_predecessor_removal_keeps_the_same_closed_selection_and_exact_survivors(tmp_path):
    from okto_pulse.core.kg.logical_transfer import schema_digest, transfer_logical_graph
    from okto_pulse.community.adapters.grafx_recovery_contracts import (
        predecessor_recovery_contract, make_grafx_recovery_logical_sink, make_grafx_recovery_logical_source,
    )
    from logical_transfer_matrix_support import MaterializedSource

    current = corpus()
    schema = predecessor_recovery_contract().schema
    original = Corpus(schema, tuple(replace(node, properties={name: value for name, value in node.properties.items()
        if name in schema.node_type(node.type_name).property_names()}) for node in current.nodes), current.relations)
    path = tmp_path / 'predecessor'
    transfer_logical_graph(MaterializedSource(original), make_grafx_recovery_logical_sink(path,
        scope='board', expected_schema_digest=schema_digest(schema)))
    with open_generation_database('grafx', path, 'board', read_only=False) as database:
        selected = plan(database)
        assert selected.node_keys == (('Criterion', 'same-id'), ('Entity', 'same-id'))
        assert selected.before_sha256 == original.fingerprint
        identity = database.identity.database_uuid
        expected = Corpus(schema, original.nodes[2:], original.relations[-2:])
        receipt = retirement.apply_sprint_graph_retirement(database, selected)
        assert retirement.apply_sprint_graph_retirement(database, selected) == receipt
        assert database.identity.database_uuid == identity
        snapshot = make_grafx_recovery_logical_source(database, scope='board').open_snapshot()
        try:
            assert graph_retirement_fingerprint(snapshot) == expected.fingerprint == selected.after_sha256
        finally:
            snapshot.close()


def test_directed_removal_preserves_neighbors_parallel_edges_and_physical_generation(graph):
    database, path, original = graph
    identity = database.identity.database_uuid
    before_lsn = database.transactions.published_lsn()
    selected = plan(database)
    assert selected.before_sha256 == original.fingerprint
    assert selected.node_keys == (("Criterion", "same-id"), ("Entity", "same-id"))
    assert selected.removed_relations == 4
    expected = Corpus(original.schema, original.nodes[2:], original.relations[4:])
    assert selected.after_sha256 == expected.fingerprint
    assert database.transactions.published_lsn() == before_lsn
    receipt = retirement.apply_sprint_graph_retirement(database, selected)
    assert receipt.plan == selected
    assert fingerprint(database) == expected.fingerprint
    assert database.identity.database_uuid == identity
    committed_lsn = database.transactions.published_lsn()
    assert retirement.apply_sprint_graph_retirement(database, selected) == receipt
    assert database.transactions.published_lsn() == committed_lsn
    # A separate native handle verifies the persisted generation, not a Python
    # result cache or a reconstruction of the candidate graph.
    database.close()
    reopened = open_generation_database("grafx", path, "board", read_only=False)
    try:
        assert fingerprint(reopened) == expected.fingerprint
        assert retirement.apply_sprint_graph_retirement(reopened, selected) == receipt
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["exception", "survivor_changed"])
def test_failure_after_first_delete_rolls_back_every_node_and_relation(graph, monkeypatch, failure):
    database, _, original = graph
    selected = plan(database)
    remove = retirement._remove
    def fail(transaction, key):
        remove(transaction, key)
        if failure == "exception":
            raise RuntimeError("injected after delete")
        transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.title=$title", {"id": "spec", "title": "unplanned"})
    monkeypatch.setattr(retirement, "_remove", fail)
    with pytest.raises((RuntimeError, ValueError), match="(injected|after_mismatch)"):
        retirement.apply_sprint_graph_retirement(database, selected)
    assert fingerprint(database) == original.fingerprint


@pytest.mark.parametrize("damage", ["source", "receipt"])
def test_changed_source_or_incorrect_selection_refuses_before_removal(graph, damage):
    database, _, _ = graph
    selected = plan(database)
    if damage == "source":
        with database.begin("write") as transaction:
            transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.title=$title", {"id": "spec", "title": "later edit"})
    else:
        selected = replace(selected, removed_relations=selected.removed_relations + 1)
    before = fingerprint(database)
    with pytest.raises(ValueError, match="(before_mismatch|selection_mismatch)"):
        retirement.apply_sprint_graph_retirement(database, selected)
    assert fingerprint(database) == before


@pytest.mark.parametrize("change", ["unknown_origin", "child_ref", "cognitive_writer", "unknown_edge"])
def test_unclassified_sources_or_incident_semantics_require_investigation(graph, change):
    database, _, _ = graph
    origins = frozenset() if change == "unknown_origin" else frozenset({"origin"})
    if change != "unknown_origin":
        with database.begin("write") as transaction:
            if change == "child_ref":
                transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.source_artifact_ref=$value",
                    {"id": "same-id", "value": "sprint:origin:alternative:abc"})
            elif change == "cognitive_writer":
                transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.created_by_agent=$value",
                    {"id": "same-id", "value": "agent:cognitive"})
            else:
                # Physical name is owned by the adapter's closed layout.
                from okto_pulse.community.adapters.logical_transfer_factories import logical_transfer_scope
                table = logical_transfer_scope("board").relationship_tables[("belongs_to", "Entity", "Entity")]
                transaction.execute(f"MATCH (a:Entity)-[r:{table}]->(b:Entity) WHERE a.id=$id SET r.rule_id=$rule",
                    {"id": "same-id", "rule": "unknown_semantics"})
    before = fingerprint(database)
    with pytest.raises(ValueError, match="requires_disposition"):
        plan(database, origins)
    assert fingerprint(database) == before


def test_session_and_content_mentions_do_not_select_an_unrelated_source(graph):
    database, _, _ = graph
    with database.begin("write") as transaction:
        transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.content=$value",
            {"id": "spec", "value": "sprint:origin is mentioned in retained history"})
    selected = plan(database)
    retirement.apply_sprint_graph_retirement(database, selected)
    assert fingerprint(database) == selected.after_sha256
    assert selected.node_keys == (("Criterion", "same-id"), ("Entity", "same-id"))


@pytest.mark.parametrize("limit", ["_MAX_RECORDS", "_MAX_BYTES"])
def test_write_snapshot_budget_refuses_before_any_removal(graph, monkeypatch, limit):
    database, _, original = graph
    selected = plan(database)
    monkeypatch.setattr(retirement, limit, 1)
    with pytest.raises(ValueError, match="record_limit"):
        retirement.apply_sprint_graph_retirement(database, selected)
    assert fingerprint(database) == original.fingerprint
