"""Community policy for Grafx ordered knowledge-graph pagination indexes."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import okto_grafx
import pytest
from okto_grafx import Timestamp
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

from okto_pulse.community.adapters.grafx_ordered_indexes import (
    ensure_pulse_grafx_ordered_page_indexes,
    pulse_ordered_page_index_name,
)
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    ensure_current_grafx_board_schema,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST,
)


def _single_node_manifest():
    manifest = PULSE_GRAFX_SCHEMA_MANIFEST
    return replace(manifest, nodes=(manifest.nodes[0],))


def _create_minimal_node_table(database, table_name: str) -> None:
    with database.begin("write") as schema:
        schema.execute(
            f"CREATE NODE TABLE {table_name}("
            "id STRING, created_at TIMESTAMP, PRIMARY KEY(id))"
        )


def test_ordered_page_index_activation_is_exact_and_idempotent(tmp_path: Path) -> None:
    manifest = _single_node_manifest()
    table = manifest.nodes[0]
    with okto_grafx.connect(tmp_path / "database") as database:
        _create_minimal_node_table(database, table.name)

        first = ensure_pulse_grafx_ordered_page_indexes(database, manifest=manifest)
        transactions = database.transactions
        wal = database.wal
        second = ensure_pulse_grafx_ordered_page_indexes(database, manifest=manifest)

        assert first.created == (pulse_ordered_page_index_name(table.name),)
        assert first.existing == ()
        assert second.created == ()
        assert second.existing == first.created
        assert database.transactions == transactions
        assert database.wal == wal


def test_conflicting_named_index_is_refused_without_replacement(tmp_path: Path) -> None:
    manifest = _single_node_manifest()
    table = manifest.nodes[0]
    name = pulse_ordered_page_index_name(table.name)
    with okto_grafx.connect(tmp_path / "database") as database:
        _create_minimal_node_table(database, table.name)
        incumbent = database.create_index(name, table.name, ("id",))
        transactions = database.transactions
        wal = database.wal

        with pytest.raises(GraphCapabilityUnavailable) as captured:
            ensure_pulse_grafx_ordered_page_indexes(database, manifest=manifest)

        assert captured.value.details["reason"] == "ordered_page_index_mismatch"
        assert database.indexes.index(name) == incumbent
        assert database.transactions == transactions
        assert database.wal == wal


def test_exact_pulse_page_uses_ordered_merge_and_matches_canonical_scan(
    tmp_path: Path,
) -> None:
    with okto_grafx.connect(tmp_path / "database") as database:
        ensure_current_grafx_board_schema(
            database,
            board_id="board-ordered-page",
            bootstrapped_at=Timestamp(micros=1),
        )
        with database.begin("write") as rows:
            rows.execute(
                "CREATE (:Decision {id: 'decision-1', created_at: $created_at, "
                "source_confidence: 1.0, relevance_score: 1.0, graph_layer: 'canonical'})",
                {"created_at": Timestamp(micros=3)},
            )
            rows.execute(
                "CREATE (:Criterion {id: 'criterion-1', created_at: $created_at, "
                "source_confidence: 1.0, relevance_score: 1.0, graph_layer: 'canonical'})",
                {"created_at": Timestamp(micros=2)},
            )
        parameters = {
            "min_confidence": 0.0,
            "min_relevance": 0.0,
            "graph_layer": "canonical",
            "include_code_traceability": True,
            "max_rows": 500,
        }
        canonical_text = tpl.GET_ALL_NODES.replace(
            "LIMIT $max_rows", "SKIP 0 LIMIT $max_rows"
        )

        plan = database.explain(tpl.GET_ALL_NODES)
        ordered = database.execute(tpl.GET_ALL_NODES, parameters)
        canonical = database.execute(canonical_text, parameters)

        merge = next(node for node in plan.walk() if node.label == "OrderedNodeMerge")
        assert "BoardMeta" not in merge.details()["tables"]
        assert ordered.rows == canonical.rows
        assert ordered.statistics["ordered_merge_tables"] == len(
            PULSE_GRAFX_SCHEMA_MANIFEST.nodes
        )
        assert ordered.statistics["rows_scanned"] < canonical.statistics["rows_scanned"]
