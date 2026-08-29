"""Real Community backends for the frozen M-PULSE-7 acceptance runner.

The factories in this module build the productive Community routed graph
bundle.  Each ``(workspace, run_id, backend)`` tuple owns a separate storage
root, while subsequent factory calls reopen that same root from its persisted
Board binding.  The module intentionally does not import the runner: module
level factories must remain importable and pickleable by Windows ``spawn``
workers.

Two graph digests are reported and must not be confused:

* ``logical_graph_sha256`` is Core's schema-bound, order-independent
  logical-transfer fingerprint;
* ``trace_model_sha256`` is the acceptance manifest's canonical model
  digest.  It is reconstructed from the same physical logical snapshot, not
  replayed from the trace and never copied from an expected argument.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from okto_pulse.core.infra.config import configure_settings
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent,
    SpecLineageEdgeSnapshot,
    is_spec_lineage_rule_id,
)
from okto_pulse.core.kg.interfaces.registry import (
    capture_registry_state_for_tests,
    restore_registry_state_for_tests,
)
from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalFingerprintAccumulator,
    LogicalNode,
    LogicalRelation,
    LogicalSchemaIndex,
    LogicalTimestamp,
    LogicalVector,
)
from okto_pulse.core.kg.logical_transfer.model import LogicalCounts

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.composition import (
    build_community_kg_composition,
)
from okto_pulse.community.adapters.graph_ddl import (
    COMMON_NODE_COLUMNS,
    COMMON_REL_COLUMNS,
)
from okto_pulse.community.adapters.logical_transfer_factories import (
    SCOPE_BOARD,
    make_grafx_logical_source,
    make_ladybug_logical_source,
)
from okto_pulse.community.config import CommunitySettings

_BACKENDS: Final[frozenset[str]] = frozenset({"ladybug", "grafx"})
_BOARD_META_TYPE: Final[str] = "BoardMeta"
_BATCH_SIZE: Final[int] = 500
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CASE = re.compile(r"[^a-z0-9-]+")
_NODE_ATTRS: Final[tuple[str, ...]] = tuple(
    name
    for name, _data_type in COMMON_NODE_COLUMNS
    if name not in {"id", "source_session_id"}
)
_REL_ATTRS: Final[tuple[str, ...]] = tuple(
    name for name, _data_type in COMMON_REL_COLUMNS
)
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_FIXED_INSTANT: Final[str] = "2026-01-01T00:00:00Z"
_FIXED_MOMENT: Final[datetime] = datetime(2026, 1, 1, tzinfo=UTC)
_WRITE_FAMILY_IDS: Final[frozenset[str]] = frozenset(
    {
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
    }
)
_PROPERTY_WRITE_FAMILIES: Final[dict[str, tuple[str, ...]]] = {
    "I05": ("last_recomputed_at",),
    "I06": (
        "pre_cancellation_relevance_score",
        "relevance_score",
        "revocation_reason",
        "superseded_by",
        "superseded_at",
    ),
    "I07": (
        "pre_cancellation_relevance_score",
        "relevance_score",
        "revocation_reason",
        "superseded_by",
        "superseded_at",
    ),
    "I10": ("priority_boost",),
    "I21": ("source_content_hash",),
    "I22": (
        "graph_layer",
        "maturity_status",
        "revocation_reason",
        "relevance_score",
        "title",
        "content",
        "context",
        "justification",
        "source_span_quote",
        "source_content_hash",
    ),
    "I23": ("graph_layer", "maturity_status"),
    "I37": ("superseded_by", "superseded_at", "revocation_reason"),
    "I40": ("relevance_score", "pre_cancellation_relevance_score"),
    "I41": ("relevance_score",),
    "I42": ("query_hits", "last_queried_at"),
    "I65": ("relevance_score", "last_recomputed_at"),
    "I66": (
        "relevance_score",
        "pre_cancellation_relevance_score",
        "last_recomputed_at",
    ),
    "I67": ("relevance_score", "last_recomputed_at"),
    "I68": (
        "relevance_score",
        "pre_cancellation_relevance_score",
        "last_recomputed_at",
    ),
}
_TEMPORAL_EVIDENCE_PROPERTIES: Final[frozenset[str]] = frozenset(
    {
        "created_at",
        "last_attested_at",
        "last_queried_at",
        "last_recomputed_at",
        "superseded_at",
    }
)


class RealGateBackendError(RuntimeError):
    """A real backend could not prove a frozen acceptance assertion."""


@dataclass(frozen=True, slots=True)
class PhysicalFingerprintObservation:
    """Both independently named digests from one fixed physical snapshot."""

    trace_model_sha256: str
    logical_graph_sha256: str
    logical_counts: LogicalCounts
    model_nodes: int
    model_edges: int
    model_lineage_edges: int


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RealGateBackendError(reason)


def _context_text(context: object, name: str) -> str:
    value = getattr(context, name, None)
    if type(value) is not str or not value:
        raise RealGateBackendError(f"gate_context_{name}_invalid")
    return value


def _storage_root(context: object) -> Path:
    """Resolve a contained, stable root without trusting ``run_id`` as a path."""

    backend = _context_text(context, "backend")
    _require(backend in _BACKENDS, "gate_context_backend_invalid")
    workspace = Path(_context_text(context, "workspace"))
    _require(workspace.is_absolute(), "gate_context_workspace_not_absolute")
    workspace = Path(os.path.abspath(workspace))
    run_digest = hashlib.sha256(
        _context_text(context, "run_id").encode("utf-8")
    ).hexdigest()
    # Grafx owns descriptive nested file names.  Keeping this harness prefix
    # short avoids crossing the legacy Windows MAX_PATH boundary in spawned
    # workers while retaining 96 bits of collision-resistant run isolation.
    backend_component = "l" if backend == "ladybug" else "g"
    root = workspace / ".mp7" / backend_component / run_digest[:24]
    try:
        root.relative_to(workspace)
    except ValueError as failure:  # pragma: no cover - defensive after hashing
        raise RealGateBackendError(
            "gate_backend_storage_escapes_workspace"
        ) from failure
    root.mkdir(parents=True, exist_ok=True)
    return root


def _logical_value(value: Any) -> Any:
    if value is LOGICAL_NULL:
        return None
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, LogicalTimestamp):
        moment = _EPOCH + timedelta(microseconds=value.micros)
        return moment.isoformat().replace("+00:00", "Z")
    if isinstance(value, LogicalVector):
        return list(value.components)
    raise RealGateBackendError(
        f"physical_snapshot_value_not_canonical:{type(value).__name__}"
    )


def _node_from_logical(node: LogicalNode) -> dict[str, Any] | None:
    if node.type_name == _BOARD_META_TYPE:
        return None
    properties = {
        name: _logical_value(value) for name, value in node.properties.items()
    }
    _require(properties.pop("id", None) == node.key, "physical_node_key_mismatch")
    _require(
        set(properties) == {"source_session_id", *_NODE_ATTRS},
        f"physical_node_property_set_mismatch:{node.type_name}",
    )
    source_session_id = properties.pop("source_session_id")
    attrs = {name: properties[name] for name in _NODE_ATTRS}
    return {
        "node_id": node.key,
        "node_type": node.type_name,
        "source_session_id": source_session_id,
        "attrs": attrs,
    }


def _relation_from_logical(
    relation: LogicalRelation,
) -> tuple[str, dict[str, Any]]:
    attrs = {name: _logical_value(value) for name, value in relation.properties.items()}
    _require(
        set(attrs) == set(_REL_ATTRS),
        f"physical_relation_property_set_mismatch:{relation.layout_name}",
    )
    attrs = {name: attrs[name] for name in _REL_ATTRS}
    rule_id = attrs.get("rule_id")
    _require(type(rule_id) is str, "physical_relation_rule_id_invalid")
    if is_spec_lineage_rule_id(rule_id):
        _require(
            relation.layout_name == "belongs_to"
            and relation.source_type == "Entity"
            and relation.target_type == "Entity",
            "physical_spec_lineage_layout_invalid",
        )
        return (
            "lineage",
            {
                "source_id": relation.source_key,
                "target_id": relation.target_key,
                "attrs": attrs,
            },
        )
    edge_key = (
        f"{relation.layout_name}|{relation.source_type}|{relation.target_type}|"
        f"{relation.source_key}|{relation.target_key}|{rule_id}"
    )
    return (
        "edge",
        {
            "edge_key": edge_key,
            "edge_type": relation.layout_name,
            "from_type": relation.source_type,
            "to_type": relation.target_type,
            "from_id": relation.source_key,
            "to_id": relation.target_key,
            "attrs": attrs,
        },
    )


def _observe_snapshot(snapshot: Any) -> PhysicalFingerprintObservation:
    """Derive both fingerprints by consuming one physical fixed snapshot once."""

    schema = snapshot.schema()
    index = LogicalSchemaIndex.build(schema)
    accumulator = LogicalFingerprintAccumulator.for_schema(schema)
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    lineage: dict[str, dict[str, Any]] = {}

    for batch in snapshot.iter_nodes(batch_size=_BATCH_SIZE):
        _require(len(batch) <= _BATCH_SIZE, "physical_node_batch_exceeds_bound")
        for node in batch:
            _require(isinstance(node, LogicalNode), "physical_node_record_invalid")
            index.validate_node(node)
            accumulator.add_node(node)
            model_node = _node_from_logical(node)
            if model_node is None:
                continue
            node_id = str(model_node["node_id"])
            _require(node_id not in nodes, f"physical_node_duplicate:{node_id}")
            nodes[node_id] = model_node

    for batch in snapshot.iter_relations(batch_size=_BATCH_SIZE):
        _require(len(batch) <= _BATCH_SIZE, "physical_relation_batch_exceeds_bound")
        for relation in batch:
            _require(
                isinstance(relation, LogicalRelation),
                "physical_relation_record_invalid",
            )
            index.validate_relation(relation)
            accumulator.add_relation(relation)
            kind, model_relation = _relation_from_logical(relation)
            if kind == "lineage":
                source_id = str(model_relation["source_id"])
                _require(
                    source_id not in lineage,
                    f"physical_spec_lineage_duplicate:{source_id}",
                )
                lineage[source_id] = model_relation
            else:
                edge_key = str(model_relation["edge_key"])
                _require(
                    edge_key not in edges,
                    f"physical_model_edge_key_duplicate:{edge_key}",
                )
                edges[edge_key] = model_relation

    observed_counts = accumulator.counts()
    declared_counts = snapshot.counts()
    _require(
        observed_counts == declared_counts,
        "physical_snapshot_declared_census_mismatch",
    )
    state = {
        "nodes": [nodes[key] for key in sorted(nodes)],
        "edges": [edges[key] for key in sorted(edges)],
        "lineage": [lineage[key] for key in sorted(lineage)],
    }
    model_fingerprint = _canonical_sha256(state)
    m_pulse_5_fingerprint = accumulator.digest()
    _require(bool(_SHA256.fullmatch(model_fingerprint)), "model_digest_invalid")
    _require(
        bool(_SHA256.fullmatch(m_pulse_5_fingerprint)),
        "m_pulse_5_digest_invalid",
    )
    return PhysicalFingerprintObservation(
        trace_model_sha256=model_fingerprint,
        logical_graph_sha256=m_pulse_5_fingerprint,
        logical_counts=observed_counts,
        model_nodes=len(nodes),
        model_edges=len(edges),
        model_lineage_edges=len(lineage),
    )


def _safe_case_board_id(board_id: str, case_id: str) -> str:
    prefix = _SAFE_CASE.sub("-", board_id.casefold()).strip("-")[:24] or "board"
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:20]
    return f"mp7-{prefix}-{digest}"


def _json_graph_value(value: Any) -> Any:
    """Convert only backend-neutral logical values; refuse opaque engine objects."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        _require(math.isfinite(value), "graph_result_non_finite_float")
        return value
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {
            str(key): _json_graph_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_graph_value(item) for item in value]
    raise RealGateBackendError(
        f"graph_result_backend_specific_value:{type(value).__name__}"
    )


def _canonical_temporal_evidence(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif type(value) is str:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as failure:
            raise RealGateBackendError(
                "before_image_temporal_value_invalid"
            ) from failure
    else:
        raise RealGateBackendError(
            f"before_image_temporal_type_invalid:{type(value).__name__}"
        )
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (
        moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _before_image_document(before_image: Any) -> dict[str, Any]:
    document = asdict(before_image)
    attrs = document.get("attrs")
    _require(type(attrs) is dict, "before_image_attrs_invalid")
    for name in _TEMPORAL_EVIDENCE_PROPERTIES.intersection(attrs):
        attrs[name] = _canonical_temporal_evidence(attrs[name])
    normalized = _json_graph_value(document)
    _require(type(normalized) is dict, "before_image_document_invalid")
    return normalized


def _statement_rows(result: Any) -> list[list[Any]]:
    rows = getattr(result, "rows", None)
    _require(type(rows) is tuple, "graph_statement_rows_not_materialized")
    return [[_json_graph_value(value) for value in row] for row in rows]


async def _freeze_gate_bootstrap_timestamp(
    graph_transaction: Any,
    board_id: str,
) -> None:
    """Give independently bootstrapped gate stores identical logical metadata.

    ``BoardMeta`` belongs to the M-PULSE-5 logical graph, so its timestamp must
    participate in the bilateral fingerprint.  The two real backends are
    necessarily initialized one after the other, though, and their productive
    bootstrap clocks therefore cannot agree by accident.  Seed the one
    nondeterministic bootstrap field to the gate's already-frozen instant
    instead of weakening the fingerprint by excluding ``BoardMeta``.
    """

    async with await graph_transaction.begin(board_id) as scope:
        result = scope.execute(
            "MATCH (m:BoardMeta {board_id: $board_id}) "
            "SET m.bootstrapped_at = $bootstrapped_at "
            "RETURN m.board_id",
            {
                "board_id": board_id,
                "bootstrapped_at": _FIXED_MOMENT,
            },
        )
        matched = _statement_rows(result)
    _require(matched == [[board_id]], "gate_board_meta_bootstrap_row_not_found")

    # The productive Grafx executor exposes the statement's pre-write snapshot
    # to RETURN.  Re-read after the transaction commits so
    # this proves durable state instead of the visibility convention of one
    # backend's SET operator.
    async with await graph_transaction.begin(board_id) as scope:
        result = scope.execute(
            "MATCH (m:BoardMeta {board_id: $board_id}) RETURN m.bootstrapped_at",
            {"board_id": board_id},
        )
        rows = _statement_rows(result)
    _require(
        len(rows) == 1
        and len(rows[0]) == 1
        and _canonical_temporal_evidence(rows[0][0])
        == _canonical_temporal_evidence(_FIXED_MOMENT),
        "gate_board_meta_bootstrap_timestamp_not_frozen",
    )


def _structural_placeholder(name: str, *, entry_id: str) -> str:
    # A structural hole is a production-controlled identifier, not a free
    # parameter.  Pick one real endpoint layout for the few templates whose
    # direction gives the same placeholder name a different endpoint role.
    if entry_id == "I12" and name == "node_type":
        return "Alternative"
    if entry_id in {"I46", "I47"}:
        if name == "node_type":
            return "Entity"
        if name == "target_type":
            return "Decision"
    if name in {"edge_type", "rel_name"}:
        return "belongs_to"
    if name in {"to_t", "to_type", "target_type"}:
        return "Entity"
    if name in {
        "from_t",
        "from_type",
        "node_type",
        "nt",
        "ntype",
        "record.node_type",
    }:
        return "Decision"
    raise RealGateBackendError(f"pulse_corpus_placeholder_unmapped:{name}")


def _render_statement(entry: Mapping[str, Any]) -> str:
    statement = entry.get("template")
    _require(
        type(statement) is str and bool(statement.strip()), "pulse_template_invalid"
    )
    entry_id = str(entry.get("id") or "")
    placeholders = entry.get("placeholders", ())
    _require(isinstance(placeholders, (list, tuple)), "pulse_placeholders_invalid")
    for raw_name in placeholders:
        _require(type(raw_name) is str and bool(raw_name), "pulse_placeholder_invalid")
        token = f"<<{raw_name}>>"
        _require(token in statement, f"pulse_placeholder_not_present:{raw_name}")
        statement = statement.replace(
            token,
            _structural_placeholder(raw_name, entry_id=entry_id),
        )
    _require("<<" not in statement and ">>" not in statement, "pulse_placeholder_open")
    return statement


def _parameter_value(name: str, *, statement: str) -> Any:
    if name == "rows":
        return [
            {
                "base_score": 0.6,
                "id": "gate-node",
                "now": _FIXED_INSTANT,
                "score": 0.4,
            }
        ]
    if name == "governed_types":
        return ["card"]
    if name in {"include_code_traceability", "include_superseded"}:
        return False
    if name in {"limit", "max_rows", "scan_limit"}:
        return 10
    if name == "delta":
        return 2
    if name in {
        "base_score",
        "boost",
        "conf",
        "default_conf",
        "deleted_relevance",
        "min_confidence",
        "min_relevance",
        "penalty",
        "relevance_score",
        "score",
    }:
        return 0.4
    if name == "cursor_ts":
        return _FIXED_MOMENT
    if name in {"cutoff", "ts"}:
        return _FIXED_INSTANT
    if name == "now":
        return _FIXED_MOMENT if "superseded_at = $now" in statement else _FIXED_INSTANT
    if name in {
        "after_id",
        "after_type",
        "cursor",
        "cursor_id",
    }:
        return ""
    if name == "owner_type":
        return "card"
    if name == "node_type":
        return "Decision"
    if name == "owner_id":
        return "gate-owner"
    if name in {"tgt"}:
        return "gate-target"
    if name in {
        "artifact_id",
        "constraint_id",
        "decision_id",
        "dup",
        "id",
        "nid",
        "node_id",
        "root_node_id",
        "src",
    }:
        return "gate-node"
    values = {
        "area": "gate",
        "c": "canonical",
        "canonical": "canonical",
        "created_by": "mpulse7-acceptance",
        "erased_text": "[erased]",
        "fallback_reason": "",
        "graph_layer": "canonical",
        "layer": "deterministic",
        "maturity_status": "working",
        "reason": "gate-reason",
        "ref": "pulse:card:gate-owner",
        "revocation_reason": "gate-revoked",
        "rule_id": "belongs_to/gate@v1",
        "source_deleted": "source_deleted",
        "source_ref": "pulse:card:gate-owner",
        "topic": "gate",
        "value": "gate-node",
        "w": "working",
        "working": "working",
        "working_stale": "working_stale",
    }
    if name in values:
        return values[name]
    raise RealGateBackendError(f"pulse_corpus_parameter_unmapped:{name}")


def _statement_params(statement: str) -> dict[str, Any]:
    names = sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", statement)))
    return {name: _parameter_value(name, statement=statement) for name in names}


def _authenticated_generic_error(
    entry: Mapping[str, Any], failure: BaseException
) -> tuple[str, str]:
    expected = entry.get("expected")
    expected_error = expected.get("error") if type(expected) is dict else None
    _require(
        type(expected_error) is dict,
        "generic_gap_failed_without_frozen_error_contract",
    )
    expected_code = expected_error.get("code")
    expected_type = expected_error.get("type")
    _require(
        type(expected_code) is str
        and bool(expected_code)
        and type(expected_type) is str
        and bool(expected_type),
        "generic_gap_frozen_error_contract_invalid",
    )

    current: BaseException | None = failure
    visited: set[int] = set()
    for _depth in range(8):
        if current is None or id(current) in visited:
            break
        visited.add(id(current))
        details = getattr(current, "details", None)
        if isinstance(details, Mapping):
            observed_code = details.get("backend_error_code")
            observed_type = details.get("backend_error_type")
            if observed_code == expected_code and observed_type == expected_type:
                return observed_code, observed_type
        observed_code = getattr(current, "code", None)
        observed_type = type(current).__name__
        if observed_code == expected_code and observed_type == expected_type:
            return observed_code, observed_type
        current = current.__cause__ or current.__context__
    raise RealGateBackendError(
        "generic_gap_error_did_not_match_frozen_backend_contract"
    ) from failure


class RealCommunityGateBackend:
    """One reopenable productive Community Board backend."""

    def __init__(
        self,
        context: object,
        *,
        storage_root: Path,
        settings: CommunitySettings,
        previous_registry: Any,
    ) -> None:
        self._context = context
        self._storage_root = storage_root
        self._settings = settings
        self._previous_registry = previous_registry
        self._backend = _context_text(context, "backend")
        self._board_id = _context_text(context, "board_id")
        self._closed = False
        self._composition: Any = None
        self._routed: Any = None
        self._board: Any = None
        self.semantic_store: Any = None
        self.graph_transaction: Any = None

    async def initialize(self) -> RealCommunityGateBackend:
        try:
            await self._compose_and_open(initialize_if_missing=True)
            return self
        except BaseException:
            restore_registry_state_for_tests(self._previous_registry)
            raise

    async def _compose_and_open(self, *, initialize_if_missing: bool) -> None:
        configure_settings(self._settings)
        composition = build_community_kg_composition(
            upload_dir=self._settings.upload_dir,
            settings=self._settings,
        )
        registry = composition.base_registry
        registry.config = self._settings
        restore_registry_state_for_tests(registry)
        routed = composition.routed_graph
        _require(routed is not None, "community_routed_graph_bundle_missing")
        board = routed.board

        try:
            binding = board.binding_store.inspect_board_binding(self._board_id)
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") != "binding_missing":
                raise
            binding = None
        if binding is None:
            _require(initialize_if_missing, "persisted_board_binding_missing")
            await routed.graph_schema_manager.ensure_bootstrapped(self._board_id)
            binding = board.binding_store.acquire_board_binding(self._board_id)
            await _freeze_gate_bootstrap_timestamp(
                board.graph_transaction,
                self._board_id,
            )
        else:
            _require(
                binding.backend == self._backend,
                "persisted_board_backend_differs_from_factory",
            )
            snapshot = board.resolver.acquire_board_route(self._board_id)
            _require(
                snapshot.backend == self._backend, "resolved_board_backend_mismatch"
            )
            opened = await board.graph_lifecycle.open(self._board_id)
            _require(opened.opened is True, "persisted_board_reopen_failed")

        _require(binding.backend == self._backend, "initialized_board_backend_mismatch")
        validation = await routed.graph_schema_manager.validate(self._board_id)
        _require(validation.valid is True, "community_board_schema_validation_failed")

        self._composition = composition
        self._routed = routed
        self._board = board
        self.semantic_store = board.graph_store
        self.graph_transaction = board.graph_transaction

    def _binding(self) -> Any:
        _require(not self._closed, "gate_backend_is_closed")
        binding = self._board.binding_store.acquire_board_binding(self._board_id)
        _require(binding.backend == self._backend, "active_board_backend_changed")
        return binding

    def _observe(self) -> PhysicalFingerprintObservation:
        binding = self._binding()
        if self._backend == "ladybug":
            # A Ladybug Database evicted by the bounded multi-Board cache is
            # cold again.  The public BoardConnection performs the productive
            # warm-open (including LOAD VECTOR) under the writer/close guards;
            # the logical source then validates the exact fixed snapshot.
            with kg_runtime.open_board_connection(self._board_id) as opened:
                database, _connection = opened
                snapshot = make_ladybug_logical_source(
                    database,
                    scope=SCOPE_BOARD,
                ).open_snapshot()
                try:
                    return _observe_snapshot(snapshot)
                finally:
                    snapshot.close()

        _require(binding.page_size is not None, "grafx_binding_page_size_missing")
        temporary_parent = self._storage_root / "logical-snapshot-temp"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with self._board.grafx_pool.acquire(
            binding.physical_path,
            page_size=binding.page_size,
        ) as lease:
            source = make_grafx_logical_source(
                lease.database,
                scope=SCOPE_BOARD,
                scan_batch_size=_BATCH_SIZE,
                temporary_parent=temporary_parent,
            )
            snapshot = source.open_snapshot()
            try:
                return _observe_snapshot(snapshot)
            finally:
                snapshot.close()

    def identity(self) -> dict[str, Any]:
        binding = self._binding()
        distribution = "ladybug" if self._backend == "ladybug" else "okto-grafx"
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as failure:
            raise RealGateBackendError(
                f"backend_distribution_not_installed:{distribution}"
            ) from failure
        return {
            "backend": self._backend,
            "backend_version": version,
            "generation": binding.generation,
            "storage_identity": _canonical_sha256(
                {
                    "backend": self._backend,
                    "binding_sha256": binding.binding_sha256,
                    "generation": binding.generation,
                    "run_id_sha256": hashlib.sha256(
                        _context_text(self._context, "run_id").encode("utf-8")
                    ).hexdigest(),
                }
            ),
        }

    def observe_fingerprints(self) -> dict[str, str]:
        """Return both named digests derived from the same physical snapshot."""

        observed = self._observe()
        return {
            "logical_graph_sha256": observed.logical_graph_sha256,
            "trace_model_sha256": observed.trace_model_sha256,
        }

    async def _verify_all(self) -> dict[str, Any]:
        validation = await self._routed.graph_schema_manager.validate(self._board_id)
        _require(validation.valid is True, "community_board_schema_validation_failed")
        if self._backend == "grafx":
            binding = self._binding()
            _require(binding.page_size is not None, "grafx_binding_page_size_missing")
            with self._board.grafx_pool.acquire(
                binding.physical_path,
                page_size=binding.page_size,
            ) as lease:
                report = lease.database.verify("all")
                _require(report.scope == "all", "grafx_verify_scope_mismatch")
                _require(report.clean is True, "grafx_verify_all_failed")
                return {
                    "engine": "okto-grafx",
                    "pages_checked": report.pages_checked,
                    "records_checked": report.records_checked,
                    "index_entries_checked": report.index_entries_checked,
                }
        # Ladybug 0.16 has no verify("all") API.  The M-PULSE-5 source has just
        # validated every physical table/column/endpoint/index and the caller
        # consumes every node and relation below.  Name that proof precisely.
        health = kg_runtime.verify_kuzu_db_health(self._board_id)
        _require(health.get("ok") is True, "ladybug_full_health_scan_failed")
        return {
            "engine": "ladybug-m-pulse-5-schema-and-full-logical-scan",
            "node_count": int(health.get("node_count", 0)),
        }

    async def reopen_recover_verify_fingerprint(
        self,
        *,
        after_operations: int,
        verify_scope: str,
    ) -> dict[str, Any]:
        """Cold-recompose from the binding, recover, verify, and observe."""

        _require(type(after_operations) is int, "recovery_boundary_invalid")
        _require(verify_scope == "all", "recovery_verify_scope_must_be_all")
        await self._close_handles()
        await self._compose_and_open(initialize_if_missing=False)
        recovery = await self._board.graph_recovery.recover_wal_only(self._board_id)
        _require(
            recovery.status in {"recovered", "skipped"}
            and recovery.main_untouched is True,
            "backend_wal_recovery_failed",
        )
        reopened = await self._board.graph_lifecycle.open(self._board_id)
        _require(reopened.opened is True, "backend_post_recovery_reopen_failed")
        await self._verify_all()
        identity = self.identity()
        observed = self._observe()
        return {
            "after_operations": after_operations,
            "closed": True,
            "reopened": True,
            "recovered": True,
            "verify_ok": True,
            "verify_scope": verify_scope,
            "storage_identity": identity["storage_identity"],
            "generation": identity["generation"],
            "fingerprint_trace_model_sha256": observed.trace_model_sha256,
            "fingerprint_logical_graph_sha256": observed.logical_graph_sha256,
        }

    async def _ensure_auxiliary_board(self, purpose: str) -> str:
        scratch = (
            "acceptance-read-scratch"
            if purpose == "corpus-read-empty"
            else "acceptance-mutation-scratch"
        )
        board_id = _safe_case_board_id(self._board_id, scratch)
        await self._routed.graph_schema_manager.ensure_bootstrapped(board_id)
        binding = self._board.binding_store.acquire_board_binding(board_id)
        _require(binding.backend == self._backend, "auxiliary_board_backend_mismatch")
        validation = await self._routed.graph_schema_manager.validate(board_id)
        _require(validation.valid is True, "auxiliary_board_schema_invalid")
        return board_id

    async def _cleanup_nodes(
        self,
        board_id: str,
        identities: tuple[tuple[str, str], ...],
    ) -> None:
        async with await self.graph_transaction.begin(board_id) as scope:
            for node_type, node_id in identities:
                scope.execute(
                    f"MATCH (n:{node_type} {{id: $id}}) DETACH DELETE n",
                    {"id": node_id},
                )
        async with await self.graph_transaction.begin(board_id) as scope:
            for node_type, node_id in identities:
                _require(
                    node_type not in scope.find_node_types(node_id),
                    "auxiliary_scratch_cleanup_failed",
                )

    @staticmethod
    def _seed_node_attrs(family_id: str) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "content": "gate-original-content",
            "context": "gate-original-context",
            "created_at": "2025-01-01T00:00:00Z",
            "created_by_agent": "system:mpulse7-acceptance",
            "graph_layer": "working",
            "justification": "gate-original-justification",
            "last_queried_at": "2025-01-01T00:00:00Z",
            "last_recomputed_at": "2025-01-01T00:00:00Z",
            "maturity_status": "working",
            "pre_cancellation_relevance_score": None,
            "priority_boost": 0.1,
            "query_hits": 1,
            "relevance_score": 0.8,
            "revocation_reason": None if family_id == "I06" else "",
            "source_artifact_ref": "card:gate-owner",
            "source_content_hash": "gate-original-hash",
            "source_span_quote": "gate-original-quote",
            "superseded_at": None,
            "superseded_by": None,
            "title": "gate-original-title",
        }
        if family_id in {"I07", "I37"}:
            attrs.update(
                {
                    "pre_cancellation_relevance_score": 0.7,
                    "revocation_reason": "gate-reason",
                    "superseded_at": "2025-02-01T00:00:00Z",
                    "superseded_by": "gate-reason",
                }
            )
        return attrs

    @staticmethod
    def _gate_edge_attrs(
        *, rule_id: str, session_id: str = "mpulse7-seed"
    ) -> dict[str, Any]:
        return {
            "confidence": 0.75,
            "created_at": _FIXED_INSTANT,
            "created_by": "mpulse7-acceptance",
            "created_by_session_id": session_id,
            "fallback_reason": "",
            "layer": "deterministic",
            "rule_id": rule_id,
        }

    async def _seed_write_board(self, board_id: str, family_id: str) -> None:
        async with await self.graph_transaction.begin(board_id) as scope:
            if family_id == "I29":
                scope.create_node(
                    "Decision",
                    "gate-source",
                    self._seed_node_attrs(family_id),
                    source_session_id="mpulse7-seed",
                )
                scope.create_node(
                    "Entity",
                    "gate-node",
                    {"title": "gate-target"},
                    source_session_id="mpulse7-seed",
                )
                created = scope.create_edge(
                    "belongs_to",
                    "Decision",
                    "Entity",
                    "gate-source",
                    "gate-node",
                    self._gate_edge_attrs(rule_id="belongs_to/gate@v1"),
                )
                _require(created is True, "write_family_seed_edge_not_created")
                return

            scope.create_node(
                "Decision",
                "gate-node",
                self._seed_node_attrs(family_id),
                source_session_id="mpulse7-seed",
            )
            if family_id in {"I26", "I27"}:
                scope.create_node(
                    "Entity",
                    "gate-target",
                    {"title": "gate-target"},
                    source_session_id="mpulse7-seed",
                )
            if family_id == "I26":
                created = scope.create_edge(
                    "belongs_to",
                    "Decision",
                    "Entity",
                    "gate-node",
                    "gate-target",
                    self._gate_edge_attrs(rule_id="belongs_to/gate@v1"),
                )
                _require(created is True, "write_family_seed_edge_not_created")

    async def _execute_write_entry(
        self,
        entry: Mapping[str, Any],
        *,
        purpose: str,
    ) -> dict[str, Any]:
        family_id = str(entry.get("id") or "")
        _require(family_id in _WRITE_FAMILY_IDS, "pulse_write_family_not_frozen")
        board_id = await self._ensure_auxiliary_board(purpose)
        await self._seed_write_board(board_id, family_id)
        statement = _render_statement(entry)
        params = _statement_params(statement)

        before: Any = None
        properties = _PROPERTY_WRITE_FAMILIES.get(family_id)
        if properties is not None:
            async with await self.graph_transaction.begin(board_id) as scope:
                before = scope.snapshot_node_properties(
                    "Decision",
                    "gate-node",
                    properties,
                )
                _require(before is not None, "write_family_before_image_missing")

        async with await self.graph_transaction.begin(board_id) as scope:
            result = scope.execute(statement, params)
            _statement_rows(result)

        proof: dict[str, Any]
        async with await self.graph_transaction.begin(board_id) as scope:
            if properties is not None:
                after = scope.snapshot_node_properties(
                    "Decision",
                    "gate-node",
                    properties,
                )
                _require(after is not None, "write_family_after_image_missing")
                before_doc = _before_image_document(before)
                after_doc = _before_image_document(after)
                _require(before_doc != after_doc, "write_family_did_not_change_target")
                proof = {
                    "after_sha256": _canonical_sha256(after_doc),
                    "before_sha256": _canonical_sha256(before_doc),
                    "postcondition": "property_change_durable",
                }
            elif family_id in {"I13", "I30", "I38"}:
                present = "Decision" in scope.find_node_types("gate-node")
                _require(not present, "write_family_node_delete_not_durable")
                proof = {"postcondition": "node_absent"}
            elif family_id == "I26":
                present = scope.edge_exists(
                    "belongs_to",
                    "Decision",
                    "Entity",
                    "gate-node",
                    "gate-target",
                    "belongs_to/gate@v1",
                )
                _require(not present, "write_family_outbound_edge_delete_failed")
                proof = {"postcondition": "edge_absent"}
            elif family_id == "I29":
                present = scope.edge_exists(
                    "belongs_to",
                    "Decision",
                    "Entity",
                    "gate-source",
                    "gate-node",
                    "belongs_to/gate@v1",
                )
                _require(not present, "write_family_inbound_edge_delete_failed")
                proof = {"postcondition": "edge_absent"}
            elif family_id == "I27":
                present = scope.edge_exists(
                    "belongs_to",
                    "Decision",
                    "Entity",
                    "gate-node",
                    "gate-target",
                    "belongs_to/gate@v1",
                )
                _require(present, "write_family_edge_create_not_durable")
                proof = {"postcondition": "edge_present"}
            else:  # pragma: no cover - frozen set is partitioned above
                raise RealGateBackendError(
                    f"write_family_postcondition_unmapped:{family_id}"
                )
        await self._cleanup_nodes(
            board_id,
            (
                ("Decision", "gate-node"),
                ("Decision", "gate-source"),
                ("Entity", "gate-node"),
                ("Entity", "gate-target"),
            ),
        )
        return {"family": family_id, **proof}

    async def run_pulse_corpus_case(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        entry_id = str(entry.get("id") or "")
        entry_class = str(entry.get("class") or "")
        classification = str(entry.get("classification") or "")
        _require(entry_id != "", "pulse_corpus_id_invalid")
        _require(
            entry_class in {"fragment", "read", "write"},
            "pulse_corpus_class_invalid",
        )
        _require(
            classification
            in {"already_supported", "duplicate_text", "fragment", "generic_gap"},
            "pulse_corpus_classification_invalid",
        )
        if entry_class == "fragment":
            return {
                "id": entry_id,
                "class": entry_class,
                "classification": classification,
                "status": "not_executable",
                "result": {"outcome": "fragment"},
            }

        try:
            if entry_class == "write":
                effect = await self._execute_write_entry(
                    entry,
                    purpose=f"corpus-write:{entry_id}",
                )
                result: dict[str, Any] = {"outcome": "effect", "effect": effect}
            else:
                board_id = await self._ensure_auxiliary_board("corpus-read-empty")
                statement = _render_statement(entry)
                async with await self.graph_transaction.begin(board_id) as scope:
                    query_result = scope.execute(
                        statement,
                        _statement_params(statement),
                    )
                    rows = _statement_rows(query_result)
                result = {"outcome": "rows", "rows": rows}
        except Exception as failure:
            if classification != "generic_gap":
                raise
            error_code, error_type = _authenticated_generic_error(entry, failure)
            result = {
                "outcome": "error",
                "error_code": error_code,
                "error_type": error_type,
            }
        return {
            "id": entry_id,
            "class": entry_class,
            "classification": classification,
            "status": "executed",
            "result": result,
        }

    async def run_raw_execute_family(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        _require(type(entry) is dict, "raw_write_family_entry_invalid")
        family_id = str(entry.get("id") or "")
        _require(family_id in _WRITE_FAMILY_IDS, "raw_write_family_not_frozen")
        _require(
            entry.get("class") == "write"
            and entry.get("classification") == "already_supported",
            "raw_write_family_not_supported",
        )
        effect = await self._execute_write_entry(
            entry,
            purpose=f"raw-write:{family_id}",
        )
        return {"id": family_id, "status": "passed", "result": effect}

    async def _receipt_restore_node_properties(self, board_id: str) -> dict[str, Any]:
        properties = ("title", "content", "relevance_score")
        async with await self.graph_transaction.begin(board_id) as scope:
            scope.create_node(
                "Entity",
                "receipt-node",
                {
                    "content": "before-content",
                    "relevance_score": 0.6,
                    "title": "before-title",
                },
                source_session_id="receipt-session",
            )
        async with await self.graph_transaction.begin(board_id) as scope:
            before = scope.snapshot_node_properties(
                "Entity", "receipt-node", properties
            )
            _require(before is not None, "receipt_node_before_image_missing")
            scope.update_node(
                "Entity",
                "receipt-node",
                {
                    "content": "transient-content",
                    "relevance_score": 0.1,
                    "title": "transient-title",
                },
            )
            scope.restore_node_properties(before)
        async with await self.graph_transaction.begin(board_id) as scope:
            restored = scope.snapshot_node_properties(
                "Entity", "receipt-node", properties
            )
            _require(restored == before, "receipt_node_before_image_not_restored")
        return {
            "before_image_sha256": _canonical_sha256(_before_image_document(before)),
            "postcondition": "exact_before_image_restored",
        }

    async def _receipt_compensate_spec_lineage(self, board_id: str) -> dict[str, Any]:
        old_rule = "belongs_to/spec_to_ideation@mpulse7"
        new_rule = "belongs_to/spec_to_refinement@mpulse7"
        old_attrs = self._gate_edge_attrs(
            rule_id=old_rule,
            session_id="receipt-old-lineage",
        )
        async with await self.graph_transaction.begin(board_id) as scope:
            for node_id in ("receipt-spec", "receipt-old", "receipt-new"):
                scope.create_node(
                    "Entity",
                    node_id,
                    {"title": node_id},
                    source_session_id="receipt-lineage-seed",
                )
            created = scope.create_edge(
                "belongs_to",
                "Entity",
                "Entity",
                "receipt-spec",
                "receipt-old",
                old_attrs,
            )
            _require(created is True, "receipt_old_lineage_not_created")
        async with await self.graph_transaction.begin(board_id) as scope:
            receipt = scope.reconcile_spec_lineage_parent(
                "receipt-spec",
                "receipt-new",
                self._gate_edge_attrs(
                    rule_id=new_rule,
                    session_id="receipt-new-lineage",
                ),
            )
        _require(receipt.new_edge_created is True, "lineage_receipt_has_no_new_edge")
        _require(len(receipt.removed_edges) == 1, "lineage_receipt_has_no_before_image")
        async with await self.graph_transaction.begin(board_id) as scope:
            scope.compensate_spec_lineage_parent(receipt)
        async with await self.graph_transaction.begin(board_id) as scope:
            old_present = scope.edge_exists(
                "belongs_to",
                "Entity",
                "Entity",
                "receipt-spec",
                "receipt-old",
                old_rule,
            )
            new_present = scope.edge_exists(
                "belongs_to",
                "Entity",
                "Entity",
                "receipt-spec",
                "receipt-new",
                new_rule,
            )
            _require(old_present and not new_present, "lineage_receipt_not_compensated")
        return {
            "postcondition": "old_parent_restored_new_parent_absent",
            "removed_before_images": len(receipt.removed_edges),
        }

    async def _receipt_compensate_projection(self, board_id: str) -> dict[str, Any]:
        owner_id = "mpulse7-owner"
        owner_node_id = "receipt-owner"
        member_id = "receipt-member"
        source_ref = f"refinement:{owner_id}:rdl:ledger:decision"
        ownership_rule = "belongs_to/relational_rdl_decision@v2.0"
        async with await self.graph_transaction.begin(board_id) as scope:
            scope.create_node(
                "Entity",
                owner_node_id,
                {
                    "source_artifact_ref": f"refinement:{owner_id}",
                    "title": "receipt owner",
                },
                source_session_id="receipt-projection-seed",
            )
            scope.create_node(
                "Decision",
                member_id,
                {
                    "content": "projection content",
                    "created_by_agent": "system:mpulse7-acceptance",
                    "revocation_reason": "",
                    "source_artifact_ref": source_ref,
                    "title": "projection member",
                },
                source_session_id="receipt-projection-seed",
            )
            created = scope.create_edge(
                "belongs_to",
                "Decision",
                "Entity",
                member_id,
                owner_node_id,
                self._gate_edge_attrs(
                    rule_id=ownership_rule,
                    session_id="receipt-projection-seed",
                ),
            )
            _require(created is True, "receipt_projection_edge_not_created")
        async with await self.graph_transaction.begin(board_id) as scope:
            receipt = scope.reconcile_projection_active_set(
                ProjectionActiveSetIntent(
                    owner_type="refinement",
                    owner_id=owner_id,
                    namespace="rdl",
                    owner_node_id=owner_node_id,
                    active_nodes=(),
                )
            )
        _require(
            len(receipt.before_images) == 1, "projection_receipt_before_image_missing"
        )
        async with await self.graph_transaction.begin(board_id) as scope:
            scope.compensate_projection_active_set(receipt)
        async with await self.graph_transaction.begin(board_id) as scope:
            restored = scope.snapshot_node_properties(
                "Decision",
                member_id,
                ("revocation_reason", "source_artifact_ref", "content"),
            )
            _require(restored is not None, "projection_member_not_restored")
            _require(
                restored.attrs.get("revocation_reason") in {None, ""}
                and restored.attrs.get("source_artifact_ref") == source_ref,
                "projection_member_payload_not_restored",
            )
            edge_present = scope.edge_exists(
                "belongs_to",
                "Decision",
                "Entity",
                member_id,
                owner_node_id,
                ownership_rule,
            )
            _require(edge_present, "projection_member_edge_not_restored")
        return {
            "before_images": len(receipt.before_images),
            "postcondition": "projection_member_and_edge_restored",
        }

    async def _receipt_preserve_session_snapshot(self, board_id: str) -> dict[str, Any]:
        session_id = "receipt-owned-session"
        lineage_rule = "belongs_to/spec_to_ideation@mpulse7"
        generic_rule = "belongs_to/receipt_generic@v1"
        lineage_attrs = self._gate_edge_attrs(
            rule_id=lineage_rule,
            session_id=session_id,
        )
        async with await self.graph_transaction.begin(board_id) as scope:
            for node_id in ("receipt-source", "receipt-parent", "receipt-generic"):
                scope.create_node(
                    "Entity",
                    node_id,
                    {"title": node_id},
                    source_session_id="receipt-preserve-seed",
                )
            _require(
                scope.create_edge(
                    "belongs_to",
                    "Entity",
                    "Entity",
                    "receipt-source",
                    "receipt-parent",
                    lineage_attrs,
                ),
                "preserved_lineage_seed_not_created",
            )
            _require(
                scope.create_edge(
                    "belongs_to",
                    "Entity",
                    "Entity",
                    "receipt-source",
                    "receipt-generic",
                    self._gate_edge_attrs(
                        rule_id=generic_rule,
                        session_id=session_id,
                    ),
                ),
                "generic_session_edge_seed_not_created",
            )
        snapshot = SpecLineageEdgeSnapshot(
            source_id="receipt-source",
            target_id="receipt-parent",
            rule_id=lineage_rule,
            attrs=dict(lineage_attrs),
        )
        async with await self.graph_transaction.begin(board_id) as scope:
            scope.delete_edges_by_session_preserving_spec_lineage(
                session_id,
                (snapshot,),
            )
        async with await self.graph_transaction.begin(board_id) as scope:
            lineage_present = scope.edge_exists(
                "belongs_to",
                "Entity",
                "Entity",
                "receipt-source",
                "receipt-parent",
                lineage_rule,
            )
            generic_present = scope.edge_exists(
                "belongs_to",
                "Entity",
                "Entity",
                "receipt-source",
                "receipt-generic",
                generic_rule,
            )
            _require(
                lineage_present and not generic_present,
                "session_snapshot_preservation_failed",
            )
        return {"postcondition": "exact_lineage_preserved_generic_removed"}

    async def run_receipt_bound_scenario(self, scenario_id: str) -> dict[str, Any]:
        methods = {
            "restore-node-properties-local-before-image": (
                self._receipt_restore_node_properties
            ),
            "compensate-spec-lineage-local-receipt": (
                self._receipt_compensate_spec_lineage
            ),
            "compensate-projection-local-receipt": (
                self._receipt_compensate_projection
            ),
            "session-delete-preserving-local-snapshots": (
                self._receipt_preserve_session_snapshot
            ),
        }
        method = methods.get(scenario_id)
        _require(method is not None, "receipt_bound_scenario_not_frozen")
        board_id = await self._ensure_auxiliary_board(f"receipt:{scenario_id}")
        result = await method(board_id)
        await self._cleanup_nodes(
            board_id,
            (
                ("Entity", "receipt-node"),
                ("Entity", "receipt-spec"),
                ("Entity", "receipt-old"),
                ("Entity", "receipt-new"),
                ("Entity", "receipt-owner"),
                ("Decision", "receipt-member"),
                ("Entity", "receipt-source"),
                ("Entity", "receipt-parent"),
                ("Entity", "receipt-generic"),
            ),
        )
        return {"id": scenario_id, "status": "passed", "result": result}

    async def run_crash_point(self, point: Mapping[str, Any]) -> dict[str, Any]:
        """Delegate the independent rollout crash harness without runner coupling."""

        module = importlib.import_module("mpulse7_crash_harness")
        handler = getattr(module, "run_crash_point", None)
        _require(callable(handler), "crash_harness_entrypoint_missing")
        value = handler(self, dict(point))
        if hasattr(value, "__await__"):
            value = await value
        _require(type(value) is dict, "crash_harness_receipt_invalid")
        return value

    async def _close_handles(self) -> None:
        if self._board is None:
            return
        await self._board.graph_lifecycle.close(None)
        remaining = self._board.grafx_pool.close_all()
        _require(remaining >= 0, "grafx_pool_close_all_invalid")

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._close_handles()
        finally:
            self._closed = True
            restore_registry_state_for_tests(self._previous_registry)


def _settings(root: Path, backend: str) -> CommunitySettings:
    return CommunitySettings(
        _env_file=None,
        data_dir=str(root),
        upload_dir=str(root / "uploads"),
        kg_base_dir=str(root / "kg"),
        kg_graph_backend=backend,
        kg_global_graph_backend=backend,
        kg_grafx_page_size=8192,
        kg_embedding_mode="stub",
        kg_kuzu_max_db_size_gb=2,
    )


async def _factory(context: object, backend: str) -> RealCommunityGateBackend:
    observed_backend = _context_text(context, "backend")
    _require(observed_backend == backend, "factory_context_backend_mismatch")
    root = _storage_root(context)
    previous = capture_registry_state_for_tests()
    instance = RealCommunityGateBackend(
        context,
        storage_root=root,
        settings=_settings(root, backend),
        previous_registry=previous,
    )
    return await instance.initialize()


async def ladybug_factory(context: object) -> RealCommunityGateBackend:
    """Build/reopen the real Ladybug Community bundle for one gate context."""

    return await _factory(context, "ladybug")


async def grafx_factory(context: object) -> RealCommunityGateBackend:
    """Build/reopen the real Grafx Community bundle for one gate context."""

    return await _factory(context, "grafx")


__all__ = [
    "PhysicalFingerprintObservation",
    "RealCommunityGateBackend",
    "RealGateBackendError",
    "grafx_factory",
    "ladybug_factory",
]
