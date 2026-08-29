from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from okto_pulse.core.kg.interfaces.graph_transaction import (
    GraphNodePropertyBeforeImage,
)
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalFingerprintAccumulator,
    LogicalNode,
    LogicalRelation,
    LogicalTimestamp,
)
from okto_pulse.core.kg.logical_transfer.model import LogicalCounts

from okto_pulse.community.adapters.graph_ddl import (
    COMMON_NODE_COLUMNS,
    COMMON_REL_COLUMNS,
)
from okto_pulse.community.adapters.logical_transfer_schema import (
    board_logical_schema,
)

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
CORPUS = ROOT.parent / "okto_grafx" / "tests" / "corpus" / "pulse_query_corpus_1_0.json"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import mpulse7_acceptance_backends as backends


def _corpus_entry(entry_id: str) -> dict[str, Any]:
    document = json.loads(CORPUS.read_text(encoding="utf-8"))
    return next(entry for entry in document["entries"] if entry["id"] == entry_id)


def _corpus_entries() -> list[dict[str, Any]]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))["entries"]


class _Snapshot:
    def __init__(
        self,
        nodes: tuple[LogicalNode, ...],
        relations: tuple[LogicalRelation, ...],
    ) -> None:
        self._schema = board_logical_schema()
        self._nodes = nodes
        self._relations = relations
        accumulator = LogicalFingerprintAccumulator.for_schema(self._schema)
        for node in nodes:
            accumulator.add_node(node)
        for relation in relations:
            accumulator.add_relation(relation)
        self._counts = accumulator.counts()

    def schema(self) -> Any:
        return self._schema

    def counts(self) -> LogicalCounts:
        return self._counts

    def iter_nodes(self, *, batch_size: int):
        assert batch_size == 500
        yield self._nodes

    def iter_relations(self, *, batch_size: int):
        assert batch_size == 500
        yield self._relations


def _node(node_type: str, node_id: str) -> LogicalNode:
    properties: dict[str, Any] = {
        name: LOGICAL_NULL for name, _data_type in COMMON_NODE_COLUMNS
    }
    properties.update(
        {
            "id": node_id,
            "source_session_id": "baseline",
            "created_at": LogicalTimestamp(1_767_225_600_000_000),
            "title": f"title-{node_id}",
        }
    )
    return LogicalNode(node_type, node_id, properties)


def _relation(
    *,
    source_type: str,
    target_type: str,
    source_id: str,
    target_id: str,
    rule_id: str,
) -> LogicalRelation:
    properties: dict[str, Any] = {
        name: LOGICAL_NULL for name, _data_type in COMMON_REL_COLUMNS
    }
    properties.update(
        {
            "confidence": 1.0,
            "created_by_session_id": "baseline",
            "created_at": LogicalTimestamp(1_767_225_600_000_000),
            "layer": "deterministic",
            "rule_id": rule_id,
            "created_by": "gate-test",
            "fallback_reason": "",
        }
    )
    return LogicalRelation(
        "belongs_to",
        source_type,
        target_type,
        source_id,
        target_id,
        properties,
    )


def test_physical_snapshot_builds_both_named_digests_without_oracle_replay() -> None:
    nodes = (
        _node("Decision", "decision-1"),
        _node("Entity", "entity-1"),
        _node("Entity", "entity-2"),
    )
    relations = (
        _relation(
            source_type="Decision",
            target_type="Entity",
            source_id="decision-1",
            target_id="entity-1",
            rule_id="belongs_to/gate@v1",
        ),
        _relation(
            source_type="Entity",
            target_type="Entity",
            source_id="entity-1",
            target_id="entity-2",
            rule_id="belongs_to/spec_to_refinement@gate-v1",
        ),
    )

    observation = backends._observe_snapshot(_Snapshot(nodes, relations))

    assert observation.model_nodes == 3
    assert observation.model_edges == 1
    assert observation.model_lineage_edges == 1
    assert observation.logical_counts == LogicalCounts(
        nodes=3,
        relations=2,
        properties=(3 * len(COMMON_NODE_COLUMNS) + 2 * len(COMMON_REL_COLUMNS)),
        vectors=0,
    )
    assert len(observation.trace_model_sha256) == 64
    assert len(observation.logical_graph_sha256) == 64
    assert observation.trace_model_sha256 != observation.logical_graph_sha256


def test_physical_snapshot_refuses_two_exclusive_lineages_for_one_source() -> None:
    nodes = (_node("Entity", "source"), _node("Entity", "target"))
    relation = _relation(
        source_type="Entity",
        target_type="Entity",
        source_id="source",
        target_id="target",
        rule_id="belongs_to/spec_to_refinement@gate-v1",
    )

    with pytest.raises(
        backends.RealGateBackendError,
        match="physical_spec_lineage_duplicate:source",
    ):
        backends._observe_snapshot(_Snapshot(nodes, (relation, relation)))


def test_storage_root_hashes_untrusted_run_id_and_stays_backend_isolated(
    tmp_path: Path,
) -> None:
    ladybug = SimpleNamespace(
        backend="ladybug",
        board_id="board",
        workspace=str(tmp_path),
        run_id="../../same-run",
    )
    grafx = SimpleNamespace(**{**vars(ladybug), "backend": "grafx"})

    ladybug_root = backends._storage_root(ladybug)
    grafx_root = backends._storage_root(grafx)

    assert ladybug_root.parent.name == "l"
    assert grafx_root.parent.name == "g"
    assert ladybug_root.name == grafx_root.name
    assert ladybug_root.is_relative_to(tmp_path)
    assert grafx_root.is_relative_to(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "factory"),
    (("ladybug", backends.ladybug_factory), ("grafx", backends.grafx_factory)),
)
async def test_real_factory_bootstraps_and_reopens_the_productive_bundle(
    tmp_path: Path,
    backend: str,
    factory: Any,
) -> None:
    context = SimpleNamespace(
        backend=backend,
        board_id=f"m7-real-{backend}",
        workspace=str(tmp_path),
        run_id="real-factory-smoke",
    )
    expected_empty_model = backends._canonical_sha256(
        {"nodes": [], "edges": [], "lineage": []}
    )

    first = await factory(context)
    try:
        first_identity = first.identity()
        first_fingerprints = first.observe_fingerprints()
        assert set(first_identity) == {
            "backend",
            "backend_version",
            "generation",
            "storage_identity",
        }
        assert first_identity["backend"] == backend
        assert first_fingerprints["trace_model_sha256"] == expected_empty_model
        assert set(first_fingerprints) == {
            "logical_graph_sha256",
            "trace_model_sha256",
        }
        frozen_writes = [
            entry for entry in _corpus_entries() if entry["class"] == "write"
        ]
        assert {entry["id"] for entry in frozen_writes} == set(
            backends._WRITE_FAMILY_IDS
        )
        assert len(frozen_writes) == 21
        for entry in frozen_writes:
            family_id = entry["id"]
            receipt = await first.run_raw_execute_family(entry)
            assert receipt["id"] == family_id
            assert receipt["status"] == "passed"
            assert receipt["result"]["family"] == family_id
            assert receipt["result"]["postcondition"] in {
                "edge_absent",
                "edge_present",
                "node_absent",
                "property_change_durable",
            }
        expected_scenarios = {
            "restore-node-properties-local-before-image": (
                "exact_before_image_restored"
            ),
            "compensate-spec-lineage-local-receipt": (
                "old_parent_restored_new_parent_absent"
            ),
            "compensate-projection-local-receipt": (
                "projection_member_and_edge_restored"
            ),
            "session-delete-preserving-local-snapshots": (
                "exact_lineage_preserved_generic_removed"
            ),
        }
        for scenario_id, postcondition in expected_scenarios.items():
            receipt = await first.run_receipt_bound_scenario(scenario_id)
            assert receipt["id"] == scenario_id
            assert receipt["status"] == "passed"
            assert receipt["result"]["postcondition"] == postcondition
        for entry in _corpus_entries():
            receipt = await first.run_pulse_corpus_case(entry)
            assert receipt["id"] == entry["id"]
            assert receipt["class"] == entry["class"]
            assert receipt["classification"] == entry["classification"]
            if entry["class"] == "fragment":
                assert receipt["status"] == "not_executable"
                assert receipt["result"] == {"outcome": "fragment"}
            else:
                assert receipt["status"] == "executed"
                allowed = {entry["expected"]["kind"]}
                if entry["classification"] == "generic_gap":
                    allowed.add("error")
                assert receipt["result"]["outcome"] in allowed
        assert first.observe_fingerprints() == first_fingerprints

        recovery = await first.reopen_recover_verify_fingerprint(
            after_operations=1,
            verify_scope="all",
        )
        assert set(recovery) == {
            "after_operations",
            "closed",
            "fingerprint_logical_graph_sha256",
            "fingerprint_trace_model_sha256",
            "generation",
            "recovered",
            "reopened",
            "storage_identity",
            "verify_ok",
            "verify_scope",
        }
        assert recovery["fingerprint_trace_model_sha256"] == expected_empty_model
        assert (
            recovery["fingerprint_logical_graph_sha256"]
            == (first_fingerprints["logical_graph_sha256"])
        )
    finally:
        await first.close()

    reopened = await factory(context)
    try:
        second_identity = reopened.identity()
        second_fingerprints = reopened.observe_fingerprints()
        assert second_identity["generation"] == first_identity["generation"]
        assert second_identity["storage_identity"] == first_identity["storage_identity"]
        assert second_fingerprints == first_fingerprints
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_real_factories_freeze_bilateral_board_meta_fingerprint(
    tmp_path: Path,
) -> None:
    observed: dict[str, dict[str, str]] = {}
    for backend, factory in (
        ("ladybug", backends.ladybug_factory),
        ("grafx", backends.grafx_factory),
    ):
        context = SimpleNamespace(
            backend=backend,
            board_id="m7-bilateral-bootstrap",
            workspace=str(tmp_path),
            run_id="bilateral-bootstrap-fingerprint",
        )
        instance = await factory(context)
        try:
            observed[backend] = instance.observe_fingerprints()
        finally:
            await instance.close()

    assert observed["ladybug"]["trace_model_sha256"] == observed["grafx"][
        "trace_model_sha256"
    ]
    assert observed["ladybug"]["logical_graph_sha256"] == observed["grafx"][
        "logical_graph_sha256"
    ]


def test_corpus_renderer_closes_structural_and_parameter_inputs() -> None:
    entry = {
        "template": (
            "MATCH (a:<<from_type>>)-[:<<rel_name>>]->(b:<<to_type>>) "
            "WHERE a.id = $src AND b.id = $tgt RETURN a.id"
        ),
        "placeholders": ["from_type", "rel_name", "to_type"],
    }

    statement = backends._render_statement(entry)

    assert statement == (
        "MATCH (a:Decision)-[:belongs_to]->(b:Entity) "
        "WHERE a.id = $src AND b.id = $tgt RETURN a.id"
    )
    assert backends._statement_params(statement) == {
        "src": "gate-node",
        "tgt": "gate-target",
    }

    assert "(n:Alternative" in backends._render_statement(_corpus_entry("I12"))
    assert "(m:Decision)-[r:belongs_to]->(n:Entity)" in (
        backends._render_statement(_corpus_entry("I46"))
    )
    assert "(n:Decision)-[r:belongs_to]->(m:Entity)" in (
        backends._render_statement(_corpus_entry("I48"))
    )


def test_raw_family_is_loaded_from_the_frozen_corpus() -> None:
    entry = _corpus_entry("I42")

    assert entry["id"] == "I42"
    assert entry["class"] == "write"
    assert "COALESCE(n.query_hits, 0) + $delta" in entry["template"]


def test_all_executable_frozen_corpus_entries_have_closed_inputs() -> None:
    document = json.loads(CORPUS.read_text(encoding="utf-8"))
    executable = [
        entry for entry in document["entries"] if entry["class"] != "fragment"
    ]

    rendered = []
    for entry in executable:
        statement = backends._render_statement(entry)
        params = backends._statement_params(statement)
        assert set(params) == set(
            re.findall(
                r"\$([A-Za-z_][A-Za-z0-9_]*)",
                statement,
            )
        )
        rendered.append(entry["id"])

    assert len(rendered) == 96


def test_corpus_temporal_parameters_match_the_productive_column_contract() -> None:
    def params(entry_id: str) -> dict[str, Any]:
        return backends._statement_params(
            backends._render_statement(_corpus_entry(entry_id))
        )

    assert params("I06")["now"] == backends._FIXED_MOMENT
    assert type(params("I14")["cutoff"]) is str
    assert params("I42")["ts"] == backends._FIXED_INSTANT
    assert type(params("I65")["now"]) is str
    assert params("I67")["rows"][0]["now"] == backends._FIXED_INSTANT
    assert params("I68")["rows"][0]["now"] == backends._FIXED_INSTANT
    assert (
        type(
            params("public:core:cypher_templates.GET_ALL_NODES_AFTER_CURSOR")[
                "cursor_ts"
            ]
        ).__name__
        == "datetime"
    )


def test_generic_gap_error_must_be_authenticated_against_frozen_contract() -> None:
    entry = {"expected": {"error": {"code": "parse_error", "type": "GrafxParseError"}}}
    failure = RuntimeError("mapped graph error")
    failure.details = {  # type: ignore[attr-defined]
        "backend_error_code": "parse_error",
        "backend_error_type": "GrafxParseError",
    }

    assert backends._authenticated_generic_error(entry, failure) == (
        "parse_error",
        "GrafxParseError",
    )

    failure.details["backend_error_code"] = "different"  # type: ignore[attr-defined]
    with pytest.raises(
        backends.RealGateBackendError,
        match="generic_gap_error_did_not_match_frozen_backend_contract",
    ):
        backends._authenticated_generic_error(entry, failure)


def test_before_image_evidence_canonicalizes_backend_timestamp_shapes() -> None:
    ladybug = GraphNodePropertyBeforeImage(
        node_type="Decision",
        node_id="gate-node",
        attrs={"superseded_at": backends._FIXED_MOMENT},
    )
    grafx = GraphNodePropertyBeforeImage(
        node_type="Decision",
        node_id="gate-node",
        attrs={"superseded_at": "2026-01-01T00:00:00.000000Z"},
    )

    assert backends._before_image_document(ladybug) == (
        backends._before_image_document(grafx)
    )
