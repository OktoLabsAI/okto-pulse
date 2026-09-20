"""Global retirement against real Board and Global Discovery generations."""

from dataclasses import replace

import pytest

from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL
from okto_pulse.core.ports.retirement_graph import graph_retirement_fingerprint
from okto_pulse.community.adapters import grafx_global_retirement as global_retirement
from okto_pulse.community.adapters import grafx_sprint_retirement as board_retirement
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from logical_transfer_matrix_support import (
    Corpus, complete_node, complete_relation, open_generation_database, schema_for, seed_generation,
)
from test_grafx_sprint_retirement import corpus as board_corpus


def corpora():
    original = board_corpus()
    aliases = {("Entity", "same-id"): "root", ("Criterion", "same-id"): "outcome"}
    vector = original.nodes[-1].properties["embedding"]
    nodes = tuple(replace(node, key=aliases.get((node.type_name, node.key), node.key), properties={**node.properties,
        "id": aliases.get((node.type_name, node.key), node.key),
        "embedding": replace(vector, space_name=original.schema.node_type(node.type_name).property_def("embedding").vector_space),
        "revocation_reason": LOGICAL_NULL, "superseded_by": LOGICAL_NULL}) for node in original.nodes)
    relations = tuple(replace(edge, source_key=aliases.get((edge.source_type, edge.source_key), edge.source_key),
        target_key=aliases.get((edge.target_type, edge.target_key), edge.target_key)) for edge in original.relations)
    board = Corpus(original.schema, nodes, relations)
    schema = schema_for("global_discovery")
    def node(kind, key, **values):
        original = complete_node(schema, kind, key, 7)
        return replace(original, properties={**original.properties, **values})
    nodes = (
        node("Board", "board-a", decision_count=4), node("Board", "board-b", decision_count=17),
        # Deliberately wrong cached type: source identity is proved in Board.
        node("DecisionDigest", "a-root", board_id="board-a", original_node_id="root", node_type="Decision"),
        node("DecisionDigest", "a-root-copy", board_id="board-a", original_node_id="root", node_type="Entity"),
        node("DecisionDigest", "a-outcome", board_id="board-a", original_node_id="outcome", node_type="Criterion"),
        node("DecisionDigest", "a-spec", board_id="board-a", original_node_id="spec", node_type="Entity"),
        node("DecisionDigest", "b-root", board_id="board-b", original_node_id="root", node_type="Entity"),
        node("Entity", "shared"), node("Topic", "topic"),
    )
    by_id = {node.key: node for node in nodes}
    def edge(kind, source, target):
        a, b = by_id[source], by_id[target]
        layout = next(item for item in schema.relation_layouts if item.identity == (kind, a.type_name, b.type_name))
        return complete_relation(schema, layout, source, target, 11)
    relations = (
        edge("CONTAINS_DECISION", "board-a", "a-root"), edge("CONTAINS_DECISION", "board-a", "a-root"),
        edge("CONTAINS_DECISION", "board-a", "a-root-copy"), edge("CONTAINS_DECISION", "board-a", "a-outcome"),
        edge("CONTAINS_DECISION", "board-a", "a-spec"), edge("CONTAINS_DECISION", "board-b", "b-root"),
        edge("DECISION_MENTIONS_ENTITY", "a-root", "shared"), edge("DECISION_MENTIONS_ENTITY", "a-spec", "shared"),
        edge("DECISION_DERIVES_FROM", "a-root", "b-root"), edge("HAS_TOPIC", "board-a", "topic"),
        edge("HAS_TOPIC", "board-b", "topic"), edge("ENTITY_RELATES_TO", "shared", "shared"),
    )
    return board, Corpus(schema, nodes, relations)


@pytest.fixture
def graphs(tmp_path):
    board_corpus, global_corpus = corpora()
    paths = tmp_path / "board.grafx", tmp_path / "global.grafx"
    for path, corpus in zip(paths, (board_corpus, global_corpus), strict=True):
        seed_generation("grafx", path, corpus)
    board = open_generation_database("grafx", paths[0], "board", read_only=False)
    global_db = open_generation_database("grafx", paths[1], "global_discovery", read_only=False)
    try:
        yield board, global_db, paths, board_corpus, global_corpus
    finally:
        global_db.close()
        board.close()


def fingerprint(database, scope="global_discovery"):
    snapshot = make_grafx_logical_source(database, scope=scope).open_snapshot()
    try:
        return graph_retirement_fingerprint(snapshot, scope=scope)
    finally:
        snapshot.close()


def prepare(graphs):
    board, global_db, *_ = graphs
    board_plan = board_retirement.prepare_sprint_graph_retirement(board, board_id="board-a", archived_origin_ids=frozenset({"origin"}))
    global_plan = global_retirement.prepare_global_graph_retirement(global_db, ((board, board_plan),))
    return board_plan, global_plan


def apply(graphs, plan):
    return global_retirement.apply_global_graph_retirement(graphs[1], plan, board_databases={"board-a": graphs[0]})


def test_global_removal_requires_absent_sources_and_preserves_other_board_and_shared_nodes(graphs):
    board, global_db, paths, _, original = graphs
    board_plan, global_plan = prepare(graphs)
    assert global_plan.digest_ids == ("a-outcome", "a-root", "a-root-copy")
    assert global_plan.board_counts == (("board-a", 4, 2),)
    assert global_plan.removed_relations == 6
    with pytest.raises(ValueError, match="board_not_retired"):
        apply(graphs, global_plan)
    assert fingerprint(global_db) == original.fingerprint
    board_retirement.apply_sprint_graph_retirement(board, board_plan)
    retired = set(global_plan.digest_ids)
    expected = Corpus(original.schema, tuple(replace(node, properties={**node.properties, "decision_count": 2})
        if node.type_name == "Board" and node.key == "board-a" else node for node in original.nodes if node.key not in retired),
        tuple(edge for edge in original.relations if edge.source_key not in retired and edge.target_key not in retired))
    uuid = global_db.identity.database_uuid
    result = apply(graphs, global_plan)
    assert fingerprint(global_db) == expected.fingerprint == global_plan.after_sha256
    assert global_db.identity.database_uuid == uuid
    lsn = global_db.transactions.published_lsn()
    assert apply(graphs, global_plan) == result
    assert global_db.transactions.published_lsn() == lsn
    global_db.close()
    reopened = open_generation_database("grafx", paths[1], "global_discovery", read_only=False)
    try:
        assert fingerprint(reopened) == expected.fingerprint
        assert global_retirement.apply_global_graph_retirement(reopened, global_plan, board_databases={"board-a": board}) == result
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["exception", "survivor", "board_drift"])
def test_partial_global_removal_rolls_back_and_never_claims_cross_store_atomicity(graphs, monkeypatch, failure):
    board, global_db, _, _, original = graphs
    board_plan, global_plan = prepare(graphs)
    board_retirement.apply_sprint_graph_retirement(board, board_plan)
    remove = global_retirement._remove_digest
    def fail(transaction, identity):
        remove(transaction, identity)
        if failure == "exception":
            raise RuntimeError("injected after digest removal")
        if failure == "survivor":
            transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.aliases=$value", {"id": "shared", "value": "unplanned"})
        else:
            with board.begin("write") as changed:
                changed.execute("MATCH (n:Entity) WHERE n.id=$id SET n.title=$value", {"id": "spec", "value": "raced"})
    monkeypatch.setattr(global_retirement, "_remove_digest", fail)
    with pytest.raises((ValueError, RuntimeError), match="(injected|after_mismatch|board_not_retired)"):
        apply(graphs, global_plan)
    assert fingerprint(global_db) == original.fingerprint
    if failure != "board_drift":
        assert fingerprint(board, "board") == board_plan.after_sha256


@pytest.mark.parametrize("damage", ["global", "plan", "board_population"])
def test_changed_global_or_retained_selection_refuses_before_global_effects(graphs, damage):
    board, global_db, *_ = graphs
    board_plan, global_plan = prepare(graphs)
    board_retirement.apply_sprint_graph_retirement(board, board_plan)
    if damage == "global":
        with global_db.begin("write") as transaction:
            transaction.execute("MATCH (n:Topic) WHERE n.id=$id SET n.name=$value", {"id": "topic", "value": "changed"})
    elif damage == "plan":
        global_plan = replace(global_plan, digest_ids=global_plan.digest_ids[:1])
    before = fingerprint(global_db)
    with pytest.raises(ValueError, match="(before_mismatch|selection_mismatch|population_mismatch)"):
        if damage == "board_population":
            global_retirement.apply_global_graph_retirement(global_db, global_plan, board_databases={})
        else:
            apply(graphs, global_plan)
    assert fingerprint(global_db) == before


@pytest.mark.parametrize("damage", ["counter", "missing_summary", "source_alias"])
def test_preparation_rejects_inconsistent_counter_missing_board_and_ambiguous_source(graphs, damage):
    board, global_db, *_ = graphs
    if damage == "source_alias":
        with board.begin("write") as transaction:
            transaction.execute("CREATE (:Criterion {id:$id, source_artifact_ref:$ref})", {"id": "root", "ref": "spec:spec"})
    else:
        with global_db.begin("write") as transaction:
            if damage == "counter":
                transaction.execute("MATCH (b:Board) WHERE b.board_id=$id SET b.decision_count=99", {"id": "board-a"})
            else:
                transaction.execute("MATCH (b:Board) WHERE b.board_id=$id DETACH DELETE b", {"id": "board-a"})
    before = fingerprint(global_db)
    with pytest.raises(ValueError, match="(count_mismatch|summary_missing|identity_ambiguous)"):
        prepare(graphs)
    assert fingerprint(global_db) == before


def test_missing_digest_rows_do_not_change_authoritative_board_counter_delta(graphs):
    board, global_db, *_ = graphs
    with global_db.begin("write") as transaction:
        transaction.execute("MATCH (d:DecisionDigest) WHERE d.id IN $ids DETACH DELETE d",
            {"ids": ["a-root", "a-root-copy", "a-outcome"]})
    board_plan, global_plan = prepare(graphs)
    assert global_plan.digest_ids == () and global_plan.board_counts == (("board-a", 4, 2),)
    board_retirement.apply_sprint_graph_retirement(board, board_plan)
    apply(graphs, global_plan)
    assert fingerprint(global_db) == global_plan.after_sha256


@pytest.mark.parametrize("change", ["embedding", "revoked", "superseded"])
def test_authoritative_count_obeys_current_outbox_source_visibility(graphs, change):
    board, global_db, *_ = graphs
    with board.begin("write") as transaction:
        if change == "embedding":
            transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.embedding=NULL", {"id": "root"})
        elif change == "revoked":
            transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.revocation_reason=$reason", {"id": "spec", "reason": "historical"})
        else:
            transaction.execute("MATCH (n:Entity) WHERE n.id=$id SET n.superseded_by=$other", {"id": "spec", "other": "newer"})
    with global_db.begin("write") as transaction:
        transaction.execute("MATCH (b:Board) WHERE b.board_id=$id SET b.decision_count=3", {"id": "board-a"})
    board_plan, global_plan = prepare(graphs)
    assert global_plan.board_counts == (("board-a", 3, 2 if change == "embedding" else 1),)
    board_retirement.apply_sprint_graph_retirement(board, board_plan)
    apply(graphs, global_plan)
    assert fingerprint(global_db) == global_plan.after_sha256


def test_multiple_affected_boards_must_all_finish_before_global_cleanup(graphs, tmp_path):
    board_a, global_db, _, original, _ = graphs
    second = replace(original, nodes=tuple(replace(node, properties={**node.properties,
        "source_artifact_ref": "sprint:origin-b"}) if node.properties["source_artifact_ref"] == "sprint:origin" else node
        for node in original.nodes))
    path = tmp_path / "board-b.grafx"
    seed_generation("grafx", path, second)
    board_b = open_generation_database("grafx", path, "board", read_only=False)
    try:
        with global_db.begin("write") as transaction:
            transaction.execute("MATCH (b:Board) WHERE b.board_id=$id SET b.decision_count=4", {"id": "board-b"})
        a_plan = board_retirement.prepare_sprint_graph_retirement(board_a, board_id="board-a", archived_origin_ids=frozenset({"origin"}))
        b_plan = board_retirement.prepare_sprint_graph_retirement(board_b, board_id="board-b", archived_origin_ids=frozenset({"origin-b"}))
        plan = global_retirement.prepare_global_graph_retirement(global_db, ((board_b, b_plan), (board_a, a_plan)))
        assert plan.digest_ids == ("a-outcome", "a-root", "a-root-copy", "b-root")
        assert plan.board_counts == (("board-a", 4, 2), ("board-b", 4, 2))
        board_retirement.apply_sprint_graph_retirement(board_a, a_plan)
        before = fingerprint(global_db)
        with pytest.raises(ValueError, match="board_not_retired"):
            global_retirement.apply_global_graph_retirement(global_db, plan, board_databases={"board-a": board_a, "board-b": board_b})
        assert fingerprint(global_db) == before
        board_retirement.apply_sprint_graph_retirement(board_b, b_plan)
        global_retirement.apply_global_graph_retirement(global_db, plan, board_databases={"board-a": board_a, "board-b": board_b})
        assert fingerprint(global_db) == plan.after_sha256
    finally:
        board_b.close()
