"""Finite M-PULSE-4 V1--V7 acceptance matrix.

The frozen contract deliberately separates broad mapping proof (V1/V2) from
representative lifecycle proof (V3--V7).  This module keeps that shape: schema
templates are built once, the thirteen public mappings are never multiplied by
every filter/lifecycle state, and the calibrated 8,192x384/256-query recall run
remains the existing standalone Grafx gate.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import okto_grafx
import pytest
from okto_grafx import Database, Timestamp, VectorValue
from okto_grafx.errors import GrafxIndexError
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.interfaces.graph_errors import GraphIndexUnavailable
from okto_pulse.core.kg.schema_contract import VECTOR_INDEX_TYPES

from okto_pulse.community.adapters.grafx_board_vector_search import (
    CommunityGrafxBoardVectorSearch,
)
from okto_pulse.community.adapters.grafx_global_discovery import (
    PULSE_GRAFX_GLOBAL_SCHEMA,
    certify_grafx_global_vector_indexes,
    ensure_current_grafx_global_schema,
    search_grafx_decision_digests,
    upsert_grafx_board_summary_vector,
    upsert_grafx_decision_digest_vector,
    validate_current_grafx_global_schema,
)
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    ensure_current_grafx_board_schema,
    validate_current_grafx_schema,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    EMBEDDING_DIMENSION,
    PULSE_GRAFX_SCHEMA_MANIFEST,
)

_BOARD_ID = "m4-acceptance-board"
_STAMP = Timestamp(micros=1_788_000_000_123_456)
_AT = "2026-08-27T12:00:00Z"
_SCORE_ABS_TOL = 1e-9
_SCORE_REL_TOL = 1e-9

_BOARD_MAPPED = tuple(
    (space.node_type, "embedding", space.name)
    for space in PULSE_GRAFX_SCHEMA_MANIFEST.spaces
    if space.node_type in VECTOR_INDEX_TYPES
)
_BOARD_NON_PUBLIC = tuple(
    (space.node_type, "embedding", space.name)
    for space in PULSE_GRAFX_SCHEMA_MANIFEST.spaces
    if space.node_type not in VECTOR_INDEX_TYPES
)
_GLOBAL_MAPPED = tuple(
    (table.name, column.name, column.vector_space)
    for table in PULSE_GRAFX_GLOBAL_SCHEMA.nodes
    for column in table.columns
    if column.vector_space is not None
)
_ALL_MAPPED = (*_BOARD_MAPPED, *_GLOBAL_MAPPED)
_EXPECTED_BOARD_MAPPED = (
    ("Decision", "embedding", "decision_embedding_idx"),
    ("Criterion", "embedding", "criterion_embedding_idx"),
    ("Constraint", "embedding", "constraint_embedding_idx"),
    ("Requirement", "embedding", "requirement_embedding_idx"),
    ("Entity", "embedding", "entity_embedding_idx"),
    ("APIContract", "embedding", "apicontract_embedding_idx"),
    ("TestScenario", "embedding", "testscenario_embedding_idx"),
    ("Bug", "embedding", "bug_embedding_idx"),
    ("Learning", "embedding", "learning_embedding_idx"),
)
_EXPECTED_GLOBAL_MAPPED = (
    ("Board", "summary_embedding", "board_summary_idx"),
    ("Topic", "centroid_embedding", "topic_centroid_idx"),
    ("Entity", "embedding", "entity_embedding_idx"),
    ("DecisionDigest", "embedding", "digest_embedding_idx"),
)


def _vector(first: float, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * (EMBEDDING_DIMENSION - 2))]


def _stored_vector(
    database: Database, space: str, values: Sequence[float]
) -> VectorValue:
    return VectorValue(
        values=tuple(float(value) for value in values),
        space_ref=database.catalog.catalog.space(space).space_id,
        dtype="float64",
    )


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Independent, stable cosine oracle; no Grafx or adapter helper is reused."""

    left_scale = max((abs(value) for value in left), default=0.0)
    right_scale = max((abs(value) for value in right), default=0.0)
    if left_scale == 0.0 or right_scale == 0.0:
        return 0.0
    scaled_left = tuple(value / left_scale for value in left)
    scaled_right = tuple(value / right_scale for value in right)
    denominator = math.hypot(*scaled_left) * math.hypot(*scaled_right)
    raw = (
        math.fsum(
            left_value * right_value
            for left_value, right_value in zip(scaled_left, scaled_right)
        )
        / denominator
    )
    return max(0.0, min(1.0, raw))


def _oracle(
    rows: Iterable[tuple[str, Sequence[float]]], query: Sequence[float]
) -> list[tuple[str, float]]:
    outcome = [(identity, _cosine(query, embedding)) for identity, embedding in rows]
    outcome.sort(key=lambda item: (-item[1], item[0]))
    return outcome


def _assert_scores(
    observed: Sequence[tuple[str, float]], expected: Sequence[tuple[str, float]]
) -> None:
    assert [identity for identity, _score in observed] == [
        identity for identity, _score in expected
    ]
    assert len(observed) == len(expected)
    for (_observed_id, observed_score), (_expected_id, expected_score) in zip(
        observed, expected
    ):
        assert math.isclose(
            observed_score,
            expected_score,
            abs_tol=_SCORE_ABS_TOL,
            rel_tol=_SCORE_REL_TOL,
        )


def _assert_ready_index(
    database: Database,
    *,
    table: str,
    column: str,
    space: str,
    allow_unloaded_positions: bool = False,
) -> tuple[bool, bool]:
    """Correlate public views and return which diagnostic positions were evicted.

    ``None`` never means healthy.  The warm caller records it without certifying a
    position; the reopened caller already has the cold-open freshness proof, because
    assembly read every header and would have marked a position unequal to published
    stale before this snapshot was exposed.
    """

    catalog = database.catalog.catalog
    space_view = database.vectors.space(space)
    vector_view = database.vectors.index(space)
    generic_view = database.indexes.index(vector_view.name)
    table_view = catalog.table(table)
    position = next(
        index
        for index, candidate in enumerate(table_view.columns)
        if candidate.name == column
    )
    published = database.transactions.published_lsn()

    assert (
        space_view.dimension,
        space_view.metric.value,
        space_view.normalized,
        space_view.storage_dtype,
        space_view.state,
    ) == (EMBEDDING_DIMENSION, "cosine", False, "float64", "active")
    assert (
        vector_view.space_id,
        vector_view.space_name,
        vector_view.dimension,
        vector_view.metric_of_space.value,
        vector_view.storage_dtype,
    ) == (
        space_view.space_id,
        space,
        EMBEDDING_DIMENSION,
        "cosine",
        "float64",
    )
    assert generic_view.name == vector_view.name
    assert generic_view.visibility.value == "proximity"
    assert generic_view.definition.table_name == table
    assert generic_view.definition.positions == (position,)
    assert vector_view.stale is generic_view.stale is False
    assert vector_view.stale_reason is generic_view.stale_reason is None
    vector_position_missing = vector_view.built_through_lsn is None
    generic_position_missing = generic_view.built_through_lsn is None
    if not (allow_unloaded_positions and vector_position_missing):
        assert vector_view.built_through_lsn == published
    if not (allow_unloaded_positions and generic_position_missing):
        assert generic_view.built_through_lsn == published
    return vector_position_missing, generic_position_missing


@dataclass(frozen=True, slots=True)
class _SchemaTemplates:
    board: Path
    global_: Path
    board_fingerprint: str
    global_fingerprint: str
    warm_missing_positions: tuple[str, ...]
    cold_missing_positions: tuple[str, ...]


@pytest.fixture(scope="module")
def schema_templates(tmp_path_factory: pytest.TempPathFactory) -> _SchemaTemplates:
    """Build V1 once; later cases copy the closed on-disk fixtures."""

    root = tmp_path_factory.mktemp("m4-vector-schema-templates")
    board_path = root / "board"
    global_path = root / "global"
    warm_missing_positions: list[str] = []
    cold_missing_positions: list[str] = []

    with okto_grafx.connect(board_path) as board:
        created = ensure_current_grafx_board_schema(
            board,
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
        assert created.changed is True
        board_lsn = board.transactions.published_lsn()
        repeated = ensure_current_grafx_board_schema(
            board,
            board_id=_BOARD_ID,
            bootstrapped_at=_STAMP,
        )
        assert repeated.changed is False
        assert repeated.logical_fingerprint == created.logical_fingerprint
        assert board.transactions.published_lsn() == board_lsn
        assert board.verify("all").findings == ()
        for table, column, space in (*_BOARD_MAPPED, *_BOARD_NON_PUBLIC):
            missing_vector, missing_generic = _assert_ready_index(
                board,
                table=table,
                column=column,
                space=space,
                allow_unloaded_positions=True,
            )
            if missing_vector or missing_generic:
                warm_missing_positions.append(
                    f"{space}:vector={missing_vector}:generic={missing_generic}"
                )

    with okto_grafx.connect(global_path) as global_database:
        created_global = ensure_current_grafx_global_schema(global_database)
        assert created_global.changed is True
        global_lsn = global_database.transactions.published_lsn()
        repeated_global = ensure_current_grafx_global_schema(global_database)
        assert repeated_global.changed is False
        assert repeated_global.logical_fingerprint == created_global.logical_fingerprint
        assert global_database.transactions.published_lsn() == global_lsn
        assert (
            tuple(
                (status.table, status.column, status.space)
                for status in certify_grafx_global_vector_indexes(global_database)
            )
            == _GLOBAL_MAPPED
        )
        assert global_database.verify("all").findings == ()

    # V1 reopen: assembly's cold indexes.open/check_freshness pass compares each
    # durable header with published_lsn before exposing this handle.  A header may be
    # evicted again before the diagnostic views below are captured, but stale=False on
    # this cold-open handle is still the durable position proof.  verify("all") then
    # independently proves index coverage and references.
    with okto_grafx.connect(board_path) as reopened_board:
        assert (
            validate_current_grafx_schema(reopened_board) == created.logical_fingerprint
        )
        for table, column, space in (*_BOARD_MAPPED, *_BOARD_NON_PUBLIC):
            missing_vector, missing_generic = _assert_ready_index(
                reopened_board,
                table=table,
                column=column,
                space=space,
                allow_unloaded_positions=True,
            )
            if missing_vector or missing_generic:
                cold_missing_positions.append(
                    f"{space}:vector={missing_vector}:generic={missing_generic}"
                )
        assert reopened_board.verify("all").findings == ()

    with okto_grafx.connect(global_path) as reopened_global:
        assert (
            validate_current_grafx_global_schema(reopened_global)
            == created_global.logical_fingerprint
        )
        assert (
            tuple(
                (status.table, status.column, status.space)
                for status in certify_grafx_global_vector_indexes(reopened_global)
            )
            == _GLOBAL_MAPPED
        )
        assert reopened_global.verify("all").findings == ()

    return _SchemaTemplates(
        board=board_path,
        global_=global_path,
        board_fingerprint=created.logical_fingerprint,
        global_fingerprint=created_global.logical_fingerprint,
        warm_missing_positions=tuple(warm_missing_positions),
        cold_missing_positions=tuple(cold_missing_positions),
    )


def _copy_template(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def test_v1_all_thirteen_mappings_are_ready_and_only_nine_board_types_search(
    schema_templates: _SchemaTemplates,
    record_property: Any,
) -> None:
    assert len(_BOARD_MAPPED) == 9
    assert len(_GLOBAL_MAPPED) == 4
    assert len(_ALL_MAPPED) == 13
    assert _BOARD_MAPPED == _EXPECTED_BOARD_MAPPED
    assert _GLOBAL_MAPPED == _EXPECTED_GLOBAL_MAPPED
    assert _BOARD_NON_PUBLIC == (
        ("Assumption", "embedding", "assumption_embedding_idx"),
        ("Alternative", "embedding", "alternative_embedding_idx"),
    )
    # Both public views explicitly report ``None`` while a header is not resident.
    # Warm ``None`` is diagnostic only.  After reopen, stale=False is backed by the
    # cold-open freshness comparison; verify("all") proves coverage/references, not LSN.
    # Every position that remains observable must still equal published_lsn exactly.
    record_property(
        "warm_positions_not_resident",
        ",".join(schema_templates.warm_missing_positions),
    )
    record_property(
        "cold_positions_not_resident",
        ",".join(schema_templates.cold_missing_positions),
    )

    with okto_grafx.connect(schema_templates.board) as board:
        resolved: list[str] = []
        search = CommunityGrafxBoardVectorSearch(
            lambda board_id: resolved.append(board_id) or board
        )
        for node_type, _column, _space in _BOARD_MAPPED:
            assert (
                search.vector_search(
                    _BOARD_ID,
                    node_type,
                    _vector(1.0),
                    1,
                    0.0,
                )
                == []
            )
        for node_type, _column, _space in _BOARD_NON_PUBLIC:
            assert (
                search.vector_search(
                    _BOARD_ID,
                    node_type,
                    _vector(1.0),
                    1,
                    0.0,
                )
                == []
            )
        assert resolved == [_BOARD_ID] * 9


def _insert_mapping_rows(
    database: Database,
    *,
    table: str,
    primary_key: str,
    column: str,
    space: str,
    rows: Sequence[tuple[str, Sequence[float]]],
) -> None:
    with database.begin("write") as writer:
        for identity, values in rows:
            writer.execute(
                f"CREATE (n:{table} {{{primary_key}: $identity, "
                f"{column}: $embedding}})",
                {
                    "identity": identity,
                    "embedding": _stored_vector(database, space, values),
                },
            )


def _raw_exact_results(
    database: Database,
    *,
    table: str,
    primary_key: str,
    column: str,
    space: str,
    query: Sequence[float],
    limit: int,
) -> list[tuple[str, float]]:
    statement = (
        f"MATCH (n:{table}) WHERE n.{column} IS NOT NULL "
        f"AND similarity(n.{column}, $query, space => '{space}') >= -1.0 "
        f"RETURN n.{primary_key}, similarity_score() AS score "
        f"ORDER BY score DESC, n.{primary_key} ASC LIMIT {limit}"
    )
    with database.begin("read") as reader:
        result = reader.execute(statement, {"query": tuple(query)})
    # Grafx deliberately does not expose its internal regime on QueryResult.  The
    # configured exact threshold plus this complete population bound selects the
    # exact path without coupling Pulse tests to a private planner object.
    assert database.vectors.exact_scan_threshold >= limit
    normalized = [
        (str(row[0]), max(0.0, min(1.0, float(row[1])))) for row in result.rows
    ]
    return normalized


def test_v2_all_thirteen_mappings_match_an_independent_exact_oracle(
    schema_templates: _SchemaTemplates,
    tmp_path: Path,
) -> None:
    board_path = _copy_template(schema_templates.board, tmp_path / "v2-board")
    global_path = _copy_template(schema_templates.global_, tmp_path / "v2-global")
    fixture_rows = (
        ("z-best", _vector(1.0)),
        ("b-tied", _vector(0.5, math.sqrt(0.75))),
        ("a-tied", _vector(0.5, math.sqrt(0.75))),
        ("negative", _vector(-1.0)),
    )
    expected = _oracle(fixture_rows, _vector(1.0))

    with okto_grafx.connect(board_path, vector_exact_scan_threshold=4096) as board:
        for table, column, space in _BOARD_MAPPED:
            _insert_mapping_rows(
                board,
                table=table,
                primary_key="id",
                column=column,
                space=space,
                rows=fixture_rows,
            )
    with okto_grafx.connect(global_path, vector_exact_scan_threshold=4096) as global_db:
        primary_keys = {
            table.name: str(table.primary_key)
            for table in PULSE_GRAFX_GLOBAL_SCHEMA.nodes
        }
        for table, column, space in _GLOBAL_MAPPED:
            _insert_mapping_rows(
                global_db,
                table=table,
                primary_key=primary_keys[table],
                column=column,
                space=space,
                rows=fixture_rows,
            )

    # Query only after a reopen: the mapping and scores must be durable, not cache evidence.
    with okto_grafx.connect(board_path, vector_exact_scan_threshold=4096) as board:
        for table, column, space in _BOARD_MAPPED:
            observed = _raw_exact_results(
                board,
                table=table,
                primary_key="id",
                column=column,
                space=space,
                query=_vector(1.0),
                limit=len(fixture_rows),
            )
            _assert_scores(observed, expected)

            public = CommunityGrafxBoardVectorSearch(lambda _board_id: board)
            hits = public.vector_search(
                _BOARD_ID,
                table,
                _vector(1.0),
                len(fixture_rows),
                0.0,
            )
            _assert_scores(
                [(str(hit["node_id"]), float(hit["similarity"])) for hit in hits],
                expected,
            )

    with okto_grafx.connect(global_path, vector_exact_scan_threshold=4096) as global_db:
        primary_keys = {
            table.name: str(table.primary_key)
            for table in PULSE_GRAFX_GLOBAL_SCHEMA.nodes
        }
        for table, column, space in _GLOBAL_MAPPED:
            observed = _raw_exact_results(
                global_db,
                table=table,
                primary_key=primary_keys[table],
                column=column,
                space=space,
                query=_vector(1.0),
                limit=len(fixture_rows),
            )
            _assert_scores(observed, expected)


def _create_decision_schema(database: Database) -> None:
    with database.begin("write") as writer:
        writer.execute(
            "CREATE VECTOR SPACE decision_embedding_idx "
            "{dimension: 384, metric: 'cosine', normalized: false, "
            "storage_dtype: 'float64'}"
        )
        writer.execute(
            "CREATE NODE TABLE Decision("
            "id STRING, title STRING, source_artifact_ref STRING, "
            "content STRING, context STRING, justification STRING, "
            "kind_of STRING, embedding VECTOR(decision_embedding_idx), "
            "superseded_by STRING, graph_layer STRING, revocation_reason STRING, "
            "PRIMARY KEY(id))"
        )


def _insert_decision(
    database: Database,
    node_id: str,
    embedding: Sequence[float] | None,
    *,
    graph_layer: str | None = "canonical",
    superseded_by: str | None = None,
    revocation_reason: str | None = None,
) -> None:
    with database.begin("write") as writer:
        writer.execute(
            "CREATE (:Decision {id: $id, title: $title, "
            "source_artifact_ref: $source, content: $content, context: $context, "
            "justification: $justification, kind_of: 'decision', "
            "embedding: $embedding, superseded_by: $superseded_by, "
            "graph_layer: $graph_layer, revocation_reason: $revocation_reason})",
            {
                "id": node_id,
                "title": f"title-{node_id}",
                "source": f"spec:{node_id}",
                "content": f"content-{node_id}",
                "context": f"context-{node_id}",
                "justification": f"justification-{node_id}",
                "embedding": (
                    None
                    if embedding is None
                    else _stored_vector(database, "decision_embedding_idx", embedding)
                ),
                "superseded_by": superseded_by,
                "graph_layer": graph_layer,
                "revocation_reason": revocation_reason,
            },
        )


def _board_ids(
    search: CommunityGrafxBoardVectorSearch,
    *,
    graph_layer: str = "all",
    include_superseded: bool = False,
    threshold: float = 0.0,
    top_k: int = 100,
) -> list[str]:
    return [
        str(hit["node_id"])
        for hit in search.vector_search(
            _BOARD_ID,
            "Decision",
            _vector(1.0),
            top_k,
            threshold,
            graph_layer=graph_layer,
            include_superseded=include_superseded,
        )
    ]


def test_v3_board_filters_are_applied_before_exact_ranking() -> None:
    with okto_grafx.connect(":memory:", vector_exact_scan_threshold=4096) as database:
        _create_decision_schema(database)
        _insert_decision(database, "canonical", _vector(1.0))
        _insert_decision(database, "negative", _vector(-1.0))
        _insert_decision(database, "working", _vector(1.0), graph_layer="working")
        _insert_decision(database, "legacy", _vector(1.0), graph_layer=None)
        _insert_decision(database, "nullable", None)
        _insert_decision(
            database,
            "superseded",
            _vector(1.0),
            superseded_by="canonical",
        )
        for reason in sorted(tpl.ACTIVE_READ_TOMBSTONE_REASONS):
            _insert_decision(
                database,
                f"tombstone-{reason}",
                _vector(1.0),
                superseded_by="canonical",
                revocation_reason=reason,
            )
        search = CommunityGrafxBoardVectorSearch(lambda _board_id: database)

        assert _board_ids(search, graph_layer="canonical") == [
            "canonical",
            "negative",
        ]
        assert _board_ids(search, graph_layer="working", threshold=1.0) == ["working"]
        assert _board_ids(search, graph_layer="all", threshold=1.0) == [
            "canonical",
            "legacy",
            "working",
        ]
        assert _board_ids(
            search,
            graph_layer="canonical",
            include_superseded=True,
            threshold=1.0,
        ) == ["canonical", "superseded"]
        assert "negative" in _board_ids(search, graph_layer="canonical", threshold=0.0)
        assert _board_ids(search, graph_layer="canonical", threshold=1e-12) == [
            "canonical"
        ]


def _upsert_board(database: Database, board_id: str) -> None:
    upsert_grafx_board_summary_vector(
        database,
        board_id=board_id,
        name=f"name-{board_id}",
        summary=f"summary-{board_id}",
        summary_embedding=_vector(1.0),
        decision_count=0,
        synced_at=_AT,
    )


def _upsert_digest(
    database: Database,
    *,
    board_id: str,
    digest_id: str,
    embedding: Sequence[float] | None = None,
    graph_layer: str = "canonical",
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
        embedding=list(embedding or _vector(1.0)),
        created_at=_AT,
    )
    with database.begin("write") as writer:
        writer.execute(
            "MATCH (b:Board {board_id: $board_id}), "
            "(d:DecisionDigest {id: $digest_id}) "
            "CREATE (b)-[:CONTAINS_DECISION]->(d)",
            {"board_id": board_id, "digest_id": digest_id},
        )


def _global_ids(
    database: Database,
    *,
    board_ids: tuple[str, ...],
    graph_layer: str = "all",
    exhaustive: bool = True,
    threshold: float = 0.0,
    top_k: int = 100,
) -> list[tuple[str, str]]:
    return [
        (str(hit["board_id"]), str(hit["digest_id"]))
        for hit in search_grafx_decision_digests(
            database,
            _vector(1.0),
            board_ids=board_ids,
            graph_layer=graph_layer,
            top_k=top_k,
            min_similarity=threshold,
            exhaustive=exhaustive,
        )
    ]


def test_v4_global_filters_acl_and_legacy_projection_are_exact(
    schema_templates: _SchemaTemplates,
    tmp_path: Path,
) -> None:
    path = _copy_template(schema_templates.global_, tmp_path / "v4-global")
    with okto_grafx.connect(path, vector_exact_scan_threshold=4096) as database:
        for board_id in ("board-a", "board-b", "board-denied"):
            _upsert_board(database, board_id)
        _upsert_digest(database, board_id="board-a", digest_id="b-tied")
        _upsert_digest(database, board_id="board-a", digest_id="a-tied")
        _upsert_digest(database, board_id="board-b", digest_id="other-board")
        _upsert_digest(database, board_id="board-denied", digest_id="denied")
        _upsert_digest(
            database,
            board_id="board-a",
            digest_id="working",
            graph_layer="working",
        )
        _upsert_digest(database, board_id="board-a", digest_id="legacy")
        _upsert_digest(database, board_id="board-a", digest_id="revoked")
        with database.begin("write") as writer:
            writer.execute(
                "MATCH (d:DecisionDigest {id: 'legacy'}) SET d.graph_layer = NULL"
            )
            writer.execute(
                "MATCH (d:DecisionDigest {id: 'revoked'}) "
                "SET d.source_revoked = true"
            )
            writer.execute(
                "CREATE (:DecisionDigest {id: 'nullable', board_id: 'board-a', "
                "original_node_id: 'node-nullable', title: 'nullable', "
                "one_line_summary: 'nullable', node_type: 'Decision', "
                "graph_layer: 'canonical', source_revoked: false})"
            )
            writer.execute(
                "MATCH (b:Board {board_id: 'board-a'}), "
                "(d:DecisionDigest {id: 'nullable'}) "
                "CREATE (b)-[:CONTAINS_DECISION]->(d)"
            )

        assert _global_ids(
            database,
            board_ids=("board-b", "board-a", "board-a"),
            graph_layer="canonical",
            threshold=1.0,
        ) == [
            ("board-a", "a-tied"),
            ("board-a", "b-tied"),
            ("board-b", "other-board"),
        ]
        assert _global_ids(
            database,
            board_ids=("board-a",),
            graph_layer="all",
            threshold=1.0,
        ) == [
            ("board-a", "a-tied"),
            ("board-a", "b-tied"),
            ("board-a", "legacy"),
            ("board-a", "working"),
        ]
        projected = search_grafx_decision_digests(
            database,
            _vector(1.0),
            board_ids=("board-a",),
            graph_layer="all",
            top_k=10,
            min_similarity=1.0,
            exhaustive=True,
        )
        assert (
            next(row for row in projected if row["digest_id"] == "legacy")[
                "graph_layer"
            ]
            == "legacy_unknown"
        )
        assert (
            search_grafx_decision_digests(
                database,
                _vector(1.0),
                board_ids=(),
                graph_layer="canonical",
                top_k=1,
                min_similarity=0.0,
                exhaustive=True,
            )
            == []
        )


@dataclass
class _RowsResult:
    rows: tuple[tuple[object, ...], ...]


class _CompleteExactReader:
    def __init__(self, rows: tuple[tuple[object, ...], ...]) -> None:
        self.rows = rows
        self.active = True
        self.statements: list[str] = []

    def execute(self, statement: str, _parameters: dict[str, object]) -> _RowsResult:
        self.statements.append(statement)
        return _RowsResult(self.rows)

    def rollback(self) -> None:
        self.active = False


class _CompleteExactDatabase:
    def __init__(self, reader: _CompleteExactReader) -> None:
        self.reader = reader

    def begin(self, mode: str) -> _CompleteExactReader:
        assert mode == "read"
        return self.reader


class _RecordingTransaction:
    """Transparent wrapper that records only public execute calls."""

    def __init__(self, transaction: Any, statements: list[str]) -> None:
        self._transaction = transaction
        self._statements = statements

    @property
    def active(self) -> bool:
        return bool(self._transaction.active)

    def execute(
        self, statement: str, parameters: dict[str, object] | None = None
    ) -> Any:
        self._statements.append(statement)
        return self._transaction.execute(statement, parameters or {})

    def rollback(self) -> None:
        self._transaction.rollback()

    def __enter__(self) -> _RecordingTransaction:
        self._transaction.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self._transaction.__exit__(exc_type, exc, traceback)


class _RecordingDatabase:
    """Database-shaped public seam used to prove an ANN call stayed bounded."""

    def __init__(self, database: Database, statements: list[str]) -> None:
        self._database = database
        self._statements = statements

    def begin(self, mode: str) -> _RecordingTransaction:
        return _RecordingTransaction(self._database.begin(mode), self._statements)


def test_v4_exhaustive_path_consumes_the_complete_result_without_a_500_cap() -> None:
    ordinary = tuple(
        (
            "board",
            f"digest-{ordinal:04d}",
            f"node-{ordinal:04d}",
            "title",
            "summary",
            "Decision",
            "canonical",
            _vector(0.0, 1.0),
        )
        for ordinal in range(501)
    )
    tail = (
        "board",
        "digest-tail-best",
        "node-tail-best",
        "title",
        "summary",
        "Decision",
        "canonical",
        _vector(1.0),
    )
    reader = _CompleteExactReader((*ordinary, tail))
    database = _CompleteExactDatabase(reader)

    result = search_grafx_decision_digests(
        database,  # type: ignore[arg-type]
        _vector(1.0),
        board_ids=("board",),
        graph_layer="canonical",
        top_k=1,
        min_similarity=0.0,
        exhaustive=True,
    )

    assert [(row["digest_id"], row["similarity"]) for row in result] == [
        ("digest-tail-best", 1.0)
    ]
    assert len(reader.statements) == 1
    assert "LIMIT" not in reader.statements[0].upper()


def _ann_vectors(count: int = 8) -> tuple[tuple[str, list[float]], ...]:
    return tuple(
        (f"eligible-{ordinal}", _vector(1.0, 0.08 * ordinal))
        for ordinal in range(count)
    )


def test_v5_board_and_global_ann_are_stable_cold_warm_reopen_and_eligible(
    schema_templates: _SchemaTemplates,
    tmp_path: Path,
) -> None:
    board_path = tmp_path / "v5-board"
    global_path = _copy_template(schema_templates.global_, tmp_path / "v5-global")
    board = okto_grafx.connect(board_path, vector_exact_scan_threshold=0)
    _create_decision_schema(board)
    for identity, embedding in _ann_vectors():
        _insert_decision(board, identity, embedding)
    _insert_decision(board, "working-ineligible", _vector(1.0), graph_layer="working")
    _insert_decision(
        board,
        "superseded-ineligible",
        _vector(1.0),
        superseded_by="eligible-0",
    )
    _insert_decision(board, "null-ineligible", None)

    global_db = okto_grafx.connect(global_path, vector_exact_scan_threshold=0)
    for board_id in ("allowed", "denied"):
        _upsert_board(global_db, board_id)
    for identity, embedding in _ann_vectors():
        _upsert_digest(
            global_db,
            board_id="allowed",
            digest_id=identity,
            embedding=embedding,
        )
    _upsert_digest(
        global_db,
        board_id="allowed",
        digest_id="working-ineligible",
        embedding=_vector(1.0),
        graph_layer="working",
    )
    _upsert_digest(
        global_db,
        board_id="denied",
        digest_id="acl-ineligible",
        embedding=_vector(1.0),
    )
    _upsert_digest(
        global_db,
        board_id="allowed",
        digest_id="revoked-ineligible",
        embedding=_vector(1.0),
    )
    with global_db.begin("write") as writer:
        writer.execute(
            "MATCH (d:DecisionDigest {id: 'revoked-ineligible'}) "
            "SET d.source_revoked = true"
        )

    board_statements: list[str] = []
    global_statements: list[str] = []
    board_search = CommunityGrafxBoardVectorSearch(
        lambda _board_id: _RecordingDatabase(board, board_statements)  # type: ignore[arg-type]
    )
    recorded_global = _RecordingDatabase(global_db, global_statements)
    board_cold = _board_ids(
        board_search,
        graph_layer="canonical",
        top_k=3,
    )
    board_warm = _board_ids(
        board_search,
        graph_layer="canonical",
        top_k=3,
    )
    global_cold = _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=3,
    )
    global_warm = _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=3,
    )
    board.close()
    global_db.close()

    with okto_grafx.connect(
        board_path, vector_exact_scan_threshold=0
    ) as reopened_board:
        reopened_board_statements: list[str] = []
        board_reopen = _board_ids(
            CommunityGrafxBoardVectorSearch(
                lambda _board_id: _RecordingDatabase(
                    reopened_board, reopened_board_statements
                )  # type: ignore[arg-type]
            ),
            graph_layer="canonical",
            top_k=3,
        )
    with okto_grafx.connect(
        global_path, vector_exact_scan_threshold=0
    ) as reopened_global:
        reopened_global_statements: list[str] = []
        global_reopen = _global_ids(
            _RecordingDatabase(
                reopened_global, reopened_global_statements
            ),  # type: ignore[arg-type]
            board_ids=("allowed",),
            graph_layer="canonical",
            exhaustive=False,
            top_k=3,
        )

    assert (
        board_cold
        == board_warm
        == board_reopen
        == [
            "eligible-0",
            "eligible-1",
            "eligible-2",
        ]
    )
    assert (
        global_cold
        == global_warm
        == global_reopen
        == [
            ("allowed", "eligible-0"),
            ("allowed", "eligible-1"),
            ("allowed", "eligible-2"),
        ]
    )
    assert all("ineligible" not in identity for identity in board_reopen)
    assert all("ineligible" not in identity for _board, identity in global_reopen)
    assert len(board_statements) == 2
    assert len(global_statements) == 2
    assert len(reopened_board_statements) == 1
    assert len(reopened_global_statements) == 1
    assert all(
        "similarity(" in statement
        for statement in (
            *board_statements,
            *global_statements,
            *reopened_board_statements,
            *reopened_global_statements,
        )
    )


def _set_decision(
    database: Database,
    *,
    embedding: Sequence[float] | None | object = ...,
    graph_layer: str | object = ...,
) -> None:
    assignments: list[str] = []
    parameters: dict[str, object] = {}
    if embedding is not ...:
        assignments.append("n.embedding = $embedding")
        parameters["embedding"] = (
            None
            if embedding is None
            else _stored_vector(
                database,
                "decision_embedding_idx",
                embedding,  # type: ignore[arg-type]
            )
        )
    if graph_layer is not ...:
        assignments.append("n.graph_layer = $graph_layer")
        parameters["graph_layer"] = graph_layer
    with database.begin("write") as writer:
        writer.execute(
            "MATCH (n:Decision {id: 'mutable'}) SET " + ", ".join(assignments),
            parameters,
        )


def _set_digest(
    database: Database,
    *,
    embedding: Sequence[float] | None | object = ...,
    graph_layer: str | object = ...,
    source_revoked: bool | object = ...,
) -> None:
    assignments: list[str] = []
    parameters: dict[str, object] = {}
    if embedding is not ...:
        assignments.append("d.embedding = $embedding")
        parameters["embedding"] = (
            None
            if embedding is None
            else _stored_vector(
                database,
                "digest_embedding_idx",
                embedding,  # type: ignore[arg-type]
            )
        )
    if graph_layer is not ...:
        assignments.append("d.graph_layer = $graph_layer")
        parameters["graph_layer"] = graph_layer
    if source_revoked is not ...:
        assignments.append("d.source_revoked = $source_revoked")
        parameters["source_revoked"] = source_revoked
    with database.begin("write") as writer:
        writer.execute(
            "MATCH (d:DecisionDigest {id: 'mutable'}) SET " + ", ".join(assignments),
            parameters,
        )


def test_v6_representative_ann_indexes_track_churn_and_reopen(
    schema_templates: _SchemaTemplates,
    tmp_path: Path,
) -> None:
    board_path = tmp_path / "v6-board"
    global_path = _copy_template(schema_templates.global_, tmp_path / "v6-global")
    board = okto_grafx.connect(board_path, vector_exact_scan_threshold=0)
    _create_decision_schema(board)
    for node_id, embedding in (
        ("stable-a", _vector(0.8, 0.6)),
        ("stable-b", _vector(0.6, 0.8)),
        ("stable-c", _vector(0.4, math.sqrt(0.84))),
    ):
        _insert_decision(board, node_id, embedding)

    global_db = okto_grafx.connect(global_path, vector_exact_scan_threshold=0)
    _upsert_board(global_db, "allowed")
    for digest_id, embedding in (
        ("stable-a", _vector(0.8, 0.6)),
        ("stable-b", _vector(0.6, 0.8)),
        ("stable-c", _vector(0.4, math.sqrt(0.84))),
    ):
        _upsert_digest(
            global_db,
            board_id="allowed",
            digest_id=digest_id,
            embedding=embedding,
        )

    board_statements: list[str] = []
    global_statements: list[str] = []
    board_search = CommunityGrafxBoardVectorSearch(
        lambda _board_id: _RecordingDatabase(board, board_statements)  # type: ignore[arg-type]
    )
    recorded_global = _RecordingDatabase(global_db, global_statements)

    assert _board_ids(board_search, graph_layer="canonical", top_k=1) == ["stable-a"]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "stable-a")]

    # Insert after both indexes have served a read, so this is an observable churn
    # transition rather than merely fixture construction.
    _insert_decision(board, "mutable", _vector(1.0))
    _upsert_digest(
        global_db,
        board_id="allowed",
        digest_id="mutable",
        embedding=_vector(1.0),
    )
    assert _board_ids(board_search, graph_layer="canonical", top_k=1) == ["mutable"]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "mutable")]

    _set_decision(board, embedding=_vector(0.0, 1.0))
    _set_digest(global_db, embedding=_vector(0.0, 1.0))
    assert _board_ids(board_search, graph_layer="canonical", top_k=1) == ["stable-a"]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "stable-a")]

    _set_decision(board, embedding=None)
    _set_digest(global_db, embedding=None)
    assert _board_ids(board_search, graph_layer="canonical", top_k=1) == ["stable-a"]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "stable-a")]

    _set_decision(board, embedding=_vector(1.0))
    _set_digest(global_db, embedding=_vector(1.0))
    assert _board_ids(board_search, graph_layer="canonical", top_k=1) == ["mutable"]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "mutable")]

    _set_decision(board, graph_layer="working")
    _set_digest(global_db, graph_layer="working")
    assert _board_ids(board_search, graph_layer="canonical", top_k=1) == ["stable-a"]
    assert _board_ids(board_search, graph_layer="all", top_k=1) == ["mutable"]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="canonical",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "stable-a")]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="all",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "mutable")]

    _set_digest(global_db, source_revoked=True)
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="all",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "stable-a")]

    # Restore eligibility before DELETE so the following result change proves
    # removal rather than merely repeating the revocation filter outcome.
    _set_digest(global_db, source_revoked=False)
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="all",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "mutable")]

    with board.begin("write") as writer:
        writer.execute("MATCH (n:Decision {id: 'mutable'}) DETACH DELETE n")
    with global_db.begin("write") as writer:
        writer.execute("MATCH (d:DecisionDigest {id: 'mutable'}) DETACH DELETE d")
    assert _board_ids(board_search, graph_layer="all", top_k=1) == ["stable-a"]
    assert _global_ids(
        recorded_global,  # type: ignore[arg-type]
        board_ids=("allowed",),
        graph_layer="all",
        exhaustive=False,
        top_k=1,
    ) == [("allowed", "stable-a")]
    board.close()
    global_db.close()

    with okto_grafx.connect(
        board_path, vector_exact_scan_threshold=0
    ) as reopened_board:
        reopened_board_statements: list[str] = []
        assert _board_ids(
            CommunityGrafxBoardVectorSearch(
                lambda _board_id: _RecordingDatabase(
                    reopened_board, reopened_board_statements
                )  # type: ignore[arg-type]
            ),
            graph_layer="all",
            top_k=1,
        ) == ["stable-a"]
    with okto_grafx.connect(
        global_path, vector_exact_scan_threshold=0
    ) as reopened_global:
        reopened_global_statements: list[str] = []
        assert _global_ids(
            _RecordingDatabase(
                reopened_global, reopened_global_statements
            ),  # type: ignore[arg-type]
            board_ids=("allowed",),
            graph_layer="all",
            exhaustive=False,
            top_k=1,
        ) == [("allowed", "stable-a")]

    # Every representative call remained bounded: no exact fallback hid index churn.
    assert len(board_statements) == 8
    assert len(global_statements) == 10
    assert len(reopened_board_statements) == 1
    assert len(reopened_global_statements) == 1
    assert all(
        "similarity(" in statement
        for statement in (
            *board_statements,
            *global_statements,
            *reopened_board_statements,
            *reopened_global_statements,
        )
    )


def test_v7_stale_refuses_failed_rebuild_never_certifies_and_reopen_is_ready(
    schema_templates: _SchemaTemplates,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_path = tmp_path / "v7-board"
    global_path = _copy_template(schema_templates.global_, tmp_path / "v7-global")
    board = okto_grafx.connect(board_path, vector_exact_scan_threshold=0)
    _create_decision_schema(board)
    _insert_decision(board, "decision", _vector(1.0))
    global_db = okto_grafx.connect(global_path, vector_exact_scan_threshold=0)
    _upsert_board(global_db, "allowed")
    _upsert_digest(
        global_db,
        board_id="allowed",
        digest_id="digest",
        embedding=_vector(1.0),
    )
    board_search = CommunityGrafxBoardVectorSearch(lambda _board_id: board)

    board_index = board.vectors.index("decision_embedding_idx").name
    global_index = global_db.vectors.index("digest_embedding_idx").name

    # Failure injection touches the public Transaction type, while stale creation and
    # every repair attempt go exclusively through the public maintenance facade.
    from okto_grafx import Transaction

    def refuse_commit(self: Transaction) -> None:
        raise GrafxIndexError(
            "M-PULSE-4 injected rebuild refusal",
            field="acceptance_rebuild_failure",
            index="acceptance-injected-index",
        )

    monkeypatch.setattr(Transaction, "commit", refuse_commit)
    for database, space in (
        (board, "decision_embedding_idx"),
        (global_db, "digest_embedding_idx"),
    ):
        with pytest.raises(GrafxIndexError) as failure:
            database.maintenance.rebuild_vector_index(space)
        assert failure.value.details["field"] == "acceptance_rebuild_failure"

    assert board.vectors.index("decision_embedding_idx").stale is True
    assert global_db.vectors.index("digest_embedding_idx").stale is True
    assert board_index in board.maintenance.status().stale_indexes
    assert global_index in global_db.maintenance.status().stale_indexes
    with pytest.raises(GraphIndexUnavailable):
        _board_ids(board_search, graph_layer="canonical", top_k=1)
    with pytest.raises(GraphIndexUnavailable):
        _global_ids(
            global_db,
            board_ids=("allowed",),
            graph_layer="canonical",
            exhaustive=False,
            top_k=1,
        )
    with pytest.raises(GraphIndexUnavailable):
        certify_grafx_global_vector_indexes(global_db)
    monkeypatch.undo()

    board_view = board.maintenance.rebuild_vector_index("decision_embedding_idx")
    global_view = global_db.maintenance.rebuild_vector_index("digest_embedding_idx")
    assert board_view.stale is global_view.stale is False
    assert board_view.stale_reason is global_view.stale_reason is None
    assert board_view.built_through_lsn is not None
    assert global_view.built_through_lsn is not None
    assert board_index not in board.maintenance.status().stale_indexes
    assert global_index not in global_db.maintenance.status().stale_indexes
    assert board.verify("all").findings == ()
    assert global_db.verify("all").findings == ()
    board.close()
    global_db.close()

    with okto_grafx.connect(
        board_path, vector_exact_scan_threshold=0
    ) as reopened_board:
        assert reopened_board.vectors.index("decision_embedding_idx").stale is False
        assert board_index not in reopened_board.maintenance.status().stale_indexes
        assert reopened_board.verify("all").findings == ()
        _assert_ready_index(
            reopened_board,
            table="Decision",
            column="embedding",
            space="decision_embedding_idx",
            allow_unloaded_positions=True,
        )
        assert _board_ids(
            CommunityGrafxBoardVectorSearch(lambda _board_id: reopened_board),
            graph_layer="canonical",
            top_k=1,
        ) == ["decision"]
    with okto_grafx.connect(
        global_path, vector_exact_scan_threshold=0
    ) as reopened_global:
        assert reopened_global.vectors.index("digest_embedding_idx").stale is False
        assert global_index not in reopened_global.maintenance.status().stale_indexes
        assert reopened_global.verify("all").findings == ()
        statuses = certify_grafx_global_vector_indexes(reopened_global)
        assert (
            next(
                status for status in statuses if status.space == "digest_embedding_idx"
            ).stale
            is False
        )
        assert _global_ids(
            reopened_global,
            board_ids=("allowed",),
            graph_layer="canonical",
            exhaustive=False,
            top_k=1,
        ) == [("allowed", "digest")]
