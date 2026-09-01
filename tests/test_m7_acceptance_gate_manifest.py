from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from mpulse7_gate_support import (
    DeterministicGraphModel,
    board_result_supplement_sha256,
    crash_points_sha256,
    evaluate_trace,
    expand_trace,
    expanded_trace_sha256,
    load_gate_manifest,
)
from okto_pulse.core.kg.interfaces.graph_store import SemanticGraphStore
from okto_pulse.core.kg.interfaces.graph_transaction import (
    SOURCE_PROJECTION_REMOVED_REASON,
    GraphTransactionScope,
    ProjectionActiveSetIntent,
)
from okto_pulse.core.kg.relational_projection import (
    is_relational_projection_node,
    parse_relational_projection_ref,
    relational_projection_belongs_to_rule,
)
from okto_pulse.core.kg.schema_contract import NODE_TYPES

from okto_pulse.community.adapters.grafx_graph_store import (
    CommunityGrafxGraphStore,
)
from okto_pulse.community.adapters.grafx_graph_transaction import (
    _GrafxTransactionScope,
)
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)
from okto_pulse.community.adapters.graph_ddl import (
    COMMON_NODE_COLUMNS,
    COMMON_REL_COLUMNS,
)
from okto_pulse.community.adapters.kuzu_graph_store import (
    CommunityKuzuGraphStore,
)
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    _KuzuTransactionScope,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "m_pulse_7_acceptance_gate_v1.json"
EXPECTED_NODE_TYPES_SHA256 = (
    "de3ee8aec849842834e1f3b102ccda01920fa450f57bf3a0702b876567779d9b"
)
EXPECTED_LAYOUTS_SHA256 = (
    "aceb580af84b5602f1c88ca2585614a18993736f8ba931df5bfab1fbb66e5f20"
)
EXPECTED_TRACE_SHA256 = (
    "243d25a4fc807b5b29b63a64c597acf56d1dc94ca026030077178ba6abd86bea"
)
EXPECTED_SUPPLEMENT_SHA256 = (
    "f88cebf59149f516f110bae51d26377ac0574cf967bc816173faa1ce821a9369"
)
EXPECTED_CRASH_POINTS_SHA256 = (
    "b47c850dac2c876d2486ba4ab1ecbd4186c880cb2e1d96965ad1d8709ed5127b"
)
EXPECTED_PULSE_CORPUS_DIGEST = (
    "b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc"
)
EXPECTED_PULSE_CORPUS_FILE_SHA256 = (
    "0997747ed8bb9172d05781a62e5f81e7694630b173aaa152ac9ea28daec9d13f"
)
EXPECTED_DISTRIBUTION = {
    "create_node": 2_000,
    "create_edge": 2_200,
    "update_node": 1_500,
    "replace_node_payload": 700,
    "mark_superseded": 500,
    "increment_attestation": 700,
    "replace_with_source_deleted_tombstone": 300,
    "reconcile_spec_lineage_parent": 500,
    "clear_spec_lineage_parent": 300,
    "reconcile_projection_active_set": 500,
    "delete_edges_by_session": 400,
    "delete_nodes_by_session": 400,
}
EXPECTED_RAW_WRITE_IDS = (
    "I05",
    "I06",
    "I07",
    "I10",
    "I13",
    "I21",
    "I22",
    "I23",
    "I26",
    "I27",
    "I29",
    "I30",
    "I37",
    "I38",
    "I40",
    "I41",
    "I42",
    "I65",
    "I66",
    "I67",
    "I68",
)
EXPECTED_CHECKPOINTS: tuple[dict[str, Any], ...] = (
    {
        "after_operations": 2500,
        "model_fingerprint_sha256": (
            "33d307d0f1d8f9c55f3eaef5e8253f0f9c5eb9e39687f1c5198106c4b377a4eb"
        ),
        "census": {
            "nodes": 932,
            "edges": 99,
            "lineage_edges": 12,
            "projection_removed_nodes": 128,
        },
    },
    {
        "after_operations": 5000,
        "model_fingerprint_sha256": (
            "5a93b77ac50e109cad88e5874b12d315b7dd460b92405a73c6335c25a2a90fa5"
        ),
        "census": {
            "nodes": 946,
            "edges": 114,
            "lineage_edges": 11,
            "projection_removed_nodes": 128,
        },
    },
    {
        "after_operations": 7500,
        "model_fingerprint_sha256": (
            "c785cc1739169a3e2ca509bd8e22fe40d004dc1e048b6f43c5f78b7876915580"
        ),
        "census": {
            "nodes": 908,
            "edges": 106,
            "lineage_edges": 10,
            "projection_removed_nodes": 128,
        },
    },
    {
        "after_operations": 10000,
        "model_fingerprint_sha256": (
            "e6b7f3abafdff55f8e4167d012083eddf2106f6ec9de7347bccd5d7e41097344"
        ),
        "census": {
            "nodes": 926,
            "edges": 70,
            "lineage_edges": 10,
            "projection_removed_nodes": 128,
        },
    },
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _public_callables(protocol: type) -> set[str]:
    return {
        name
        for name, value in protocol.__dict__.items()
        if not name.startswith("_") and callable(value)
    }


def _independent_replay(
    operations: tuple[dict[str, Any], ...],
    boundaries: frozenset[int],
    recovery_boundaries: frozenset[int],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Second checkpoint oracle; deliberately does not use the support model."""

    node_attrs = tuple(
        name
        for name, _kind in COMMON_NODE_COLUMNS
        if name not in {"id", "source_session_id"}
    )
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    lineage: dict[str, dict[str, Any]] = {}
    checkpoints: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []

    def edge_key(payload: dict[str, Any]) -> str:
        return "|".join(
            (
                str(payload["edge_type"]),
                str(payload["from_type"]),
                str(payload["to_type"]),
                str(payload["from_id"]),
                str(payload["to_id"]),
                str(payload["attrs"].get("rule_id", "")),
            )
        )

    def state() -> dict[str, Any]:
        return {
            "nodes": [nodes[key] for key in sorted(nodes)],
            "edges": [edges[key] for key in sorted(edges)],
            "lineage": [lineage[key] for key in sorted(lineage)],
        }

    def census() -> dict[str, int]:
        return {
            "nodes": len(nodes),
            "edges": len(edges) + len(lineage),
            "lineage_edges": len(lineage),
            "projection_removed_nodes": sum(
                1
                for node in nodes.values()
                if node["attrs"].get("revocation_reason")
                == SOURCE_PROJECTION_REMOVED_REASON
            ),
        }

    for operation in operations:
        family = operation["family"]
        payload = deepcopy(operation["payload"])
        if family == "create_node":
            attrs = dict.fromkeys(node_attrs)
            attrs.update(payload["attrs"])
            session_id = payload.get(
                "source_session_id", attrs.pop("source_session_id", None)
            )
            nodes[payload["node_id"]] = {
                "node_id": payload["node_id"],
                "node_type": payload["node_type"],
                "source_session_id": session_id,
                "attrs": attrs,
            }
        elif family == "create_edge":
            key = edge_key(payload)
            edges[key] = {"edge_key": key, **payload}
        elif family == "update_node":
            nodes[payload["node_id"]]["attrs"].update(payload["attrs"])
        elif family == "replace_node_payload":
            attrs = dict.fromkeys(node_attrs)
            attrs.update(payload["attrs"])
            nodes[payload["node_id"]]["attrs"] = attrs
            nodes[payload["node_id"]]["source_session_id"] = payload[
                "source_session_id"
            ]
        elif family == "mark_superseded":
            nodes[payload["node_id"]]["attrs"].update(
                {
                    "superseded_by": payload["superseded_by"],
                    "superseded_at": payload["superseded_at"],
                    "revocation_reason": payload["revocation_reason"],
                }
            )
        elif family == "increment_attestation":
            attrs = nodes[payload["node_id"]]["attrs"]
            attrs["attestation_count"] = int(attrs.get("attestation_count") or 0) + 1
            attrs["last_attested_at"] = payload["attested_at"]
        elif family == "replace_with_source_deleted_tombstone":
            node_id = payload["node_id"]
            before = nodes[node_id]["attrs"]
            attrs = dict.fromkeys(node_attrs)
            attrs.update(
                {
                    "title": "",
                    "content": "",
                    "context": "",
                    "justification": "",
                    "source_artifact_ref": before.get("source_artifact_ref") or "",
                    "graph_layer": payload["graph_layer"],
                    "maturity_status": payload["maturity_status"],
                    "created_by_agent": before.get("created_by_agent")
                    or "system:source-deletion",
                    "source_confidence": 0.0,
                    "relevance_score": payload["relevance_score"],
                    "query_hits": 0,
                    "priority_boost": 0.0,
                    "revocation_reason": payload["revocation_reason"],
                    "human_curated": False,
                    "generation": int(before.get("generation") or 0),
                    "source_span_quote": "",
                }
            )
            if before.get("created_at") is not None:
                attrs["created_at"] = before["created_at"]
            nodes[node_id]["attrs"] = attrs
            edges = {
                key: edge
                for key, edge in edges.items()
                if node_id not in {edge["from_id"], edge["to_id"]}
            }
        elif family == "reconcile_spec_lineage_parent":
            existing = lineage.get(payload["source_id"])
            if not (
                existing is not None
                and existing["target_id"] == payload["target_id"]
                and existing["attrs"].get("rule_id") == payload["attrs"].get("rule_id")
            ):
                lineage[payload["source_id"]] = payload
        elif family == "clear_spec_lineage_parent":
            lineage.pop(payload["source_id"], None)
        elif family == "reconcile_projection_active_set":
            owner_slot = int(payload["owner_id"].rsplit("-", maxsplit=1)[1])
            removed = {
                f"trace-node-{NODE_TYPES.index(kind) + len(NODE_TYPES) * owner_slot:05d}"
                for kind in ("Decision", "Alternative")
            }
            for node_id in removed:
                attrs = nodes[node_id]["attrs"]
                if attrs.get("revocation_reason", "") in {
                    "",
                    SOURCE_PROJECTION_REMOVED_REASON,
                }:
                    attrs["revocation_reason"] = SOURCE_PROJECTION_REMOVED_REASON
            edges = {
                key: edge
                for key, edge in edges.items()
                if edge["from_id"] not in removed and edge["to_id"] not in removed
            }
        elif family == "delete_edges_by_session":
            session_id = payload["session_id"]
            edges = {
                key: edge
                for key, edge in edges.items()
                if edge["attrs"].get("created_by_session_id") != session_id
            }
        elif family == "delete_nodes_by_session":
            session_id = payload["session_id"]
            allowed = frozenset(payload.get("node_types", NODE_TYPES))
            removed = {
                node_id
                for node_id, node in nodes.items()
                if node["source_session_id"] == session_id
                and node["node_type"] in allowed
            }
            nodes = {
                node_id: node
                for node_id, node in nodes.items()
                if node_id not in removed
            }
            edges = {
                key: edge
                for key, edge in edges.items()
                if edge["from_id"] not in removed and edge["to_id"] not in removed
            }
            lineage = {
                source_id: item
                for source_id, item in lineage.items()
                if source_id not in removed and item.get("target_id") not in removed
            }
        else:
            raise AssertionError(f"unhandled independent family {family!r}")

        sequence = operation["sequence"]
        if sequence in boundaries:
            checkpoints.append(
                {
                    "after_operations": sequence,
                    "model_fingerprint_sha256": _sha256(state()),
                    "census": census(),
                }
            )
        if sequence in recovery_boundaries:
            before = _sha256(state())
            recovered = json.loads(_canonical_bytes(state()))
            after = _sha256(recovered)
            assert before == after
            recoveries.append(
                {
                    "after_operations": sequence,
                    "before_fingerprint_sha256": before,
                    "after_fingerprint_sha256": after,
                }
            )

    return tuple(checkpoints), tuple(recoveries)


def _assert_signature_accepts(operation: dict[str, Any]) -> None:
    method = operation["method"]
    payload = operation["payload"]
    method_name = method.rsplit(".", maxsplit=1)[1]
    if method.startswith("SemanticGraphStore."):
        for implementation in (CommunityKuzuGraphStore, CommunityGrafxGraphStore):
            inspect.signature(getattr(implementation, method_name)).bind(
                None, "trace-board", **payload
            )
        return
    if method == "GraphTransactionScope.reconcile_projection_active_set":
        intent = ProjectionActiveSetIntent(
            owner_type=payload["owner_type"],
            owner_id=payload["owner_id"],
            namespace=payload["namespace"],
            owner_node_id=payload["owner_node_id"],
            active_nodes=(),
            active_edges=(),
        )
        for implementation in (_KuzuTransactionScope, _GrafxTransactionScope):
            inspect.signature(getattr(implementation, method_name)).bind(None, intent)
        return
    for implementation in (_KuzuTransactionScope, _GrafxTransactionScope):
        inspect.signature(getattr(implementation, method_name)).bind(None, **payload)


def test_trace_is_exactly_frozen_and_schema_authoritative() -> None:
    manifest = load_gate_manifest(MANIFEST_PATH)
    trace = manifest["trace"]
    operations = expand_trace(manifest)
    distribution = {
        item["family"]: item["count"] for item in trace["family_distribution"]
    }

    assert manifest["manifest_format"].endswith("/1")
    assert manifest["scope"]["source_revisions"] == {
        "community": "050ced9b79533d50efed453d53ed450984f75cf3",
        "core": "ccc1f345ece1db89a274cfdd634bd4da27028f63",
        "okto_grafx_corpus": "a1c3c496fe21d8e9f86953ca3932aced5917fb22",
    }
    assert trace["seed"] == "0x6F6B746F4D503731"
    assert trace["operation_count"] == 10_000
    assert (
        trace["bootstrap_create_nodes"],
        trace["projection_owner_slots"],
        trace["bootstrap_projection_edges"],
    ) == (880, 64, 128)
    assert distribution == EXPECTED_DISTRIBUTION
    assert len(operations) == 10_000
    assert Counter(item["family"] for item in operations) == EXPECTED_DISTRIBUTION
    assert [item["sequence"] for item in operations] == list(range(1, 10_001))
    assert operations[0]["operation_id"] == "m-pulse-7-00001"
    assert operations[-1]["operation_id"] == "m-pulse-7-10000"
    method_counts = Counter(item["method"] for item in operations)
    assert set(method_counts) == {
        method
        for family in trace["family_distribution"]
        for method in family["methods"]
    }
    for family in trace["family_distribution"]:
        quotient, remainder = divmod(family["count"], len(family["methods"]))
        for index, method in enumerate(family["methods"]):
            assert method_counts[method] == quotient + (index < remainder)
    bootstrap_nodes = trace["bootstrap_create_nodes"]
    bootstrap_edges = trace["bootstrap_projection_edges"]
    assert all(item["family"] == "create_node" for item in operations[:bootstrap_nodes])
    assert all(
        item["family"] == "create_edge"
        for item in operations[bootstrap_nodes : bootstrap_nodes + bootstrap_edges]
    )

    layouts = [
        [entry.logical_type, entry.from_type, entry.to_type]
        for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    ]
    authority = trace["schema_authority"]
    assert len(NODE_TYPES) == authority["node_type_count"] == 11
    assert len(layouts) == authority["relationship_layout_count"] == 69
    assert len({entry[0] for entry in layouts}) == 16
    assert _sha256(list(NODE_TYPES)) == EXPECTED_NODE_TYPES_SHA256
    assert _sha256(layouts) == EXPECTED_LAYOUTS_SHA256
    assert authority["node_types_sha256"] == EXPECTED_NODE_TYPES_SHA256
    assert authority["relationship_layouts_sha256"] == EXPECTED_LAYOUTS_SHA256

    independent = hashlib.sha256()
    for operation in operations:
        independent.update(_canonical_bytes(operation))
        independent.update(b"\n")
    assert independent.hexdigest() == EXPECTED_TRACE_SHA256
    assert expanded_trace_sha256(operations) == EXPECTED_TRACE_SHA256
    assert trace["expanded_trace_sha256"] == EXPECTED_TRACE_SHA256
    assert MANIFEST_PATH.stat().st_size < 64 * 1024


def test_every_trace_operation_binds_to_both_real_adapters() -> None:
    manifest = load_gate_manifest(MANIFEST_PATH)
    operations = expand_trace(manifest)
    node_properties = {name for name, _kind in COMMON_NODE_COLUMNS}
    relationship_properties = {name for name, _kind in COMMON_REL_COLUMNS}
    layouts = {
        (entry.logical_type, entry.from_type, entry.to_type)
        for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    }
    created_nodes = {
        operation["payload"]["node_id"]: operation["payload"]
        for operation in operations
        if operation["family"] == "create_node"
    }
    created_edges = [
        operation["payload"]
        for operation in operations
        if operation["family"] == "create_edge"
    ]

    for owner_slot in range(manifest["trace"]["projection_owner_slots"]):
        owner_id = f"trace-owner-{owner_slot:02d}"
        owner_node_id = f"trace-node-{NODE_TYPES.index('Entity') + len(NODE_TYPES) * owner_slot:05d}"
        assert created_nodes[owner_node_id]["attrs"]["source_artifact_ref"] == (
            f"refinement:{owner_id}"
        )
        for kind in ("Decision", "Alternative"):
            node_id = f"trace-node-{NODE_TYPES.index(kind) + len(NODE_TYPES) * owner_slot:05d}"
            node = created_nodes[node_id]
            source_ref = node["attrs"]["source_artifact_ref"]
            identity = parse_relational_projection_ref(source_ref)
            assert identity is not None
            assert identity.owner_id == owner_id
            assert identity.node_type == kind
            assert is_relational_projection_node(
                node_type=kind,
                source_artifact_ref=source_ref,
                created_by_agent=node["attrs"]["created_by_agent"],
                owner_type="refinement",
                owner_id=owner_id,
                namespace="rdl",
            )
            assert any(
                edge["edge_type"] == "belongs_to"
                and edge["from_type"] == kind
                and edge["to_type"] == "Entity"
                and edge["from_id"] == node_id
                and edge["to_id"] == owner_node_id
                and edge["attrs"]["rule_id"]
                == relational_projection_belongs_to_rule(kind)
                for edge in created_edges
            )

    for operation in operations:
        _assert_signature_accepts(operation)
        family = operation["family"]
        payload = operation["payload"]
        if "node_type" in payload:
            assert payload["node_type"] in NODE_TYPES
        if family == "create_node":
            assert set(payload["attrs"]) <= node_properties - {"id"}
        elif family in {"update_node", "replace_node_payload"}:
            assert set(payload["attrs"]) <= node_properties - {
                "id",
                "source_session_id",
            }
        elif family == "create_edge":
            assert (
                payload["edge_type"],
                payload["from_type"],
                payload["to_type"],
            ) in layouts
            assert set(payload["attrs"]) <= relationship_properties
        elif family == "reconcile_spec_lineage_parent":
            assert ("belongs_to", "Entity", "Entity") in layouts
            assert payload["attrs"]["rule_id"].startswith(
                "belongs_to/spec_to_refinement@"
            )
            assert set(payload["attrs"]) <= relationship_properties
        elif family == "reconcile_projection_active_set":
            assert payload["owner_type"] == "refinement"
            assert payload["namespace"] == "rdl"
            assert payload["active_nodes"] == []
            assert payload["active_edges"] == []
        elif family == "delete_nodes_by_session" and "node_types" in payload:
            assert tuple(payload["node_types"]) == NODE_TYPES


def test_checkpoints_and_three_recovery_cycles_have_independent_goldens() -> None:
    manifest = load_gate_manifest(MANIFEST_PATH)
    operations = expand_trace(manifest)
    boundaries = frozenset(
        item["after_operations"] for item in manifest["trace"]["checkpoints"]
    )
    recovery_boundaries = frozenset(
        manifest["reopen_recovery_cycles"]["after_operations"]
    )
    checkpoints, recoveries = _independent_replay(
        operations, boundaries, recovery_boundaries
    )
    evaluation = evaluate_trace(manifest)

    assert checkpoints == EXPECTED_CHECKPOINTS
    assert checkpoints == tuple(manifest["trace"]["checkpoints"])
    assert evaluation.checkpoints == EXPECTED_CHECKPOINTS
    assert (
        evaluation.final_fingerprint_sha256
        == EXPECTED_CHECKPOINTS[-1]["model_fingerprint_sha256"]
    )
    assert (
        manifest["trace"]["final_model_fingerprint_sha256"]
        == (EXPECTED_CHECKPOINTS[-1]["model_fingerprint_sha256"])
    )
    assert manifest["reopen_recovery_cycles"] == {
        "count": 3,
        "after_operations": [2500, 5000, 7500],
        "required_steps": [
            "close_active_and_shadow_handles",
            "reopen_from_persisted_binding",
            "run_backend_recovery",
            "verify_all",
            "compare_logical_fingerprint",
        ],
    }
    assert tuple(evaluation.recovery_cycles) == recoveries
    assert len(recoveries) == 3


def test_lineage_retry_oracles_preserve_the_first_edge_attributes() -> None:
    first = {
        "family": "reconcile_spec_lineage_parent",
        "sequence": 1,
        "payload": {
            "source_id": "spec",
            "target_id": "parent",
            "attrs": {
                "created_at": "2026-01-01T00:00:01Z",
                "rule_id": "belongs_to/spec_to_refinement@trace-v1",
            },
        },
    }
    retry = deepcopy(first)
    retry["sequence"] = 2
    retry["payload"]["attrs"]["created_at"] = "2026-01-01T00:00:02Z"
    operations = (first, retry)

    model = DeterministicGraphModel()
    for operation in operations:
        model.apply(operation)
    checkpoints, _recoveries = _independent_replay(
        operations,
        frozenset({2}),
        frozenset(),
    )

    lineage = model.export_state()["lineage"]
    assert lineage == [first["payload"]]
    assert checkpoints[0]["model_fingerprint_sha256"] == model.fingerprint_sha256()


def test_mutation_inventory_is_closed_and_exclusions_are_scenario_bound() -> None:
    manifest = load_gate_manifest(MANIFEST_PATH)
    inventories = {
        item["protocol"]: item for item in manifest["mutation_surface_inventory"]
    }
    for protocol, protocol_type in (
        ("SemanticGraphStore", SemanticGraphStore),
        ("GraphTransactionScope", GraphTransactionScope),
    ):
        inventory = inventories[protocol]
        classified = (
            set(inventory["included_methods"])
            | set(inventory["non_mutating_methods"])
            | set(inventory["control_methods"])
            | {item["method"] for item in inventory["excluded_mutations"]}
        )
        assert classified == _public_callables(protocol_type)
        for exclusion in inventory["excluded_mutations"]:
            assert exclusion["reason_code"]
            assert exclusion["justification"]

    extension = manifest["backend_neutral_extension_mutations"]
    assert [item["method"] for item in extension] == [
        "replace_with_source_deleted_tombstone"
    ]
    kuzu_signature = inspect.signature(
        _KuzuTransactionScope.replace_with_source_deleted_tombstone
    )
    grafx_signature = inspect.signature(
        _GrafxTransactionScope.replace_with_source_deleted_tombstone
    )
    assert tuple(kuzu_signature.parameters) == tuple(grafx_signature.parameters)

    scenarios = manifest["receipt_bound_scenarios"]
    assert {item["method"] for item in scenarios} == {
        "restore_node_properties",
        "compensate_spec_lineage_parent",
        "compensate_projection_active_set",
        "delete_edges_by_session_preserving_spec_lineage",
    }
    assert all(
        item["input_authority"].startswith("same-backend-") for item in scenarios
    )

    supplement = manifest["raw_execute_supplement"]
    assert tuple(supplement["family_ids"]) == EXPECTED_RAW_WRITE_IDS
    assert supplement["family_count"] == len(EXPECTED_RAW_WRITE_IDS) == 21
    assert supplement["corpus_digest"] == EXPECTED_PULSE_CORPUS_DIGEST


def test_crash_points_corpus_and_bilateral_benchmark_are_frozen() -> None:
    manifest = load_gate_manifest(MANIFEST_PATH)
    crash = manifest["crash_points"]
    points = crash["points"]
    assert len(points) == 11
    assert len({point["id"] for point in points}) == len(points)
    assert all(1 <= point["after_operation"] <= 10_000 for point in points)
    assert all(point["hook"] and point["expected_recovery"] for point in points)
    assert crash_points_sha256(manifest) == EXPECTED_CRASH_POINTS_SHA256
    assert crash["points_sha256"] == EXPECTED_CRASH_POINTS_SHA256

    corpus = manifest["pulse_query_corpus"]
    assert corpus["descriptor"] == "pulse-1"
    assert corpus["query_contract_version"] == "1.0"
    assert corpus["digest"] == EXPECTED_PULSE_CORPUS_DIGEST
    assert corpus["physical_file_sha256"] == EXPECTED_PULSE_CORPUS_FILE_SHA256
    assert (
        corpus["read_entry_count"]
        + corpus["write_entry_count"]
        + corpus["fragment_entry_count"]
        == corpus["entry_count"]
        == 97
    )
    assert corpus["external_timeout_seconds"] == 30
    assert corpus["timeout_enforcement"].startswith("isolated-process-watchdog")
    assert corpus["result_normalization"] == {
        "ordered": "preserve-row-sequence-and-canonicalize-each-row/1",
        "multiset": "sort-canonical-row-encodings-preserving-duplicates/1",
    }

    board_cases = manifest["board_result_supplement"]
    assert board_result_supplement_sha256(manifest) == EXPECTED_SUPPLEMENT_SHA256
    assert board_cases["queries_sha256"] == EXPECTED_SUPPLEMENT_SHA256
    assert board_cases["external_timeout_seconds"] == 30
    assert all(
        item["ordering"] in {"ordered", "multiset"} for item in board_cases["queries"]
    )
    for case in board_cases["queries"]:
        arguments = case["arguments"]
        for type_field in ("node_type", "from_type", "to_type"):
            if type_field in arguments:
                assert arguments[type_field] in NODE_TYPES
        if "graph_layer" in arguments:
            assert arguments["graph_layer"] in {"all", "canonical", "working"}
        if case["method"] == "edge_exists":
            assert (
                arguments["edge_type"],
                arguments["from_type"],
                arguments["to_type"],
            ) in {
                (entry.logical_type, entry.from_type, entry.to_type)
                for entry in PULSE_RELATIONSHIP_LAYOUT.entries
            }
    mutating_store_methods = {
        "bootstrap",
        "create_node",
        "create_edge",
        "update_node",
        "mark_superseded",
        "increment_attestation",
        "delete_nodes_by_session",
        "delete_edges_by_session",
    }
    executable_read_methods = (
        _public_callables(SemanticGraphStore)
        - mutating_store_methods
        - {"capabilities"}
    )
    assert {item["method"] for item in board_cases["queries"]} == (
        executable_read_methods
    )

    benchmark = manifest["benchmark_contract"]
    assert benchmark["backends"] == ["ladybug", "grafx"]
    assert [item["field"] for item in benchmark["required_metrics"]] == [
        "throughput_ops_per_second",
        "latency_ms_p50",
        "latency_ms_p90",
        "latency_ms_p99",
        "peak_memory_bytes",
    ]
    assert benchmark["one_record_per_backend"] is True
    assert benchmark["same_trace_corpus_and_host_required"] is True
    assert manifest["acceptance"] == {
        "maximum_unexplained_divergences": 0,
        "maximum_verify_failures": 0,
        "required_verify_scope": "all",
        "query_timeout_failures_allowed": 0,
    }


def test_loader_rejects_unfrozen_or_ambiguous_json(tmp_path: Path) -> None:
    original = MANIFEST_PATH.read_text(encoding="utf-8")
    stale = original.replace(EXPECTED_TRACE_SHA256, "0" * 64, 1)
    stale_path = tmp_path / "stale.json"
    stale_path.write_text(stale, encoding="utf-8")
    with pytest.raises(ValueError, match="invalid frozen digest"):
        load_gate_manifest(stale_path)

    duplicate = original.replace(
        '"manifest_format":',
        '"manifest_format": "duplicate", "manifest_format":',
        1,
    )
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_gate_manifest(duplicate_path)
