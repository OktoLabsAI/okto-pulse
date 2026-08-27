"""Frozen M-PULSE-3D contract for the 0.3.12 -> 0.5.0 Grafx rebuild.

The historical constants in this file are intentionally transcribed.  In
particular, source counts, columns, endpoint pairs, and fingerprints are not
derived from the implementation under test.  That keeps this suite capable of
detecting a coordinated drift in the adapter and the current manifest.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import FrozenInstanceError, dataclass, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from filelock import FileLock
import okto_grafx
import pytest
from okto_grafx import Database, Timestamp, VectorValue
from okto_grafx.domain.model import CATALOG_FORMAT_VERSION
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
    GraphLockContention,
)

from okto_pulse.community.adapters import grafx_schema_evolution as evolution
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    ensure_current_grafx_board_schema,
    validate_current_grafx_schema,
)


SOURCE_SCHEMA_FINGERPRINT = (
    "f4f9905b1012b98df6669117c0ab8feb926f763d7d4c26caf91cc0f138354717"
)
TARGET_SCHEMA_FINGERPRINT = (
    "4a7b425bf4b8c4864be633c1a87f034e5f7f641019dc029015b7d3ca786deb81"
)
GOLDEN_CODEC_FINGERPRINT = (
    "9d1123371dc1ed6009737f24bdf19a2a293fa7598585c3e45ba598ff64b6b175"
)
POPULATED_FIXTURE_FINGERPRINT = (
    "978aa7b26d0090aada97d72e425eec363ddc8fb07d6596e6cfd5120e1f69f670"
)

SOURCE_NODE_SPACES: tuple[tuple[str, str, bool], ...] = (
    ("Decision", "decision_embedding_idx", True),
    ("Criterion", "criterion_embedding_idx", True),
    ("Constraint", "constraint_embedding_idx", True),
    ("Assumption", "assumption_embedding_idx", False),
    ("Requirement", "requirement_embedding_idx", True),
    ("Entity", "entity_embedding_idx", True),
    ("APIContract", "apicontract_embedding_idx", True),
    ("TestScenario", "testscenario_embedding_idx", True),
    ("Bug", "bug_embedding_idx", True),
    ("Learning", "learning_embedding_idx", True),
    ("Alternative", "alternative_embedding_idx", False),
)

SOURCE_NODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "STRING"),
    ("title", "STRING"),
    ("content", "STRING"),
    ("context", "STRING"),
    ("justification", "STRING"),
    ("source_artifact_ref", "STRING"),
    ("graph_layer", "STRING"),
    ("maturity_status", "STRING"),
    ("source_session_id", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("created_by_agent", "STRING"),
    ("source_confidence", "DOUBLE"),
    ("relevance_score", "DOUBLE"),
    ("pre_cancellation_relevance_score", "DOUBLE"),
    ("query_hits", "INT64"),
    ("last_queried_at", "STRING"),
    ("last_recomputed_at", "STRING"),
    ("priority_boost", "DOUBLE"),
    ("superseded_by", "STRING"),
    ("superseded_at", "TIMESTAMP"),
    ("revocation_reason", "STRING"),
    ("human_curated", "BOOLEAN"),
    ("generation", "INT64"),
    ("source_span_start", "INT64"),
    ("source_span_end", "INT64"),
    ("source_span_quote", "STRING"),
    ("extraction_model_id", "STRING"),
    ("extraction_prompt_hash", "STRING"),
    ("source_content_hash", "STRING"),
    ("attestation_count", "INT64"),
    ("last_attested_at", "TIMESTAMP"),
    ("kind_of", "STRING"),
    ("embedding", "DOUBLE[384]"),
)

RELATIONSHIP_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("confidence", "DOUBLE"),
    ("created_by_session_id", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("layer", "STRING"),
    ("rule_id", "STRING"),
    ("created_by", "STRING"),
    ("fallback_reason", "STRING"),
)

# Core 24a7aa4 relationship authority in its exact logical/endpoint order.
SOURCE_RELATIONSHIP_TRIPLES: tuple[tuple[str, str, str], ...] = (
    ("supersedes", "Decision", "Decision"),
    ("contradicts", "Decision", "Decision"),
    ("derives_from", "Decision", "Requirement"),
    ("relates_to", "Decision", "Alternative"),
    ("mentions", "Decision", "Entity"),
    ("depends_on", "Decision", "Decision"),
    ("violates", "Bug", "Constraint"),
    ("implements", "APIContract", "Requirement"),
    ("tests", "TestScenario", "Criterion"),
    ("validates", "Learning", "Bug"),
    ("implements", "APIContract", "Constraint"),
    ("supersedes", "Criterion", "Criterion"),
    ("supersedes", "Constraint", "Constraint"),
    ("supersedes", "Assumption", "Assumption"),
    ("supersedes", "Requirement", "Requirement"),
    ("supersedes", "Entity", "Entity"),
    ("supersedes", "APIContract", "APIContract"),
    ("supersedes", "TestScenario", "TestScenario"),
    ("supersedes", "Bug", "Bug"),
    ("supersedes", "Learning", "Learning"),
    ("supersedes", "Alternative", "Alternative"),
    ("relates_to", "Decision", "Entity"),
    ("relates_to", "Decision", "Bug"),
    ("relates_to", "Learning", "Entity"),
    ("relates_to", "Learning", "Decision"),
    ("relates_to", "Learning", "Requirement"),
    ("relates_to", "Learning", "Constraint"),
    ("relates_to", "Learning", "TestScenario"),
    ("relates_to", "Learning", "APIContract"),
    ("relates_to", "Learning", "Criterion"),
    ("relates_to", "Alternative", "Entity"),
    ("relates_to", "Alternative", "Decision"),
    ("relates_to", "Alternative", "Requirement"),
    ("relates_to", "Alternative", "Constraint"),
    ("relates_to", "Alternative", "TestScenario"),
    ("relates_to", "Alternative", "APIContract"),
    ("relates_to", "Alternative", "Criterion"),
    ("relates_to", "Assumption", "Entity"),
    ("relates_to", "Assumption", "Decision"),
    ("relates_to", "Assumption", "Requirement"),
    ("relates_to", "Assumption", "Constraint"),
    ("relates_to", "Assumption", "TestScenario"),
    ("relates_to", "Assumption", "APIContract"),
    ("relates_to", "Assumption", "Criterion"),
    ("belongs_to", "Entity", "Entity"),
    ("belongs_to", "Entity", "Bug"),
    ("belongs_to", "Requirement", "Entity"),
    ("belongs_to", "Constraint", "Entity"),
    ("belongs_to", "Criterion", "Entity"),
    ("belongs_to", "TestScenario", "Entity"),
    ("belongs_to", "APIContract", "Entity"),
    ("belongs_to", "Decision", "Entity"),
    ("belongs_to", "Bug", "Entity"),
    ("belongs_to", "Alternative", "Entity"),
    ("belongs_to", "Assumption", "Entity"),
    ("belongs_to", "Learning", "Entity"),
    ("originates_from", "Bug", "Entity"),
    ("covered_by", "Bug", "Entity"),
    ("covered_by", "Bug", "TestScenario"),
)

INTRODUCED_NODE_PROPERTIES: tuple[str, ...] = (
    "investigation_receipt_id",
    "source_ref",
    "attestor_actor_id",
    "declared_revision",
    "workspace_state_id",
    "code_path",
    "symbol_qualified_name",
    "symbol_kind",
    "selector_kind",
    "selector_fingerprint",
    "resolution_state",
)

INTRODUCED_RELATIONSHIP_TABLES: tuple[str, ...] = (
    "precedes__Entity__Entity",
    "supports__Entity__Requirement",
    "supports__Entity__Constraint",
    "supports__Entity__Criterion",
    "supports__Entity__APIContract",
    "supports__Entity__Decision",
    "supports__Entity__TestScenario",
    "supports__Entity__Entity",
    "derives_from__Entity__Entity",
    "overlaps__Entity__Entity",
)

INTRODUCED_RELATIONSHIP_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("precedes__Entity__Entity", "Entity", "Entity"),
    ("supports__Entity__Requirement", "Entity", "Requirement"),
    ("supports__Entity__Constraint", "Entity", "Constraint"),
    ("supports__Entity__Criterion", "Entity", "Criterion"),
    ("supports__Entity__APIContract", "Entity", "APIContract"),
    ("supports__Entity__Decision", "Entity", "Decision"),
    ("supports__Entity__TestScenario", "Entity", "TestScenario"),
    ("supports__Entity__Entity", "Entity", "Entity"),
    ("derives_from__Entity__Entity", "Entity", "Entity"),
    ("overlaps__Entity__Entity", "Entity", "Entity"),
)


def _physical_name(triple: tuple[str, str, str]) -> str:
    logical, source, target = triple
    return f"{logical}__{source}__{target}"


SOURCE_RELATIONSHIP_TABLES = tuple(
    _physical_name(triple) for triple in SOURCE_RELATIONSHIP_TRIPLES
)

# Exact 0.5.0 physical order: ten additive tables are interleaved at the
# positions owned by the published Core logical relationship order.
TARGET_RELATIONSHIP_TABLES: tuple[str, ...] = (
    *SOURCE_RELATIONSHIP_TABLES[:10],
    INTRODUCED_RELATIONSHIP_TABLES[0],
    SOURCE_RELATIONSHIP_TABLES[10],
    *INTRODUCED_RELATIONSHIP_TABLES[1:],
    *SOURCE_RELATIONSHIP_TABLES[11:],
)

EXPECTED_NODE_COUNTS: tuple[tuple[str, int], ...] = tuple(
    (node, 2) for node, _space, _searchable in SOURCE_NODE_SPACES
)
EXPECTED_RELATIONSHIP_COUNTS: tuple[tuple[str, int], ...] = tuple(
    (
        table,
        4
        if table == "belongs_to__Entity__Entity"
        else 1
        if table in SOURCE_RELATIONSHIP_TABLES
        else 0,
    )
    for table in TARGET_RELATIONSHIP_TABLES
)


def _logical_column(
    name: str, data_type: str, *, nullable: bool, space: str | None = None
) -> dict[str, object]:
    return {
        "name": name,
        "type": "VECTOR" if data_type == "DOUBLE[384]" else data_type,
        "nullable": nullable,
        "space": space,
    }


def _literal_predecessor_descriptor() -> dict[str, object]:
    grouped: dict[str, list[list[str]]] = {}
    for logical, source, target in SOURCE_RELATIONSHIP_TRIPLES:
        grouped.setdefault(logical, []).append([source, target])
    relationship_columns = [
        _logical_column(name, data_type, nullable=True)
        for name, data_type in RELATIONSHIP_PROPERTIES
    ]
    return {
        "contract": "okto-pulse-board-schema",
        "schema_version": "0.3.12",
        "nodes": [
            {
                "name": node,
                "primary_key": "id",
                "columns": [
                    _logical_column(
                        name,
                        data_type,
                        nullable=name != "id",
                        space=space if data_type == "DOUBLE[384]" else None,
                    )
                    for name, data_type in SOURCE_NODE_COLUMNS
                ],
            }
            for node, space, _searchable in SOURCE_NODE_SPACES
        ],
        "board_meta": {
            "name": "BoardMeta",
            "primary_key": "board_id",
            "columns": [
                _logical_column("board_id", "STRING", nullable=False),
                _logical_column("schema_version", "STRING", nullable=True),
                _logical_column("bootstrapped_at", "TIMESTAMP", nullable=True),
                _logical_column("embedding_model", "STRING", nullable=True),
                _logical_column("embedding_dimension", "INT64", nullable=True),
            ],
        },
        "relationships": [
            {
                "name": logical,
                "endpoint_pairs": pairs,
                "columns": relationship_columns,
            }
            for logical, pairs in grouped.items()
        ],
        "spaces": [
            {
                "node_type": node,
                "name": space,
                "dimension": 384,
                "metric": "cosine",
                "normalized": False,
                "storage_dtype": "float64",
                "searchable": searchable,
            }
            for node, space, searchable in SOURCE_NODE_SPACES
        ],
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()


def _independent_digest(records: tuple[bytes, ...]) -> str:
    digest = hashlib.sha256(b"pulse-grafx-schema-rebuild/1\0")
    for record in records:
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
    return digest.hexdigest()


def _fixture_node_id(node: str, variant: str) -> str:
    return f"{node.casefold()}-{variant}"


def _fixture_vector_values(ordinal: int) -> tuple[float, ...]:
    return (
        float(ordinal + 1) / 16.0,
        -0.0,
        *(0.0 for _ in range(382)),
    )


def _fixture_node_values(
    node: str,
    ordinal: int,
    *,
    vector_space_ref: int | None,
) -> dict[str, object]:
    if vector_space_ref is None:
        values = {name: None for name, _data_type in SOURCE_NODE_COLUMNS}
        values["id"] = _fixture_node_id(node, "null")
        return values
    return {
        "id": _fixture_node_id(node, "vector"),
        "title": f"{node} title",
        "content": "" if ordinal % 2 == 0 else f"content-{ordinal}",
        "context": None if ordinal % 3 == 0 else f"context-{ordinal}",
        "justification": f"justification-{ordinal}",
        "source_artifact_ref": f"artifact-{ordinal}",
        "graph_layer": "" if ordinal == 0 else f"layer-{ordinal}",
        "maturity_status": "stable",
        "source_session_id": f"session-{ordinal}",
        "created_at": Timestamp(micros=1_000_000 + ordinal),
        "created_by_agent": "fixture-agent",
        "source_confidence": -0.0 if ordinal == 0 else ordinal / 10.0,
        "relevance_score": 0.5,
        "pre_cancellation_relevance_score": (None if ordinal % 2 == 0 else -2.25),
        "query_hits": ordinal - 5,
        "last_queried_at": "" if ordinal % 2 == 0 else f"query-{ordinal}",
        "last_recomputed_at": None,
        "priority_boost": -1.5,
        "superseded_by": None,
        "superseded_at": Timestamp(micros=2_000_000 + ordinal),
        "revocation_reason": "",
        "human_curated": ordinal % 2 == 0,
        "generation": ordinal,
        "source_span_start": 0,
        "source_span_end": ordinal + 1,
        "source_span_quote": f"quote-α-{ordinal}",
        "extraction_model_id": "fixture-model",
        "extraction_prompt_hash": f"prompt-{ordinal:02d}",
        "source_content_hash": f"content-{ordinal:02d}",
        "attestation_count": ordinal,
        "last_attested_at": Timestamp(micros=-(ordinal + 1)),
        "kind_of": f"kind-{ordinal}",
        "embedding": VectorValue(
            values=_fixture_vector_values(ordinal),
            space_ref=vector_space_ref,
            dtype="float64",
        ),
    }


@dataclass(frozen=True, slots=True)
class _FixtureRelationship:
    triple: tuple[str, str, str]
    from_key: str
    to_key: str
    properties: tuple[tuple[str, object], ...]

    @property
    def table(self) -> str:
        return _physical_name(self.triple)


def _fixture_relationship_properties(ordinal: int) -> tuple[tuple[str, object], ...]:
    values: tuple[object, ...] = (
        float(ordinal + 1) / 100.0,
        "" if ordinal == 0 else f"edge-session-{ordinal}",
        Timestamp(micros=3_000_000 + ordinal),
        None if ordinal % 2 else f"edge-layer-{ordinal}",
        f"rule-{ordinal}",
        "fixture-agent",
        "" if ordinal % 3 == 0 else None,
    )
    return tuple(
        (name, value)
        for (name, _data_type), value in zip(
            RELATIONSHIP_PROPERTIES, values, strict=True
        )
    )


def _fixture_relationships() -> tuple[_FixtureRelationship, ...]:
    relationships: list[_FixtureRelationship] = []
    for ordinal, triple in enumerate(SOURCE_RELATIONSHIP_TRIPLES):
        _logical, source, target = triple
        from_key = _fixture_node_id(source, "vector")
        to_key = _fixture_node_id(target, "vector")
        if triple == ("belongs_to", "Entity", "Entity"):
            to_key = _fixture_node_id("Entity", "null")
        base = _FixtureRelationship(
            triple,
            from_key,
            to_key,
            _fixture_relationship_properties(ordinal),
        )
        relationships.append(base)
        if triple == ("belongs_to", "Entity", "Entity"):
            # Exact duplicate, distinct parallel edge, and a self-loop.
            relationships.append(base)
            relationships.append(
                _FixtureRelationship(
                    triple,
                    from_key,
                    to_key,
                    (
                        ("confidence", -0.0),
                        ("created_by_session_id", "parallel-distinct"),
                        ("created_at", Timestamp(micros=4_000_000)),
                        ("layer", ""),
                        ("rule_id", "parallel-rule"),
                        ("created_by", "fixture-agent"),
                        ("fallback_reason", None),
                    ),
                )
            )
            relationships.append(
                _FixtureRelationship(
                    triple,
                    from_key,
                    from_key,
                    _fixture_relationship_properties(ordinal + 100),
                )
            )
    return tuple(relationships)


def _fixture_encode(value: object, *, space: str | None = None) -> list[object]:
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int64", str(value)]
    if type(value) is float:
        return ["float64", value.hex()]
    if type(value) is str:
        return ["string", value]
    if type(value) is Timestamp:
        return ["timestamp_us", str(value.micros)]
    if type(value) is VectorValue:
        return [
            "vector",
            space or "",
            value.dtype,
            [component.hex() for component in value.values],
        ]
    raise AssertionError(f"fixture contains unsupported {type(value).__name__}")


def _fixture_relationship_record(relationship: _FixtureRelationship) -> bytes:
    logical, source, target = relationship.triple
    return _canonical_json(
        [
            "rel",
            logical,
            source,
            target,
            _fixture_encode(relationship.from_key),
            _fixture_encode(relationship.to_key),
            [[name, _fixture_encode(value)] for name, value in relationship.properties],
        ]
    )


def _populated_fixture_fingerprint() -> str:
    records: list[bytes] = [
        _canonical_json(
            [
                "meta",
                _fixture_encode("board-1"),
                _fixture_encode("0.5.0"),
                _fixture_encode(Timestamp(micros=1)),
                _fixture_encode("fixture-model"),
                _fixture_encode(384),
            ]
        )
    ]
    target_columns = (
        *(name for name, _data_type in SOURCE_NODE_COLUMNS[:-1]),
        *INTRODUCED_NODE_PROPERTIES,
        "embedding",
    )
    for ordinal, (node, space, _searchable) in enumerate(SOURCE_NODE_SPACES):
        node_records: list[tuple[bytes, bytes]] = []
        for space_ref in (None, 1):
            values = _fixture_node_values(node, ordinal, vector_space_ref=space_ref)
            target_values = {
                **values,
                **{name: None for name in INTRODUCED_NODE_PROPERTIES},
            }
            encoded = _canonical_json(
                [
                    "node",
                    node,
                    [
                        [name, _fixture_encode(target_values[name], space=space)]
                        for name in target_columns
                    ],
                ]
            )
            key = _canonical_json(_fixture_encode(values["id"]))
            node_records.append((key, encoded))
        records.extend(encoded for _key, encoded in sorted(node_records))

    relationships_by_table: dict[str, list[bytes]] = {}
    for relationship in _fixture_relationships():
        relationships_by_table.setdefault(relationship.table, []).append(
            _fixture_relationship_record(relationship)
        )
    for table in TARGET_RELATIONSHIP_TABLES:
        records.extend(sorted(relationships_by_table.get(table, [])))
    return _independent_digest(tuple(records))


def _error_reason(captured: pytest.ExceptionInfo[GraphError]) -> str:
    return str(captured.value.details["reason"])


def test_policy_authorities_and_public_surface_are_frozen() -> None:
    specification = (
        Path(__file__).parents[1] / "docs" / "grafx-schema-evolution-0.3.12-to-0.5.0.md"
    ).read_text(encoding="utf-8")
    for authority in (
        "24a7aa47109f125212a4ddf90035681d48c4ac51",
        "de1f494003d4d95af5da8bdb8af99b6f816d42d0",
        "ab61b9a785f2018312fc91541a580877fd068bbb",
        "02418584b4716b6b6b9630ab2a082658087a2344",
        "715ad68193cb14263c02f08fd4ff9ff7921b9648",
        "7e126a7130090c00891f8d1d35bd44819afe7a7a",
        "fa9ab58",
    ):
        assert authority in specification
    assert "separate development lineage" in specification
    assert "out-of-place rebuild" in specification
    assert "never bound or activated" in specification
    assert CATALOG_FORMAT_VERSION == 1
    assert "rebuild_grafx_schema_candidate" not in okto_grafx.__all__
    assert "GrafxSchemaCandidateResult" not in okto_grafx.__all__

    signature = inspect.signature(evolution.rebuild_grafx_schema_candidate)
    assert tuple(signature.parameters) == ("source", "candidate_path", "batch_size")
    assert (
        signature.parameters["source"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert (
        signature.parameters["candidate_path"].kind
        is inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert signature.parameters["batch_size"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["batch_size"].default == 256
    assert evolution.__all__ == [
        "GrafxSchemaCandidateResult",
        "rebuild_grafx_schema_candidate",
    ]

    result_fields = tuple(
        field.name for field in fields(evolution.GrafxSchemaCandidateResult)
    )
    assert result_fields == (
        "source_schema_version",
        "target_schema_version",
        "source_schema_fingerprint",
        "target_schema_fingerprint",
        "source_snapshot_lsn",
        "logical_data_fingerprint",
        "node_row_counts",
        "relationship_row_counts",
        "candidate_database_uuid",
        "changed",
    )
    result = evolution.GrafxSchemaCandidateResult(
        "0.3.12",
        "0.5.0",
        SOURCE_SCHEMA_FINGERPRINT,
        TARGET_SCHEMA_FINGERPRINT,
        7,
        "a" * 64,
        (),
        (),
        b"u" * 16,
        True,
    )
    with pytest.raises(FrozenInstanceError):
        result.changed = False  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        result.extra = "not slotted"  # type: ignore[attr-defined]


def test_literal_predecessor_and_target_goldens_do_not_drift() -> None:
    descriptor = _literal_predecessor_descriptor()
    encoded = json.dumps(
        descriptor,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == SOURCE_SCHEMA_FINGERPRINT

    assert len(SOURCE_NODE_SPACES) == 11
    assert len(SOURCE_NODE_COLUMNS) == 33
    assert len(SOURCE_RELATIONSHIP_TRIPLES) == 59
    assert len(SOURCE_RELATIONSHIP_TABLES) == 59
    assert (
        len({logical for logical, _source, _target in SOURCE_RELATIONSHIP_TRIPLES})
        == 13
    )
    assert len(RELATIONSHIP_PROPERTIES) == 7
    assert len(INTRODUCED_NODE_PROPERTIES) == 11
    assert len(INTRODUCED_RELATIONSHIP_TABLES) == 10
    assert len(TARGET_RELATIONSHIP_TABLES) == 69
    assert len(set(TARGET_RELATIONSHIP_TABLES)) == 69
    assert len(EXPECTED_RELATIONSHIP_COUNTS) == 69
    assert sum(count for _table, count in EXPECTED_RELATIONSHIP_COUNTS) == 62

    assert evolution.SOURCE_SCHEMA_VERSION == "0.3.12"
    assert evolution.TARGET_SCHEMA_VERSION == "0.5.0"
    assert evolution.SOURCE_SCHEMA_FINGERPRINT == SOURCE_SCHEMA_FINGERPRINT
    assert evolution.TARGET_SCHEMA_FINGERPRINT == TARGET_SCHEMA_FINGERPRINT
    assert evolution._predecessor_descriptor() == descriptor
    assert tuple(table.name for table in evolution.PREDECESSOR_NODE_TABLES) == tuple(
        node for node, _space, _searchable in SOURCE_NODE_SPACES
    )
    assert all(len(table.columns) == 33 for table in evolution.PREDECESSOR_NODE_TABLES)
    assert (
        tuple(table.name for table in evolution.PREDECESSOR_RELATIONSHIP_TABLES)
        == SOURCE_RELATIONSHIP_TABLES
    )
    assert evolution.INTRODUCED_NODE_PROPERTIES == INTRODUCED_NODE_PROPERTIES
    assert evolution.INTRODUCED_RELATIONSHIP_TABLES == INTRODUCED_RELATIONSHIP_TABLES

    target = evolution.PULSE_GRAFX_SCHEMA_MANIFEST
    assert len(target.nodes) == 11
    assert all(len(table.columns) == 44 for table in target.nodes)
    assert len(target.board_meta.columns) == 5
    assert len(target.relationships) == 69
    assert tuple(table.name for table in target.relationships) == (
        TARGET_RELATIONSHIP_TABLES
    )
    assert len(target.tables) == 81
    assert len(target.spaces) == 11
    assert len(target.logical_descriptor["relationships"]) == 16
    assert target.logical_fingerprint == TARGET_SCHEMA_FINGERPRINT


def test_populated_fixture_has_an_independent_frozen_logical_fingerprint() -> None:
    assert len(_fixture_relationships()) == 62
    assert _populated_fixture_fingerprint() == POPULATED_FIXTURE_FINGERPRINT


def test_m3_v1_codec_matches_the_independent_published_vector() -> None:
    records = tuple(
        _canonical_json(record)
        for record in (
            [
                "meta",
                ["string", "board-α"],
                ["string", "0.5.0"],
                ["timestamp_us", "1"],
                ["null"],
                ["null"],
            ],
            [
                "node",
                "Entity",
                [
                    ["id", ["string", "e1"]],
                    ["flag", ["bool", True]],
                    ["count", ["int64", "-2"]],
                    ["score", ["float64", "0x1.0000000000000p-1"]],
                    ["when", ["timestamp_us", "7"]],
                    [
                        "embedding",
                        [
                            "vector",
                            "entity_embedding",
                            "float64",
                            ["0x1.0000000000000p+0", "-0x0.0p+0"],
                        ],
                    ],
                    ["missing", ["null"]],
                ],
            ],
            [
                "rel",
                "supports",
                "Entity",
                "Requirement",
                ["string", "e1"],
                ["string", "r1"],
                [
                    ["confidence", ["float64", "0x1.8000000000000p-1"]],
                    ["created_at", ["timestamp_us", "9"]],
                    ["note", ["null"]],
                ],
            ],
            [
                "rel",
                "supports",
                "Entity",
                "Requirement",
                ["string", "e1"],
                ["string", "r1"],
                [
                    ["confidence", ["float64", "0x1.8000000000000p-1"]],
                    ["created_at", ["timestamp_us", "9"]],
                    ["note", ["null"]],
                ],
            ],
        )
    )
    assert records[2] == records[3]
    assert _independent_digest(records) == GOLDEN_CODEC_FINGERPRINT
    assert evolution._digest(list(records)) == GOLDEN_CODEC_FINGERPRINT
    assert evolution._encode_value(-0.0) == ["float64", "-0x0.0p+0"]
    assert evolution._encode_value(Timestamp(micros=-7)) == ["timestamp_us", "-7"]
    with pytest.raises(GraphCapabilityUnavailable) as non_finite:
        evolution._encode_value(float("nan"))
    assert _error_reason(non_finite) == "non_finite_double"


@pytest.mark.parametrize("batch_size", (True, 1.0, 0, -1, 1025, 2**63))
def test_batch_size_refuses_every_non_exact_or_out_of_range_value(
    batch_size: object, tmp_path: Path
) -> None:
    source = okto_grafx.connect(":memory:")
    try:
        with pytest.raises(GraphCapabilityUnavailable) as captured:
            evolution.rebuild_grafx_schema_candidate(
                source,
                tmp_path / "candidate",
                batch_size=batch_size,  # type: ignore[arg-type]
            )
    finally:
        source.close()
    assert _error_reason(captured) == "invalid_rebuild_argument"
    assert captured.value.details["field"] == "batch_size"
    assert captured.value.details["phase"] == "arguments"


@pytest.mark.parametrize("batch_size", (1, 1024))
def test_batch_size_inclusive_bounds_reach_source_preflight(
    batch_size: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = okto_grafx.connect(":memory:")
    marker = evolution._divergence(
        "test_reached_source_preflight", phase="source_preflight_catalog"
    )

    def reached(*_args: object, **_kwargs: object) -> None:
        raise marker

    monkeypatch.setattr(evolution, "_require_catalog", reached)
    try:
        with pytest.raises(GraphCapabilityUnavailable) as captured:
            evolution.rebuild_grafx_schema_candidate(
                source, tmp_path / "candidate", batch_size=batch_size
            )
    finally:
        source.close()
    assert captured.value is marker


def test_invalid_public_values_fail_before_backend_or_candidate_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(GraphCapabilityUnavailable) as wrong_source:
        evolution.rebuild_grafx_schema_candidate(  # type: ignore[arg-type]
            object(), tmp_path / "candidate"
        )
    assert wrong_source.value.details["field"] == "source"

    source = okto_grafx.connect(":memory:")
    try:
        with pytest.raises(GraphCapabilityUnavailable) as memory_candidate:
            evolution.rebuild_grafx_schema_candidate(source, ":memory:")
        assert memory_candidate.value.details["field"] == "candidate_path"
        with pytest.raises(GraphCapabilityUnavailable) as wrong_path:
            evolution.rebuild_grafx_schema_candidate(  # type: ignore[arg-type]
                source, object()
            )
        assert wrong_path.value.details["field"] == "candidate_path"
    finally:
        source.close()


def _observed_table(table: Any) -> SimpleNamespace:
    return SimpleNamespace(
        name=table.name,
        kind=table.kind,
        primary_key=table.primary_key,
        from_table=table.from_table,
        to_table=table.to_table,
        table_id=getattr(table, "table_id", 1),
        columns=tuple(
            SimpleNamespace(
                name=column.name,
                type=SimpleNamespace(name=column.grafx_value_type),
                nullable=column.nullable,
                vector_space=column.vector_space,
            )
            for column in table.columns
        ),
    )


def _observed_space(space: Any, space_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        name=space.name,
        space_id=space_id,
        dimension=space.dimension,
        metric=SimpleNamespace(value=space.metric),
        normalized=space.normalized,
        storage_dtype=space.storage_dtype,
        state="active",
    )


def _catalog_candidate(
    tables: list[SimpleNamespace], spaces: list[SimpleNamespace]
) -> SimpleNamespace:
    catalog = SimpleNamespace(
        tables=lambda: tuple(tables),
        spaces=lambda: tuple(spaces),
    )
    return SimpleNamespace(catalog=SimpleNamespace(catalog=catalog))


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_table",
        "extra_table",
        "column_order",
        "column_type",
        "column_nullability",
        "relationship_endpoint",
        "missing_space",
        "extra_space",
        "space_definition",
    ),
)
def test_every_predecessor_catalog_divergence_is_refused(mutation: str) -> None:
    manifest = evolution.PULSE_GRAFX_SCHEMA_MANIFEST
    tables = [
        _observed_table(table)
        for table in (
            *evolution.PREDECESSOR_NODE_TABLES,
            manifest.board_meta,
            *evolution.PREDECESSOR_RELATIONSHIP_TABLES,
        )
    ]
    spaces = [
        _observed_space(space, position)
        for position, space in enumerate(manifest.spaces, start=1)
    ]
    if mutation == "missing_table":
        tables.pop()
    elif mutation == "extra_table":
        tables.append(SimpleNamespace(name="Unexpected"))
    elif mutation == "column_order":
        columns = list(tables[0].columns)
        columns[0], columns[1] = columns[1], columns[0]
        tables[0].columns = tuple(columns)
    elif mutation == "column_type":
        tables[0].columns[1].type = SimpleNamespace(name="INT64")
    elif mutation == "column_nullability":
        tables[0].columns[1].nullable = False
    elif mutation == "relationship_endpoint":
        first_relationship = 1 + len(evolution.PREDECESSOR_NODE_TABLES)
        tables[first_relationship].to_table = "Entity"
    elif mutation == "missing_space":
        spaces.pop()
    elif mutation == "extra_space":
        spaces.append(SimpleNamespace(name="unexpected_space"))
    elif mutation == "space_definition":
        spaces[0].dimension = 383

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        evolution._require_catalog(
            _catalog_candidate(tables, spaces),  # type: ignore[arg-type]
            evolution.PREDECESSOR_NODE_TABLES,
            evolution.PREDECESSOR_RELATIONSHIP_TABLES,
            reason="source_catalog_not_predecessor",
            phase="source_preflight_catalog",
        )
    assert _error_reason(captured) == "source_catalog_not_predecessor"


def test_bad_source_catalog_never_creates_or_opens_candidate(tmp_path: Path) -> None:
    source = okto_grafx.connect(":memory:")
    candidate = tmp_path / "must-not-exist"
    try:
        with pytest.raises(GraphCapabilityUnavailable) as captured:
            evolution.rebuild_grafx_schema_candidate(source, candidate)
    finally:
        source.close()
    assert _error_reason(captured) == "source_catalog_not_predecessor"
    assert not candidate.exists()


def _index_candidate() -> tuple[SimpleNamespace, list[Any], list[Any]]:
    manifest = evolution.PULSE_GRAFX_SCHEMA_MANIFEST
    catalog_tables = [
        SimpleNamespace(name=table.name, table_id=position)
        for position, table in enumerate(manifest.tables, start=1)
    ]
    table_ids = {table.name: table.table_id for table in catalog_tables}
    catalog_spaces = [
        SimpleNamespace(name=space.name, space_id=position)
        for position, space in enumerate(manifest.spaces, start=101)
    ]
    space_ids = {space.name: space.space_id for space in catalog_spaces}
    registered: list[Any] = []

    def add_index(
        name: str,
        table_name: str,
        positions: tuple[int, ...],
        visibility: str,
        key_derivation: str,
    ) -> None:
        file = f"index/{name}.idx"
        enum = SimpleNamespace(value=visibility)
        definition = SimpleNamespace(
            name=name,
            file=file,
            table_id=table_ids[table_name],
            table_name=table_name,
            positions=positions,
            visibility=enum,
            bucket_count=64,
            key_derivation=key_derivation,
        )
        registered.append(
            SimpleNamespace(
                name=name,
                file=file,
                visibility=enum,
                definition=definition,
                stale=False,
                stale_reason=None,
                missing_targets=0,
            )
        )

    for table in (manifest.board_meta, *manifest.nodes):
        primary_position = (
            next(
                index
                for index, column in enumerate(table.columns)
                if column.name == "id"
            )
            if table.name != "BoardMeta"
            else 0
        )
        add_index(
            f"pk_{table.name}",
            table.name,
            (primary_position,),
            "exact",
            "columns",
        )
    for table in manifest.relationships:
        add_index(f"ef_{table.name}", table.name, (0,), "exact", "columns")
        add_index(f"et_{table.name}", table.name, (1,), "exact", "columns")
    vectors: list[Any] = []
    for table, space in zip(manifest.nodes, manifest.spaces, strict=True):
        position = next(
            index
            for index, column in enumerate(table.columns)
            if column.name == "embedding"
        )
        name = f"vector_{table.name}_{space.name}"
        add_index(name, table.name, (position,), "proximity", "vector_digest_v1")
        vectors.append(
            SimpleNamespace(
                name=name,
                file=f"index/{name}.idx",
                space_id=space_ids[space.name],
                space_name=space.name,
                dimension=space.dimension,
                metric_of_space=SimpleNamespace(value=space.metric),
                storage_dtype=space.storage_dtype,
                stale=False,
                stale_reason=None,
            )
        )

    catalog = SimpleNamespace(
        tables=lambda: tuple(catalog_tables),
        spaces=lambda: tuple(catalog_spaces),
    )
    candidate = SimpleNamespace(
        unindexed_tables=(),
        stale_indexes=(),
        catalog=SimpleNamespace(catalog=catalog),
        indexes=SimpleNamespace(indexes=lambda: tuple(registered)),
        vectors=SimpleNamespace(indexes=lambda: tuple(vectors)),
    )
    return candidate, registered, vectors


def test_exact_index_and_vector_inventory_is_accepted() -> None:
    candidate, registered, vectors = _index_candidate()
    assert len(registered) == 161
    assert len(vectors) == 11
    evolution._require_indexes(candidate, "test")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("same_total_substitution", "candidate_index_inventory_test"),
        ("duplicate_name", "candidate_duplicate_index_name_test"),
        ("wrong_position", "candidate_index_definition_test"),
        ("missing_target", "candidate_index_coverage_test"),
        ("vector_name", "candidate_vector_index_definition_test"),
        ("vector_metric", "candidate_vector_index_definition_test"),
    ),
)
def test_same_total_index_mutants_are_refused(mutation: str, reason: str) -> None:
    candidate, registered, vectors = _index_candidate()
    if mutation == "same_total_substitution":
        registered[0].name = "pk_Substituted"
        registered[0].definition.name = "pk_Substituted"
    elif mutation == "duplicate_name":
        registered[1].name = registered[0].name
    elif mutation == "wrong_position":
        registered[0].definition.positions = (1,)
    elif mutation == "missing_target":
        registered[0].missing_targets = 1
    elif mutation == "vector_name":
        vectors[0].name = "vector_wrong"
    elif mutation == "vector_metric":
        vectors[0].metric_of_space = SimpleNamespace(value="euclidean")

    with pytest.raises(GraphCapabilityUnavailable) as captured:
        evolution._require_indexes(candidate, "test")  # type: ignore[arg-type]
    assert _error_reason(captured) == reason


class _Transaction:
    def __init__(
        self,
        *,
        report: Any | None = None,
        commit_failure: BaseException | None = None,
        active_after_commit_failure: bool = False,
    ) -> None:
        self.active = True
        self.report = report or SimpleNamespace(durable=True, wrote=True)
        self.commit_failure = commit_failure
        self.active_after_commit_failure = active_after_commit_failure
        self.rolled_back = False

    def execute(self, _text: str, parameters: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            rows=((parameters["board_id"],),),
            statistics={"rows_updated": 1, "properties_set": 1},
        )

    def commit(self) -> Any:
        if self.commit_failure is not None:
            self.active = self.active_after_commit_failure
            raise self.commit_failure
        self.active = False
        return self.report

    def rollback(self) -> None:
        self.rolled_back = True
        self.active = False


def _snapshot() -> evolution._Snapshot:
    meta = evolution._BoardMeta(
        board_id="board-test",
        schema_version="0.3.12",
        bootstrapped_at=Timestamp(micros=1),
        embedding_model=None,
        embedding_dimension=None,
    )
    return evolution._Snapshot(
        meta=meta,
        node_plans=(),
        relationship_plans=(),
        node_counts=tuple(
            (node, 0) for node, _space, _searchable in SOURCE_NODE_SPACES
        ),
        relationship_counts=tuple((table, 0) for table in SOURCE_RELATIONSHIP_TABLES),
        fingerprint="d" * 64,
    )


@pytest.mark.parametrize("durable,wrote", ((False, True), (True, False)))
def test_terminal_invalid_commit_report_is_ambiguous(
    durable: bool,
    wrote: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _Transaction(report=SimpleNamespace(durable=durable, wrote=wrote))
    candidate = SimpleNamespace(begin=lambda _mode: transaction)
    monkeypatch.setattr(evolution, "_certify", lambda *_args, **_kwargs: ())

    with pytest.raises(evolution._StampOutcomeAmbiguous) as captured:
        evolution._rescan_and_stamp(candidate, 1, _snapshot())
    assert isinstance(captured.value.__cause__, GraphCapabilityUnavailable)
    assert (
        captured.value.__cause__.details["phase"] == "candidate_terminal_commit_report"
    )


def test_terminal_commit_exception_is_ambiguous_only_after_lifecycle_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evolution, "_certify", lambda *_args, **_kwargs: ())
    post_barrier = RuntimeError("commit result lost")
    inactive = _Transaction(
        commit_failure=post_barrier,
        active_after_commit_failure=False,
    )
    with pytest.raises(evolution._StampOutcomeAmbiguous) as ambiguous:
        evolution._rescan_and_stamp(
            SimpleNamespace(begin=lambda _mode: inactive), 1, _snapshot()
        )
    assert isinstance(ambiguous.value.__cause__, GraphError)
    assert ambiguous.value.__cause__.details["backend_error_type"] == "RuntimeError"
    assert ambiguous.value.__cause__.details["phase"] == "candidate_terminal_commit"
    assert inactive.rolled_back is False

    pre_barrier = RuntimeError("commit refused")
    active = _Transaction(
        commit_failure=pre_barrier,
        active_after_commit_failure=True,
    )
    with pytest.raises(GraphError) as refused:
        evolution._rescan_and_stamp(
            SimpleNamespace(begin=lambda _mode: active), 1, _snapshot()
        )
    assert refused.value.__cause__ is pre_barrier
    assert refused.value.details["phase"] == "candidate_terminal_commit"
    assert active.rolled_back is True


def test_terminal_rescan_failure_preserves_primary_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _Transaction()
    primary = evolution._divergence("terminal_rescan_failed", phase="terminal")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(evolution, "_certify", refuse)
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        evolution._rescan_and_stamp(
            SimpleNamespace(begin=lambda _mode: transaction), 1, _snapshot()
        )
    assert captured.value is primary
    assert transaction.rolled_back is True


def test_cleanup_failures_never_replace_the_primary_failure() -> None:
    primary = evolution._divergence("primary", phase="test")

    class BrokenClose:
        def close(self) -> None:
            raise OSError("close failed")

    assert evolution._closed(BrokenClose(), "test", primary) is False  # type: ignore[arg-type]
    assert any(
        "closing the test handle also failed" in note for note in primary.__notes__
    )

    class BrokenRollbackTransaction:
        active = True

        def execute(self, _text: str, _parameters: dict[str, Any]) -> None:
            raise primary

        def rollback(self) -> None:
            raise OSError("rollback failed")

    handle = SimpleNamespace(begin=lambda _mode: BrokenRollbackTransaction())
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        evolution._commit(handle, (("CREATE", {}, "node", "Entity"),))
    assert captured.value is primary
    assert any("rollback also failed" in note for note in primary.__notes__)


def test_error_boundary_always_has_stable_bounded_context() -> None:
    refusal = evolution._divergence("catalog_bad", phase="source", table="Entity")
    assert refusal.details == {
        "backend": "okto_grafx",
        "operation": "rebuild_grafx_schema_candidate",
        "phase": "source",
        "reason": "catalog_bad",
        "table": "Entity",
    }
    mapped = evolution._mapped(RuntimeError("secret payload"), "copy", table="Entity")
    assert isinstance(mapped, GraphError)
    assert mapped.details["backend"] == "okto_grafx"
    assert mapped.details["operation"] == "rebuild_grafx_schema_candidate"
    assert mapped.details["phase"] == "copy"
    assert mapped.details["reason"] == "backend_failure_copy"
    assert mapped.details["table"] == "Entity"
    assert "secret payload" not in repr(mapped.details)


class _FakeLock:
    instances: list[_FakeLock] = []

    def __init__(self, path: str, timeout: int) -> None:
        self.path = path
        self.timeout = timeout
        self.acquired = False
        self.released = False
        self.instances.append(self)

    def acquire(self) -> None:
        self.acquired = True

    def release(self) -> None:
        self.released = True


class _FakeCandidate:
    def __init__(self, database_uuid: bytes, *, empty: bool) -> None:
        self.identity = SimpleNamespace(database_uuid=database_uuid)
        self.catalog = SimpleNamespace(catalog=SimpleNamespace(is_empty=lambda: empty))
        self.close_complete = False
        self.close_calls = 0
        self.checkpoint_calls = 0

    def checkpoint(self) -> None:
        self.checkpoint_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        self.close_complete = True


def _patch_public_flow(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: evolution._Snapshot,
) -> None:
    monkeypatch.setattr(evolution, "FileLock", _FakeLock)
    monkeypatch.setattr(evolution, "_require_catalog", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution, "_space_id", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(evolution, "_read_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(evolution, "_require_indexes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(evolution, "_verify_clean", lambda *_args, **_kwargs: None)


def test_noop_return_closes_probe_and_releases_path_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _snapshot()
    target_counts = evolution._expected_relationship_counts(
        snapshot.relationship_counts
    )
    _patch_public_flow(monkeypatch, snapshot)
    candidate_path = tmp_path / "existing"
    candidate_path.mkdir()
    (candidate_path / "grafx.meta").write_bytes(b"observational sentinel")
    probe = _FakeCandidate(b"p" * 16, empty=False)
    opened: list[bool] = []

    def open_probe(
        _path: Path,
        _identity: Any,
        *,
        read_only: bool,
        phase: str = "candidate_open",
    ) -> _FakeCandidate:
        del phase
        opened.append(read_only)
        return probe

    monkeypatch.setattr(evolution, "_open", open_probe)
    monkeypatch.setattr(
        evolution,
        "_read_board_meta",
        lambda _handle: evolution._BoardMeta(
            snapshot.meta.board_id,
            evolution.TARGET_SCHEMA_VERSION,
            snapshot.meta.bootstrapped_at,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        evolution,
        "_certify",
        lambda *_args, **_kwargs: target_counts,
    )
    source = okto_grafx.connect(":memory:")
    try:
        result = evolution.rebuild_grafx_schema_candidate(source, candidate_path)
    finally:
        source.close()

    assert result.changed is False
    assert opened == [True]
    assert probe.close_calls == 1
    assert probe.close_complete is True
    assert _FakeLock.instances[-1].acquired is True
    assert _FakeLock.instances[-1].released is True


def test_ambiguous_stamp_uses_same_invocation_writable_recovery_then_cold_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _snapshot()
    target_counts = evolution._expected_relationship_counts(
        snapshot.relationship_counts
    )
    _patch_public_flow(monkeypatch, snapshot)
    database_uuid = b"c" * 16
    build = _FakeCandidate(database_uuid, empty=True)
    recovery = _FakeCandidate(database_uuid, empty=False)
    cold = _FakeCandidate(database_uuid, empty=False)
    handles = iter((build, recovery, cold))
    opens: list[tuple[bool, str]] = []

    def open_candidate(
        _path: Path,
        _identity: Any,
        *,
        read_only: bool,
        phase: str = "candidate_open",
    ) -> _FakeCandidate:
        opens.append((read_only, phase))
        return next(handles)

    monkeypatch.setattr(evolution, "_open", open_candidate)
    monkeypatch.setattr(evolution, "_initialise_candidate", lambda *_args: None)
    monkeypatch.setattr(evolution, "_write_nodes", lambda *_args: None)
    monkeypatch.setattr(evolution, "_write_relationships", lambda *_args: None)
    monkeypatch.setattr(
        evolution,
        "_certify",
        lambda *_args, **_kwargs: target_counts,
    )

    def ambiguous(*_args: object, **_kwargs: object) -> None:
        raise evolution._StampOutcomeAmbiguous("lost terminal report")

    monkeypatch.setattr(evolution, "_rescan_and_stamp", ambiguous)
    monkeypatch.setattr(
        evolution,
        "_read_board_meta",
        lambda _handle: evolution._BoardMeta(
            snapshot.meta.board_id,
            evolution.TARGET_SCHEMA_VERSION,
            snapshot.meta.bootstrapped_at,
            None,
            None,
        ),
    )
    source = okto_grafx.connect(":memory:")
    try:
        result = evolution.rebuild_grafx_schema_candidate(
            source, tmp_path / "candidate"
        )
    finally:
        source.close()

    assert result.changed is True
    assert result.candidate_database_uuid == database_uuid
    assert opens == [
        (False, "candidate_build_open"),
        (False, "candidate_ambiguous_recovery_open"),
        (True, "candidate_cold_open"),
    ]
    assert build.checkpoint_calls == 1
    assert recovery.checkpoint_calls == 1
    assert cold.checkpoint_calls == 0
    assert all(handle.close_calls == 1 for handle in (build, recovery, cold))
    assert _FakeLock.instances[-1].released is True


def test_lexical_aliases_contend_on_the_same_kernel_lock(tmp_path: Path) -> None:
    canonical = tmp_path / "candidate"
    alias = tmp_path / "unused" / ".." / "candidate"
    lock_path = evolution._lock_path(evolution._canonical_path(canonical, "candidate"))
    held = FileLock(str(lock_path), timeout=0)
    held.acquire()
    source = okto_grafx.connect(":memory:")
    try:
        with pytest.raises(GraphLockContention) as captured:
            evolution.rebuild_grafx_schema_candidate(source, alias)
    finally:
        source.close()
        held.release()
    assert captured.value.details == {
        "backend": "okto_grafx",
        "operation": "rebuild_grafx_schema_candidate",
        "phase": "lock_acquire",
        "reason": "candidate_locked",
    }


def _source_node_ddl(node: str, space: str) -> str:
    columns = ", ".join(
        f"{name} VECTOR({space})"
        if data_type == "DOUBLE[384]"
        else f"{name} {data_type}"
        for name, data_type in SOURCE_NODE_COLUMNS
    )
    return f"CREATE NODE TABLE {node}({columns}, PRIMARY KEY(id))"


def _source_relationship_ddl(triple: tuple[str, str, str]) -> str:
    _logical, source, target = triple
    properties = ", ".join(
        f"{name} {data_type}" for name, data_type in RELATIONSHIP_PROPERTIES
    )
    return (
        f"CREATE REL TABLE {_physical_name(triple)}"
        f"(FROM {source} TO {target}, {properties})"
    )


def _build_populated_predecessor(path: Path) -> Database:
    database = okto_grafx.connect(
        path,
        # This exhaustive predecessor populates 71 tables; the target defines 81.
        # A 512-byte heap header addresses only 16 populated tables, while 4096
        # keeps both inside Grafx's documented single-heap directory capacity.
        page_size=4096,
        partitions_per_table=1,
    )
    with database.begin("write") as schema:
        # Rotate creation order so every source numeric space id differs from the
        # target id. A passing payload assertion therefore proves name-based rebind.
        space_creation_order = (*SOURCE_NODE_SPACES[1:], SOURCE_NODE_SPACES[0])
        for _node, space, _searchable in space_creation_order:
            schema.execute(
                f"CREATE VECTOR SPACE {space} "
                "{dimension: 384, metric: 'cosine', normalized: false, "
                "storage_dtype: 'float64'}"
            )
        for node, space, _searchable in SOURCE_NODE_SPACES:
            schema.execute(_source_node_ddl(node, space))
        schema.execute(
            "CREATE NODE TABLE BoardMeta("
            "board_id STRING, schema_version STRING, bootstrapped_at TIMESTAMP, "
            "embedding_model STRING, embedding_dimension INT64, PRIMARY KEY(board_id))"
        )
        for triple in SOURCE_RELATIONSHIP_TRIPLES:
            schema.execute(_source_relationship_ddl(triple))

    source_space_ids = {
        space.name: space.space_id for space in database.catalog.catalog.spaces()
    }

    with database.begin("write") as seed:
        seed.execute(
            "CREATE (m:BoardMeta {board_id: $board_id, schema_version: $version, "
            "bootstrapped_at: $stamp, embedding_model: $model, "
            "embedding_dimension: $dimension})",
            {
                "board_id": "board-1",
                "version": "0.3.12",
                "stamp": Timestamp(micros=1),
                "model": "fixture-model",
                "dimension": 384,
            },
        )
        node_assignments = ", ".join(
            f"{name}: ${name}" for name, _data_type in SOURCE_NODE_COLUMNS
        )
        for ordinal, (node, space, _searchable) in enumerate(SOURCE_NODE_SPACES):
            for source_space_ref in (None, source_space_ids[space]):
                seed.execute(
                    f"CREATE (n:{node} {{{node_assignments}}})",
                    _fixture_node_values(
                        node,
                        ordinal,
                        vector_space_ref=source_space_ref,
                    ),
                )

        property_assignments = ", ".join(
            f"{name}: ${name}" for name, _data_type in RELATIONSHIP_PROPERTIES
        )
        for relationship in _fixture_relationships():
            _logical, source, target = relationship.triple
            parameters = {
                "from_key": relationship.from_key,
                "to_key": relationship.to_key,
                **dict(relationship.properties),
            }
            seed.execute(
                f"MATCH (a:{source} {{id: $from_key}}), "
                f"(b:{target} {{id: $to_key}}) "
                f"CREATE (a)-[r:{relationship.table} "
                f"{{{property_assignments}}}]->(b)",
                parameters,
            )
    database.checkpoint()
    return database


def _durable_bytes(root: Path) -> tuple[tuple[str, bytes], ...]:
    """Capture database-owned bytes, excluding reader/locking control artifacts."""
    return tuple(
        (entry.relative_to(root).as_posix(), entry.read_bytes())
        for entry in sorted(root.rglob("*"))
        if entry.is_file() and "control" not in entry.relative_to(root).parts
    )


@pytest.mark.slow
def test_real_populated_rebuild_is_cold_exact_source_inert_and_noop(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source"
    candidate_path = tmp_path / "candidate"
    source = _build_populated_predecessor(source_path)
    source_identity = source.identity
    source_catalog_before = source.catalog.catalog
    source_transactions_before = source.transactions
    source_wal_before = source.wal
    source_space_ids = {
        space.name: space.space_id for space in source.catalog.catalog.spaces()
    }
    source_vectors: dict[str, VectorValue] = {}
    for ordinal, (node, space, _searchable) in enumerate(SOURCE_NODE_SPACES):
        rows = source.execute(
            f"MATCH (n:{node} {{id: $id}}) RETURN n.embedding",
            {"id": _fixture_node_id(node, "vector")},
        ).rows
        assert len(rows) == 1 and type(rows[0][0]) is VectorValue
        source_vectors[node] = rows[0][0]
        assert source_vectors[node].space_ref == source_space_ids[space]
        assert source_vectors[node].values == _fixture_vector_values(ordinal)
        assert source_vectors[node].dtype == "float64"
        assert source.execute(
            f"MATCH (n:{node} {{id: $id}}) RETURN n.embedding",
            {"id": _fixture_node_id(node, "null")},
        ).rows == ((None,),)
    source_bytes_before = _durable_bytes(source_path)
    assert source.verify("all").clean

    try:
        first = evolution.rebuild_grafx_schema_candidate(
            source, candidate_path, batch_size=2
        )
        assert first.changed is True
        assert first.source_schema_version == "0.3.12"
        assert first.target_schema_version == "0.5.0"
        assert first.source_schema_fingerprint == SOURCE_SCHEMA_FINGERPRINT
        assert first.target_schema_fingerprint == TARGET_SCHEMA_FINGERPRINT
        assert first.logical_data_fingerprint == POPULATED_FIXTURE_FINGERPRINT
        assert len(first.candidate_database_uuid) == 16
        assert first.node_row_counts == EXPECTED_NODE_COUNTS
        assert first.relationship_row_counts == EXPECTED_RELATIONSHIP_COUNTS

        assert source.identity == source_identity
        assert source.catalog.catalog == source_catalog_before
        assert source.transactions == source_transactions_before
        assert source.wal == source_wal_before
        assert _durable_bytes(source_path) == source_bytes_before
        assert source.verify("all").clean

        cold = okto_grafx.connect(
            candidate_path,
            page_size=source_identity.page_size,
            partitions_per_table=source_identity.partitions_per_table,
            read_only=True,
        )
        try:
            assert cold.identity.database_uuid == first.candidate_database_uuid
            assert validate_current_grafx_schema(cold) == TARGET_SCHEMA_FINGERPRINT
            assert cold.verify("all").clean
            evolution._require_indexes(cold, "test")
            metadata = cold.execute(
                "MATCH (m:BoardMeta) RETURN m.board_id, m.schema_version, "
                "m.bootstrapped_at, m.embedding_model, m.embedding_dimension"
            ).rows
            assert metadata == (
                (
                    "board-1",
                    "0.5.0",
                    Timestamp(micros=1),
                    "fixture-model",
                    384,
                ),
            )
            candidate_space_ids = {
                space.name: space.space_id for space in cold.catalog.catalog.spaces()
            }
            assert all(
                source_space_ids[space] != candidate_space_ids[space]
                for _node, space, _searchable in SOURCE_NODE_SPACES
            )
            introduced_catalog_slots = 0
            introduced_null_cells = 0
            projection = ", ".join(f"n.{name}" for name in INTRODUCED_NODE_PROPERTIES)
            for ordinal, (table, space, _searchable) in enumerate(SOURCE_NODE_SPACES):
                table_columns = tuple(
                    column.name for column in cold.catalog.catalog.table(table).columns
                )
                introduced_catalog_slots += sum(
                    name in INTRODUCED_NODE_PROPERTIES for name in table_columns
                )
                assert table_columns[-12:-1] == INTRODUCED_NODE_PROPERTIES
                rows = cold.execute(
                    f"MATCH (n:{table}) RETURN n.id, n.embedding, {projection} "
                    "ORDER BY n.id"
                ).rows
                assert len(rows) == 2
                by_id = {row[0]: row for row in rows}
                null_row = by_id[_fixture_node_id(table, "null")]
                vector_row = by_id[_fixture_node_id(table, "vector")]
                assert null_row[1] is None
                assert null_row[2:] == (None,) * 11
                assert vector_row[2:] == (None,) * 11
                introduced_null_cells += sum(
                    value is None for row in rows for value in row[2:]
                )
                rebuilt = vector_row[1]
                assert type(rebuilt) is VectorValue
                assert rebuilt.space_ref == candidate_space_ids[space]
                assert rebuilt.space_ref != source_vectors[table].space_ref
                assert rebuilt.values == source_vectors[table].values
                assert rebuilt.values == _fixture_vector_values(ordinal)
                assert rebuilt.dtype == source_vectors[table].dtype == "float64"
            assert introduced_catalog_slots == 121
            assert introduced_null_cells == 242

            for triple in SOURCE_RELATIONSHIP_TRIPLES:
                _logical, source_table, target_table = triple
                table = _physical_name(triple)
                expected_count = 4 if table == "belongs_to__Entity__Entity" else 1
                rows = cold.execute(
                    f"MATCH (a:{source_table})-[r:{table}]->"
                    f"(b:{target_table}) RETURN a.id, b.id, r.confidence, "
                    "r.created_by_session_id"
                ).rows
                assert len(rows) == expected_count
            edges = cold.execute(
                "MATCH (a:Entity)-[r:belongs_to__Entity__Entity]->(b:Entity) "
                "RETURN a.id, b.id, r.confidence, r.created_by_session_id "
                "ORDER BY a.id, b.id, r.confidence"
            ).rows
            entity_vector = _fixture_node_id("Entity", "vector")
            entity_null = _fixture_node_id("Entity", "null")
            parallel = [row for row in edges if row[:2] == (entity_vector, entity_null)]
            self_loops = [
                row for row in edges if row[:2] == (entity_vector, entity_vector)
            ]
            assert len(parallel) == 3
            assert len(self_loops) == 1
            base_confidence = (
                float(
                    SOURCE_RELATIONSHIP_TRIPLES.index(
                        ("belongs_to", "Entity", "Entity")
                    )
                    + 1
                )
                / 100.0
            )
            assert sum(row[2] == base_confidence for row in parallel) == 2
            assert sum(row[3] == "parallel-distinct" for row in parallel) == 1

            for table, source_table, target_table in INTRODUCED_RELATIONSHIP_ENDPOINTS:
                assert (
                    cold.execute(
                        f"MATCH (a:{source_table})-[r:{table}]->"
                        f"(b:{target_table}) RETURN a.id"
                    ).rows
                    == ()
                )
        finally:
            cold.close()
        assert cold.close_complete

        candidate_bytes_before_noop = _durable_bytes(candidate_path)
        second = evolution.rebuild_grafx_schema_candidate(
            source, candidate_path, batch_size=1
        )
        assert second.changed is False
        assert second.logical_data_fingerprint == first.logical_data_fingerprint
        assert second.candidate_database_uuid == first.candidate_database_uuid
        assert second.node_row_counts == first.node_row_counts
        assert second.relationship_row_counts == first.relationship_row_counts
        assert _durable_bytes(candidate_path) == candidate_bytes_before_noop
        assert source.catalog.catalog == source_catalog_before
        assert source.transactions == source_transactions_before
        assert source.wal == source_wal_before
        assert _durable_bytes(source_path) == source_bytes_before
    finally:
        source.close()
    assert source.close_complete

    marked = okto_grafx.connect(
        candidate_path,
        page_size=source_identity.page_size,
        partitions_per_table=source_identity.partitions_per_table,
    )
    try:
        with marked.begin("write") as mutation:
            mutation.execute(
                "MATCH (m:BoardMeta {board_id: 'board-1'}) "
                "SET m.schema_version = $marker RETURN m.board_id",
                {"marker": evolution.BUILD_MARKER},
            )
        with pytest.raises(GraphCapabilityUnavailable) as refused:
            ensure_current_grafx_board_schema(
                marked,
                board_id="board-1",
                bootstrapped_at=Timestamp(micros=1),
                embedding_model="fixture-model",
                embedding_dimension=384,
            )
        assert refused.value.details["reason"] == "board_meta_version_mismatch"
        assert marked.execute("MATCH (m:BoardMeta) RETURN m.schema_version").rows == (
            (evolution.BUILD_MARKER,),
        )
    finally:
        marked.close()
