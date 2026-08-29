"""Deterministic expansion and oracle for the frozen M-PULSE-7 gate.

This module is test infrastructure, not rollout runtime code.  Its formats and
algorithms are deliberately small enough to audit and have no dependency on
the M-PULSE-7 implementation modules.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from okto_pulse.core.kg.interfaces.graph_transaction import (
    SOURCE_PROJECTION_REMOVED_REASON,
)
from okto_pulse.core.kg.schema_contract import NODE_TYPES as PULSE_NODE_TYPES

from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)
from okto_pulse.community.adapters.graph_ddl import COMMON_NODE_COLUMNS

MASK_64 = (1 << 64) - 1
SPLITMIX64_GAMMA = 0x9E3779B97F4A7C15
TRACE_SHUFFLE_SALT = 0x4D50554C53453731
RELATIONSHIP_LAYOUTS = tuple(
    (entry.logical_type, entry.from_type, entry.to_type)
    for entry in PULSE_RELATIONSHIP_LAYOUT.entries
)
NODE_ATTRIBUTE_NAMES = tuple(
    name
    for name, _data_type in COMMON_NODE_COLUMNS
    if name not in {"id", "source_session_id"}
)
TRACE_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def load_gate_manifest(path: Path) -> dict[str, Any]:
    document = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=_reject_json_constant,
    )
    if type(document) is not dict:
        raise ValueError("gate manifest must be a JSON object")
    if document.get("manifest_format") != (
        "okto-pulse-community-m-pulse-7-acceptance-gate/1"
    ):
        raise ValueError("unsupported M-PULSE-7 gate manifest format")
    trace = document.get("trace")
    if type(trace) is not dict:
        raise ValueError("gate manifest trace must be an object")
    schema = trace.get("schema_authority")
    if type(schema) is not dict:
        raise ValueError("gate manifest schema authority must be an object")
    frozen_digests = [
        schema.get("node_types_sha256"),
        schema.get("relationship_layouts_sha256"),
        trace.get("expanded_trace_sha256"),
        trace.get("final_model_fingerprint_sha256"),
        document.get("crash_points", {}).get("points_sha256"),
        document.get("pulse_query_corpus", {}).get("digest"),
        document.get("pulse_query_corpus", {}).get("physical_file_sha256"),
        document.get("board_result_supplement", {}).get("queries_sha256"),
        *(
            item.get("model_fingerprint_sha256")
            for item in trace.get("checkpoints", ())
            if type(item) is dict
        ),
    ]
    if any(
        type(value) is not str
        or len(value) != 64
        or value == "0" * 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in frozen_digests
    ):
        raise ValueError("gate manifest contains an absent or invalid frozen digest")
    return document


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class SplitMix64:
    """Version-stable PRNG used only by the frozen trace generator."""

    def __init__(self, seed: int) -> None:
        self._state = seed & MASK_64

    def next_u64(self) -> int:
        self._state = (self._state + SPLITMIX64_GAMMA) & MASK_64
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK_64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK_64
        return (value ^ (value >> 31)) & MASK_64

    def randbelow(self, upper_bound: int) -> int:
        if upper_bound <= 0:
            raise ValueError("upper_bound must be positive")
        limit = (1 << 64) - ((1 << 64) % upper_bound)
        while True:
            value = self.next_u64()
            if value < limit:
                return value % upper_bound


def _parse_seed(value: object) -> int:
    if type(value) is not str or not value.startswith("0x"):
        raise ValueError("trace seed must be a hexadecimal string")
    seed = int(value, 16)
    if not 0 <= seed <= MASK_64:
        raise ValueError("trace seed must fit in 64 bits")
    return seed


def _sample(seed: int, sequence: int, salt: int) -> int:
    mixed_seed = seed ^ ((sequence * SPLITMIX64_GAMMA) & MASK_64) ^ salt
    return SplitMix64(mixed_seed).next_u64()


def _timestamp(sequence: int) -> str:
    return (
        (TRACE_EPOCH + timedelta(seconds=sequence)).isoformat().replace("+00:00", "Z")
    )


def _node_id(index: int) -> str:
    return f"trace-node-{index:05d}"


def _node_type(index: int) -> str:
    return PULSE_NODE_TYPES[index % len(PULSE_NODE_TYPES)]


def _anchor_index(
    *,
    seed: int,
    sequence: int,
    salt: int,
    bootstrap_nodes: int,
    projection_owner_slots: int,
    node_type: str | None = None,
) -> int:
    sampled = _sample(seed, sequence, salt)
    typed_node_count = bootstrap_nodes // len(PULSE_NODE_TYPES)
    safe_node_count = typed_node_count - projection_owner_slots
    if safe_node_count <= 0:
        raise ValueError("the trace needs non-projection anchor nodes")
    selected_type = (
        PULSE_NODE_TYPES[sampled % len(PULSE_NODE_TYPES)]
        if node_type is None
        else node_type
    )
    type_offset = PULSE_NODE_TYPES.index(selected_type)
    ordinal = projection_owner_slots + sampled % safe_node_count
    return type_offset + len(PULSE_NODE_TYPES) * ordinal


def _projection_anchor_index(node_type: str, owner_slot: int) -> int:
    return PULSE_NODE_TYPES.index(node_type) + len(PULSE_NODE_TYPES) * owner_slot


def _operation_payload(
    family: str,
    *,
    method: str,
    family_index: int,
    sequence: int,
    seed: int,
    bootstrap_nodes: int,
    projection_owner_slots: int,
    bootstrap_projection_edges: int,
) -> dict[str, Any]:
    sampled = _sample(seed, sequence, 0xA11CE5EED)
    timestamp = _timestamp(sequence)

    if family == "create_node":
        node_type = _node_type(family_index)
        typed_ordinal = family_index // len(PULSE_NODE_TYPES)
        source_session_id = (
            "baseline"
            if family_index < bootstrap_nodes
            else f"session-{family_index % 32:02d}"
        )
        source_artifact_ref = f"trace-artifact-{family_index:05d}"
        created_by_agent = "trace-gate-v1"
        if family_index < bootstrap_nodes and typed_ordinal < projection_owner_slots:
            owner_id = f"trace-owner-{typed_ordinal:02d}"
            if node_type == "Entity":
                source_artifact_ref = f"refinement:{owner_id}"
            elif node_type == "Decision":
                source_artifact_ref = (
                    f"refinement:{owner_id}:rdl:ledger-{typed_ordinal:02d}:decision"
                )
                created_by_agent = "system:layer1_worker"
            elif node_type == "Alternative":
                source_artifact_ref = (
                    f"refinement:{owner_id}:rdl:ledger-{typed_ordinal:02d}:"
                    f"alternative:{typed_ordinal:064x}"
                )
                created_by_agent = "system:layer1_worker"
        attrs = {
            "attestation_count": 0,
            "content": f"deterministic-content-{family_index:05d}",
            "created_at": timestamp,
            "created_by_agent": created_by_agent,
            "generation": 1,
            "graph_layer": "working",
            "human_curated": False,
            "maturity_status": "working_mature",
            "priority_boost": 0.0,
            "query_hits": 0,
            "relevance_score": round(
                0.5 + (sampled % 500) / 1000.0,
                3,
            ),
            "revocation_reason": "",
            "source_artifact_ref": source_artifact_ref,
            "source_confidence": 1.0,
            "source_content_hash": hashlib.sha256(
                f"trace-content-{family_index}".encode()
            ).hexdigest(),
            "title": f"trace-title-{family_index:05d}",
        }
        payload = {
            "node_type": node_type,
            "node_id": _node_id(family_index),
            "attrs": attrs,
        }
        if method.startswith("SemanticGraphStore."):
            attrs["source_session_id"] = source_session_id
        else:
            payload["source_session_id"] = source_session_id
        return payload

    if family == "create_edge":
        if family_index < bootstrap_projection_edges:
            owner_slot = family_index // 2
            from_type = "Decision" if family_index % 2 == 0 else "Alternative"
            to_type = "Entity"
            edge_type = "belongs_to"
            from_index = _projection_anchor_index(from_type, owner_slot)
            to_index = _projection_anchor_index(to_type, owner_slot)
            projection_kind = from_type.lower()
            created_by_session_id = "baseline-projection"
            rule_id = f"belongs_to/relational_rdl_{projection_kind}@v2.0"
        else:
            generic_index = family_index - bootstrap_projection_edges
            edge_type, from_type, to_type = RELATIONSHIP_LAYOUTS[
                generic_index % len(RELATIONSHIP_LAYOUTS)
            ]
            from_index = _anchor_index(
                seed=seed,
                sequence=sequence,
                salt=0x0E01,
                bootstrap_nodes=bootstrap_nodes,
                projection_owner_slots=projection_owner_slots,
                node_type=from_type,
            )
            to_index = _anchor_index(
                seed=seed,
                sequence=sequence,
                salt=0x0E02,
                bootstrap_nodes=bootstrap_nodes,
                projection_owner_slots=projection_owner_slots,
                node_type=to_type,
            )
            created_by_session_id = f"session-{generic_index % 32:02d}"
            rule_id = f"{edge_type}/trace_gate@v1/{generic_index:05d}"
        return {
            "edge_type": edge_type,
            "from_type": from_type,
            "to_type": to_type,
            "from_id": _node_id(from_index),
            "to_id": _node_id(to_index),
            "attrs": {
                "confidence": round(0.5 + (sampled % 500) / 1000.0, 3),
                "created_by": "trace-gate-v1",
                "created_by_session_id": created_by_session_id,
                "created_at": timestamp,
                "fallback_reason": "",
                "layer": "deterministic",
                "rule_id": rule_id,
            },
        }

    if family in {"update_node", "replace_node_payload"}:
        target_index = _anchor_index(
            seed=seed,
            sequence=sequence,
            salt=0x1001 if family == "update_node" else 0x1002,
            bootstrap_nodes=bootstrap_nodes,
            projection_owner_slots=projection_owner_slots,
        )
        attrs = {
            "attestation_count": 0,
            "content": f"{family}-content-{family_index:05d}",
            "created_by_agent": "trace-gate-v1",
            "generation": family_index + 2,
            "graph_layer": "working",
            "human_curated": False,
            "maturity_status": "working_mature",
            "priority_boost": 0.0,
            "query_hits": family_index + 1,
            "relevance_score": round(0.5 + (sampled % 500) / 1000.0, 3),
            "revocation_reason": "",
            "source_artifact_ref": f"trace-replacement-{family_index:05d}",
            "source_confidence": 1.0,
            "title": f"{family}-title-{family_index:05d}",
        }
        return {
            "node_type": _node_type(target_index),
            "node_id": _node_id(target_index),
            "attrs": attrs,
            **(
                {"source_session_id": "baseline"}
                if family == "replace_node_payload"
                else {}
            ),
        }

    if family == "mark_superseded":
        target_index = _anchor_index(
            seed=seed,
            sequence=sequence,
            salt=0x2001,
            bootstrap_nodes=bootstrap_nodes,
            projection_owner_slots=projection_owner_slots,
            node_type="Decision",
        )
        replacement_index = _anchor_index(
            seed=seed,
            sequence=sequence,
            salt=0x2002,
            bootstrap_nodes=bootstrap_nodes,
            projection_owner_slots=projection_owner_slots,
            node_type="Decision",
        )
        return {
            "node_type": "Decision",
            "node_id": _node_id(target_index),
            "superseded_by": _node_id(replacement_index),
            "superseded_at": timestamp,
            "revocation_reason": f"trace-reason-{family_index % 11:02d}",
        }

    if family == "increment_attestation":
        target_index = _anchor_index(
            seed=seed,
            sequence=sequence,
            salt=0x3001,
            bootstrap_nodes=bootstrap_nodes,
            projection_owner_slots=projection_owner_slots,
            node_type="Learning",
        )
        return {
            "node_type": "Learning",
            "node_id": _node_id(target_index),
            "attested_at": timestamp,
        }

    if family == "replace_with_source_deleted_tombstone":
        target_index = _anchor_index(
            seed=seed,
            sequence=sequence,
            salt=0x3501,
            bootstrap_nodes=bootstrap_nodes,
            projection_owner_slots=projection_owner_slots,
            node_type="Constraint",
        )
        return {
            "node_type": "Constraint",
            "node_id": _node_id(target_index),
            "graph_layer": "working",
            "maturity_status": "working_stale",
            "revocation_reason": "source_deleted",
            "relevance_score": 0.0,
        }

    if family in {
        "reconcile_spec_lineage_parent",
        "clear_spec_lineage_parent",
    }:
        source_index = _anchor_index(
            seed=seed,
            sequence=sequence,
            salt=0x4001,
            bootstrap_nodes=bootstrap_nodes,
            projection_owner_slots=projection_owner_slots,
            node_type="Entity",
        )
        payload: dict[str, Any] = {"source_id": _node_id(source_index)}
        if family == "reconcile_spec_lineage_parent":
            target_index = _anchor_index(
                seed=seed,
                sequence=sequence,
                salt=0x4002,
                bootstrap_nodes=bootstrap_nodes,
                projection_owner_slots=projection_owner_slots,
                node_type="Entity",
            )
            payload.update(
                {
                    "target_id": _node_id(target_index),
                    "attrs": {
                        "created_at": timestamp,
                        "rule_id": ("belongs_to/spec_to_refinement@trace-v1"),
                        "confidence": 1.0,
                        "created_by_session_id": "baseline",
                        "layer": "deterministic",
                        "created_by": "trace-gate-v1",
                        "fallback_reason": "",
                    },
                }
            )
        return payload

    if family == "reconcile_projection_active_set":
        owner_slot = family_index % projection_owner_slots
        owner_index = _projection_anchor_index("Entity", owner_slot)
        return {
            "owner_type": "refinement",
            "owner_id": f"trace-owner-{owner_slot:02d}",
            "namespace": "rdl",
            "owner_node_id": _node_id(owner_index),
            "active_nodes": [],
            "active_edges": [],
        }

    if family in {"delete_edges_by_session", "delete_nodes_by_session"}:
        payload = {"session_id": f"session-{family_index % 32:02d}"}
        if family == "delete_nodes_by_session" and method.startswith(
            "GraphTransactionScope."
        ):
            payload["node_types"] = list(PULSE_NODE_TYPES)
        return payload

    raise ValueError(f"unsupported trace family: {family}")


def expand_trace(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    trace = manifest["trace"]
    seed = _parse_seed(trace["seed"])
    operation_count = int(trace["operation_count"])
    bootstrap_nodes = int(trace["bootstrap_create_nodes"])
    projection_owner_slots = int(trace["projection_owner_slots"])
    bootstrap_projection_edges = int(trace["bootstrap_projection_edges"])
    distribution = trace["family_distribution"]
    counts = {str(item["family"]): int(item["count"]) for item in distribution}
    methods = {
        str(item["family"]): tuple(str(method) for method in item["methods"])
        for item in distribution
    }
    if len(counts) != len(distribution):
        raise ValueError("trace family names must be unique")
    if sum(counts.values()) != operation_count:
        raise ValueError("trace family counts do not match operation_count")
    if bootstrap_nodes <= 0 or bootstrap_nodes % len(PULSE_NODE_TYPES) != 0:
        raise ValueError("bootstrap nodes must be positive and type-balanced")
    typed_node_count = bootstrap_nodes // len(PULSE_NODE_TYPES)
    if projection_owner_slots <= 0 or projection_owner_slots >= typed_node_count:
        raise ValueError("projection slots must leave at least one safe typed anchor")
    if bootstrap_projection_edges != projection_owner_slots * 2:
        raise ValueError("each projection owner needs Decision and Alternative edges")
    if counts.get("create_node", 0) < bootstrap_nodes:
        raise ValueError("create_node count cannot satisfy bootstrap prefix")
    if counts.get("create_edge", 0) < bootstrap_projection_edges:
        raise ValueError("create_edge count cannot satisfy projection bootstrap")
    if any(not family_methods for family_methods in methods.values()):
        raise ValueError("every trace family must name an executable method surface")

    shuffled_families: list[str] = []
    for family, count in counts.items():
        prefix_count = 0
        if family == "create_node":
            prefix_count = bootstrap_nodes
        elif family == "create_edge":
            prefix_count = bootstrap_projection_edges
        remaining = count - prefix_count
        if remaining < 0:
            raise ValueError("negative trace family remainder")
        shuffled_families.extend([family] * remaining)
    random = SplitMix64(seed ^ TRACE_SHUFFLE_SALT)
    for index in range(len(shuffled_families) - 1, 0, -1):
        swap_index = random.randbelow(index + 1)
        shuffled_families[index], shuffled_families[swap_index] = (
            shuffled_families[swap_index],
            shuffled_families[index],
        )
    families = (
        ["create_node"] * bootstrap_nodes
        + ["create_edge"] * bootstrap_projection_edges
        + shuffled_families
    )
    if len(families) != operation_count:
        raise AssertionError("expanded trace length drifted")

    family_indices = {family: 0 for family in counts}
    operations: list[dict[str, Any]] = []
    for sequence, family in enumerate(families, start=1):
        family_index = family_indices[family]
        family_indices[family] += 1
        method = methods[family][family_index % len(methods[family])]
        operations.append(
            {
                "sequence": sequence,
                "operation_id": f"m-pulse-7-{sequence:05d}",
                "family": family,
                "method": method,
                "payload": _operation_payload(
                    family,
                    method=method,
                    family_index=family_index,
                    sequence=sequence,
                    seed=seed,
                    bootstrap_nodes=bootstrap_nodes,
                    projection_owner_slots=projection_owner_slots,
                    bootstrap_projection_edges=bootstrap_projection_edges,
                ),
            }
        )
    return tuple(operations)


def expanded_trace_sha256(operations: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for operation in operations:
        digest.update(canonical_json_bytes(operation))
        digest.update(b"\n")
    return digest.hexdigest()


class DeterministicGraphModel:
    """Small logical oracle for trace checkpoint fingerprints."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.lineage: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _edge_key(payload: Mapping[str, Any]) -> str:
        attrs = payload.get("attrs", {})
        return "|".join(
            (
                str(payload["edge_type"]),
                str(payload["from_type"]),
                str(payload["to_type"]),
                str(payload["from_id"]),
                str(payload["to_id"]),
                str(attrs.get("rule_id", "")),
            )
        )

    def apply(self, operation: Mapping[str, Any]) -> None:
        family = str(operation["family"])
        payload = deepcopy(operation["payload"])

        if family == "create_node":
            node_id = str(payload["node_id"])
            if node_id in self.nodes:
                raise ValueError(f"duplicate model node: {node_id}")
            attrs = {name: None for name in NODE_ATTRIBUTE_NAMES}
            attrs.update(payload["attrs"])
            source_session_id = payload.get(
                "source_session_id", attrs.pop("source_session_id", None)
            )
            self.nodes[node_id] = {
                "node_id": node_id,
                "node_type": payload["node_type"],
                "source_session_id": source_session_id,
                "attrs": attrs,
            }
            return

        if family == "create_edge":
            if (
                payload["from_id"] not in self.nodes
                or payload["to_id"] not in self.nodes
            ):
                raise ValueError("model edge endpoint is absent")
            key = self._edge_key(payload)
            if key in self.edges:
                raise ValueError(f"duplicate model edge: {key}")
            self.edges[key] = {"edge_key": key, **payload}
            return

        if family in {"update_node", "replace_node_payload"}:
            node_id = str(payload["node_id"])
            node = self.nodes.get(node_id)
            if node is None:
                raise ValueError(f"model update target is absent: {node_id}")
            if family == "update_node":
                node["attrs"].update(payload["attrs"])
            else:
                attrs = {name: None for name in NODE_ATTRIBUTE_NAMES}
                attrs.update(payload["attrs"])
                node["attrs"] = attrs
                node["source_session_id"] = payload["source_session_id"]
            return

        if family == "mark_superseded":
            node = self.nodes[str(payload["node_id"])]
            node["attrs"].update(
                {
                    "superseded_by": payload["superseded_by"],
                    "superseded_at": payload["superseded_at"],
                    "revocation_reason": payload["revocation_reason"],
                }
            )
            return

        if family == "increment_attestation":
            node = self.nodes[str(payload["node_id"])]
            current = int(node["attrs"].get("attestation_count", 0))
            node["attrs"]["attestation_count"] = current + 1
            node["attrs"]["last_attested_at"] = payload["attested_at"]
            return

        if family == "replace_with_source_deleted_tombstone":
            node_id = str(payload["node_id"])
            node = self.nodes[node_id]
            before = node["attrs"]
            attrs = {name: None for name in NODE_ATTRIBUTE_NAMES}
            attrs.update(
                {
                    "content": "",
                    "context": "",
                    "created_by_agent": before.get("created_by_agent")
                    or "system:source-deletion",
                    "generation": int(before.get("generation") or 0),
                    "graph_layer": payload["graph_layer"],
                    "human_curated": False,
                    "justification": "",
                    "maturity_status": payload["maturity_status"],
                    "priority_boost": 0.0,
                    "query_hits": 0,
                    "relevance_score": payload["relevance_score"],
                    "revocation_reason": payload["revocation_reason"],
                    "source_artifact_ref": before.get("source_artifact_ref") or "",
                    "source_confidence": 0.0,
                    "source_span_quote": "",
                    "title": "",
                }
            )
            if before.get("created_at") is not None:
                attrs["created_at"] = before["created_at"]
            node["attrs"] = attrs
            self.edges = {
                key: edge
                for key, edge in self.edges.items()
                if edge["from_id"] != node_id and edge["to_id"] != node_id
            }
            return

        if family == "reconcile_spec_lineage_parent":
            source_id = str(payload["source_id"])
            existing = self.lineage.get(source_id)
            if (
                existing is not None
                and existing["target_id"] == payload["target_id"]
                and existing["attrs"].get("rule_id") == payload["attrs"].get("rule_id")
            ):
                return
            self.lineage[source_id] = payload
            return

        if family == "clear_spec_lineage_parent":
            self.lineage.pop(str(payload["source_id"]), None)
            return

        if family == "reconcile_projection_active_set":
            owner_id = str(payload["owner_id"])
            owner_slot = int(owner_id.rsplit("-", maxsplit=1)[1])
            removed = {
                _node_id(_projection_anchor_index(node_type, owner_slot))
                for node_type in ("Decision", "Alternative")
            }
            for node_id in removed:
                node = self.nodes.get(node_id)
                if node is not None and node["attrs"].get("revocation_reason", "") in {
                    "",
                    SOURCE_PROJECTION_REMOVED_REASON,
                }:
                    node["attrs"]["revocation_reason"] = (
                        SOURCE_PROJECTION_REMOVED_REASON
                    )
            self.edges = {
                key: edge
                for key, edge in self.edges.items()
                if edge["from_id"] not in removed and edge["to_id"] not in removed
            }
            return

        if family == "delete_edges_by_session":
            session_id = payload["session_id"]
            self.edges = {
                key: edge
                for key, edge in self.edges.items()
                if edge["attrs"].get("created_by_session_id") != session_id
            }
            return

        if family == "delete_nodes_by_session":
            session_id = payload["session_id"]
            allowed_types = frozenset(payload.get("node_types", PULSE_NODE_TYPES))
            removed = {
                node_id
                for node_id, node in self.nodes.items()
                if node["source_session_id"] == session_id
                and node["node_type"] in allowed_types
            }
            for node_id in removed:
                del self.nodes[node_id]
            self.edges = {
                key: edge
                for key, edge in self.edges.items()
                if edge["from_id"] not in removed and edge["to_id"] not in removed
            }
            for source_id in tuple(self.lineage):
                lineage = self.lineage[source_id]
                if source_id in removed or lineage.get("target_id") in removed:
                    del self.lineage[source_id]
            return

        raise ValueError(f"unsupported model family: {family}")

    def export_state(self) -> dict[str, Any]:
        return {
            "nodes": [self.nodes[key] for key in sorted(self.nodes)],
            "edges": [self.edges[key] for key in sorted(self.edges)],
            "lineage": [self.lineage[key] for key in sorted(self.lineage)],
        }

    @classmethod
    def from_export(cls, state: Mapping[str, Any]) -> DeterministicGraphModel:
        model = cls()
        model.nodes = {str(node["node_id"]): deepcopy(node) for node in state["nodes"]}
        model.edges = {str(edge["edge_key"]): deepcopy(edge) for edge in state["edges"]}
        model.lineage = {
            str(item["source_id"]): deepcopy(item) for item in state["lineage"]
        }
        return model

    def recovered_copy(self) -> DeterministicGraphModel:
        encoded = canonical_json_bytes(self.export_state())
        decoded = json.loads(encoded, object_pairs_hook=_strict_object)
        return self.from_export(decoded)

    def fingerprint_sha256(self) -> str:
        return canonical_sha256(self.export_state())

    def census(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges) + len(self.lineage),
            "lineage_edges": len(self.lineage),
            "projection_removed_nodes": sum(
                1
                for node in self.nodes.values()
                if node["attrs"].get("revocation_reason")
                == SOURCE_PROJECTION_REMOVED_REASON
            ),
        }


@dataclass(frozen=True, slots=True)
class TraceEvaluation:
    trace_sha256: str
    checkpoints: tuple[dict[str, Any], ...]
    recovery_cycles: tuple[dict[str, Any], ...]
    final_fingerprint_sha256: str
    final_census: dict[str, int]


def evaluate_trace(manifest: Mapping[str, Any]) -> TraceEvaluation:
    operations = expand_trace(manifest)
    checkpoint_boundaries = frozenset(
        int(item["after_operations"]) for item in manifest["trace"]["checkpoints"]
    )
    recovery_boundaries = frozenset(
        int(value) for value in manifest["reopen_recovery_cycles"]["after_operations"]
    )
    model = DeterministicGraphModel()
    checkpoints: list[dict[str, Any]] = []
    recovery_cycles: list[dict[str, Any]] = []

    for operation in operations:
        model.apply(operation)
        sequence = int(operation["sequence"])
        if sequence in checkpoint_boundaries:
            checkpoints.append(
                {
                    "after_operations": sequence,
                    "model_fingerprint_sha256": model.fingerprint_sha256(),
                    "census": model.census(),
                }
            )
        if sequence in recovery_boundaries:
            before = model.fingerprint_sha256()
            model = model.recovered_copy()
            after = model.fingerprint_sha256()
            recovery_cycles.append(
                {
                    "after_operations": sequence,
                    "before_fingerprint_sha256": before,
                    "after_fingerprint_sha256": after,
                }
            )

    return TraceEvaluation(
        trace_sha256=expanded_trace_sha256(operations),
        checkpoints=tuple(checkpoints),
        recovery_cycles=tuple(recovery_cycles),
        final_fingerprint_sha256=model.fingerprint_sha256(),
        final_census=model.census(),
    )


def board_result_supplement_sha256(manifest: Mapping[str, Any]) -> str:
    return canonical_sha256(manifest["board_result_supplement"]["queries"])


def pulse_query_corpus_digest(manifest: Mapping[str, Any]) -> str:
    return str(manifest["pulse_query_corpus"]["digest"])


def crash_points_sha256(manifest: Mapping[str, Any]) -> str:
    return canonical_sha256(manifest["crash_points"]["points"])


__all__ = [
    "DeterministicGraphModel",
    "SplitMix64",
    "TraceEvaluation",
    "board_result_supplement_sha256",
    "canonical_json_bytes",
    "canonical_sha256",
    "crash_points_sha256",
    "evaluate_trace",
    "expand_trace",
    "expanded_trace_sha256",
    "load_gate_manifest",
    "pulse_query_corpus_digest",
]
