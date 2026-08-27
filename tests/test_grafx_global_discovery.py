from __future__ import annotations

from pathlib import Path

import pytest
from okto_grafx import VectorValue, connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

from okto_pulse.community.adapters.grafx_global_discovery import (
    PULSE_GRAFX_GLOBAL_SCHEMA,
    certify_grafx_global_vector_indexes,
    ensure_current_grafx_global_schema,
    search_grafx_decision_digests,
    upsert_grafx_board_summary_vector,
    upsert_grafx_decision_digest_vector,
    validate_current_grafx_global_schema,
)


def _vector(first: float, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * 382)]


def _stored_vector(database, space: str, values: list[float]) -> VectorValue:
    return VectorValue(
        values=tuple(values),
        space_ref=database.catalog.catalog.space(space).space_id,
        dtype="float64",
    )


def _upsert_board(database, board_id: str) -> None:
    upsert_grafx_board_summary_vector(
        database,
        board_id=board_id,
        name=board_id.upper(),
        summary=f"summary-{board_id}",
        summary_embedding=_vector(1.0),
        decision_count=0,
        synced_at="2026-08-27T12:00:00Z",
    )


def _upsert_digest(
    database,
    *,
    board_id: str,
    digest_id: str,
    graph_layer: str = "canonical",
    embedding: list[float] | None = None,
) -> None:
    upsert_grafx_decision_digest_vector(
        database,
        digest_id=digest_id,
        board_id=board_id,
        original_node_id=f"node-{digest_id}",
        title=f"title-{digest_id}",
        summary=f"summary-{digest_id}",
        node_type="Decision",
        graph_layer=graph_layer,
        embedding=embedding or _vector(1.0),
        created_at="2026-08-27T12:00:00Z",
    )
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (b:Board {board_id: $board_id}), "
            "(d:DecisionDigest {id: $digest_id}) "
            "CREATE (b)-[:CONTAINS_DECISION]->(d)",
            {"board_id": board_id, "digest_id": digest_id},
        )


def test_global_manifest_bootstrap_status_and_cold_reopen(tmp_path: Path) -> None:
    root = tmp_path / "global"
    expected_mapping = (
        ("Board", "summary_embedding", "board_summary_idx"),
        ("Topic", "centroid_embedding", "topic_centroid_idx"),
        ("Entity", "embedding", "entity_embedding_idx"),
        ("DecisionDigest", "embedding", "digest_embedding_idx"),
    )
    observed_mapping = tuple(
        (table.name, column.name, column.vector_space)
        for table in PULSE_GRAFX_GLOBAL_SCHEMA.nodes
        for column in table.columns
        if column.vector_space is not None
    )
    assert observed_mapping == expected_mapping

    with connect(root) as database:
        created = ensure_current_grafx_global_schema(database)
        assert created.changed is True
        committed_lsn = database.transactions.published_lsn()

        repeated = ensure_current_grafx_global_schema(database)
        assert repeated.changed is False
        assert repeated.logical_fingerprint == created.logical_fingerprint
        assert database.transactions.published_lsn() == committed_lsn

        statuses = certify_grafx_global_vector_indexes(database)
        assert tuple((item.table, item.column, item.space) for item in statuses) == (
            expected_mapping
        )
        assert all(item.built_through_lsn == committed_lsn for item in statuses)
        assert database.verify("all").findings == ()

    with connect(root) as reopened:
        assert (
            validate_current_grafx_global_schema(reopened)
            == created.logical_fingerprint
        )
        cold_statuses = certify_grafx_global_vector_indexes(reopened)
        assert tuple(item.space for item in cold_statuses) == tuple(
            item[2] for item in expected_mapping
        )
        assert reopened.verify("all").findings == ()


def test_global_bootstrap_rejects_divergence_before_writing() -> None:
    with connect(":memory:") as database:
        with database.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE Foreign(id STRING, PRIMARY KEY(id))")
        before = database.transactions.published_lsn()

        with pytest.raises(GraphCapabilityUnavailable) as refusal:
            ensure_current_grafx_global_schema(database)

        assert refusal.value.details["reason"] == "unexpected_schema_object"
        assert database.transactions.published_lsn() == before
        assert database.catalog.catalog.spaces() == ()


def test_global_exact_search_enforces_acl_lifecycle_layer_and_total_order() -> None:
    with connect(":memory:", vector_exact_scan_threshold=4096) as database:
        ensure_current_grafx_global_schema(database)
        for board_id in ("board-a", "board-b"):
            _upsert_board(database, board_id)
        _upsert_digest(database, board_id="board-a", digest_id="digest-b")
        _upsert_digest(database, board_id="board-a", digest_id="digest-a")
        _upsert_digest(database, board_id="board-b", digest_id="digest-aa")
        _upsert_digest(
            database,
            board_id="board-a",
            digest_id="working",
            graph_layer="working",
        )
        _upsert_digest(database, board_id="board-a", digest_id="revoked")
        _upsert_digest(database, board_id="board-a", digest_id="legacy")
        with database.begin("write") as transaction:
            transaction.execute(
                "MATCH (b:Board {board_id: 'board-a'}), "
                "(d:DecisionDigest {id: 'digest-a'}) "
                "CREATE (b)-[:CONTAINS_DECISION]->(d)"
            )
            transaction.execute(
                "MATCH (d:DecisionDigest {id: 'revoked'}) SET d.source_revoked = true"
            )
            transaction.execute(
                "MATCH (d:DecisionDigest {id: 'legacy'}) SET d.graph_layer = NULL"
            )
            transaction.execute(
                "CREATE (:DecisionDigest {id: 'nullable', board_id: 'board-a', "
                "original_node_id: 'node-nullable', title: 'nullable', "
                "one_line_summary: 'nullable', node_type: 'Decision', "
                "graph_layer: 'canonical', source_revoked: false, "
                "created_at: timestamp('2026-08-27T12:00:00Z')})"
            )
            transaction.execute(
                "MATCH (b:Board {board_id: 'board-a'}), "
                "(d:DecisionDigest {id: 'nullable'}) "
                "CREATE (b)-[:CONTAINS_DECISION]->(d)"
            )

        canonical = search_grafx_decision_digests(
            database,
            _vector(1.0),
            board_ids=("board-b", "board-a", "board-a"),
            graph_layer="canonical",
            top_k=20,
            min_similarity=1.0,
            exhaustive=True,
        )
        assert [(row["board_id"], row["digest_id"]) for row in canonical] == [
            ("board-a", "digest-a"),
            ("board-a", "digest-b"),
            ("board-b", "digest-aa"),
        ]
        assert all(
            tuple(row)
            == (
                "board_id",
                "digest_id",
                "id",
                "title",
                "summary",
                "node_type",
                "graph_layer",
                "similarity",
            )
            for row in canonical
        )

        all_layers = search_grafx_decision_digests(
            database,
            _vector(1.0),
            board_ids=("board-a",),
            graph_layer="all",
            top_k=20,
            min_similarity=1.0,
            exhaustive=True,
        )
        by_id = {row["digest_id"]: row for row in all_layers}
        assert set(by_id) == {"digest-a", "digest-b", "legacy", "working"}
        assert by_id["legacy"]["graph_layer"] == "legacy_unknown"
        assert "revoked" not in by_id
        assert "nullable" not in by_id


def test_global_bounded_search_falls_back_on_zero_tail_tie() -> None:
    with connect(":memory:", vector_exact_scan_threshold=0) as database:
        ensure_current_grafx_global_schema(database)
        _upsert_board(database, "board")
        _upsert_digest(
            database,
            board_id="board",
            digest_id="z-last",
            embedding=_vector(-1.0),
        )
        _upsert_digest(
            database,
            board_id="board",
            digest_id="a-first",
            embedding=_vector(-1.0),
        )
        # Fill the physical top_k+1 page with the same digest twice.  After
        # logical identity deduplication there is no unique cutoff witness, so
        # the bounded path must use the complete exact order.
        with database.begin("write") as transaction:
            transaction.execute(
                "MATCH (b:Board {board_id: 'board'}), "
                "(d:DecisionDigest {id: 'z-last'}) "
                "CREATE (b)-[:CONTAINS_DECISION]->(d)"
            )

        result = search_grafx_decision_digests(
            database,
            _vector(1.0),
            board_ids=("board",),
            graph_layer="canonical",
            top_k=1,
            min_similarity=0.0,
        )

        assert [(row["digest_id"], row["similarity"]) for row in result] == [
            ("a-first", 0.0)
        ]
        zero_query = search_grafx_decision_digests(
            database,
            _vector(0.0),
            board_ids=("board",),
            graph_layer="canonical",
            top_k=1,
            min_similarity=0.0,
        )
        assert [(row["digest_id"], row["similarity"]) for row in zero_query] == [
            ("a-first", 0.0)
        ]


def test_global_vector_replacements_survive_reopen(tmp_path: Path) -> None:
    root = tmp_path / "global"
    with connect(root, vector_exact_scan_threshold=4096) as database:
        ensure_current_grafx_global_schema(database)
        _upsert_board(database, "board")
        _upsert_digest(
            database,
            board_id="board",
            digest_id="digest",
            embedding=_vector(0.0, 1.0),
        )
        assert (
            search_grafx_decision_digests(
                database,
                _vector(1.0),
                board_ids=("board",),
                graph_layer="canonical",
                top_k=1,
                min_similarity=0.5,
                exhaustive=True,
            )
            == []
        )

        upsert_grafx_board_summary_vector(
            database,
            board_id="board",
            name="updated",
            summary="updated",
            summary_embedding=_vector(0.0, 1.0),
            decision_count=1,
            synced_at="2026-08-27T13:00:00Z",
        )
        assert (
            upsert_grafx_decision_digest_vector(
                database,
                digest_id="digest",
                board_id="board",
                original_node_id="node-digest",
                title="updated",
                summary="updated",
                node_type="Decision",
                graph_layer="canonical",
                embedding=_vector(1.0),
                created_at="2026-08-27T13:00:00Z",
            )
            == "updated"
        )
        assert database.verify("all").findings == ()

    with connect(root, vector_exact_scan_threshold=4096) as reopened:
        hits = search_grafx_decision_digests(
            reopened,
            _vector(1.0),
            board_ids=("board",),
            graph_layer="canonical",
            top_k=1,
            min_similarity=1.0,
            exhaustive=False,
        )
        assert [(row["digest_id"], row["similarity"]) for row in hits] == [
            ("digest", 1.0)
        ]
        rows = reopened.execute(
            "MATCH (b:Board {board_id: 'board'}) "
            "RETURN b.name, b.summary, b.summary_embedding"
        ).rows
        assert rows[0][0:2] == ("updated", "updated")
        assert rows[0][2] == _stored_vector(
            reopened, "board_summary_idx", _vector(0.0, 1.0)
        )
        assert reopened.verify("all").findings == ()


def test_global_ann_is_stable_cold_warm_after_reopen_and_never_leaks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "global-ann"
    database = connect(root, vector_exact_scan_threshold=0)
    ensure_current_grafx_global_schema(database)
    for board_id in ("allowed", "denied"):
        _upsert_board(database, board_id)
    _upsert_digest(
        database,
        board_id="allowed",
        digest_id="best",
        embedding=_vector(1.0),
    )
    _upsert_digest(
        database,
        board_id="allowed",
        digest_id="second",
        embedding=_vector(0.8, 0.6),
    )
    _upsert_digest(
        database,
        board_id="allowed",
        digest_id="third",
        embedding=_vector(0.0, 1.0),
    )
    _upsert_digest(
        database,
        board_id="allowed",
        digest_id="opposite",
        embedding=_vector(-1.0),
    )
    _upsert_digest(
        database,
        board_id="allowed",
        digest_id="ineligible-working",
        graph_layer="working",
    )
    _upsert_digest(
        database,
        board_id="allowed",
        digest_id="ineligible-revoked",
    )
    _upsert_digest(
        database,
        board_id="denied",
        digest_id="ineligible-board",
    )
    with database.begin("write") as transaction:
        transaction.execute(
            "MATCH (d:DecisionDigest {id: 'ineligible-revoked'}) "
            "SET d.source_revoked = true"
        )
        transaction.execute(
            "CREATE (:DecisionDigest {id: 'ineligible-null', board_id: 'allowed', "
            "original_node_id: 'node-ineligible-null', title: 'nullable', "
            "one_line_summary: 'nullable', node_type: 'Decision', "
            "graph_layer: 'canonical', source_revoked: false, "
            "created_at: timestamp('2026-08-27T12:00:00Z')})"
        )
        transaction.execute(
            "MATCH (b:Board {board_id: 'allowed'}), "
            "(d:DecisionDigest {id: 'ineligible-null'}) "
            "CREATE (b)-[:CONTAINS_DECISION]->(d)"
        )

    def search(handle) -> list[dict]:
        return search_grafx_decision_digests(
            handle,
            _vector(1.0),
            board_ids=("allowed",),
            graph_layer="canonical",
            top_k=2,
            min_similarity=0.0,
        )

    cold = search(database)
    warm = search(database)
    database.close()

    with connect(root, vector_exact_scan_threshold=0) as reopened:
        cold_reopen = search(reopened)

    assert [hit["digest_id"] for hit in cold] == ["best", "second"]
    assert warm == cold
    assert cold_reopen == cold
    assert not {
        "ineligible-working",
        "ineligible-revoked",
        "ineligible-board",
        "ineligible-null",
    }.intersection(hit["digest_id"] for hit in (*cold, *warm, *cold_reopen))
