"""M-PULSE-3B contract for logical relationships over physical Grafx tables."""

from __future__ import annotations

from pathlib import Path

import okto_grafx
import pytest
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
)

from okto_pulse.community.adapters.grafx_graph_transaction import (
    CommunityGrafxGraphTransaction,
)
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
    RelationshipLayout,
    introspect_logical_relationships,
    resolve_relationship_table,
)


def _install_layout(database, layout: RelationshipLayout) -> None:
    node_types = tuple(
        dict.fromkeys(
            endpoint
            for entry in layout.entries
            for endpoint in (entry.from_type, entry.to_type)
        )
    )
    with database.begin("write") as schema:
        for node_type in node_types:
            schema.execute(f"CREATE NODE TABLE {node_type}(id STRING, PRIMARY KEY(id))")
    with database.begin("write") as schema:
        for entry in layout.entries:
            schema.execute(
                f"CREATE REL TABLE {entry.physical_table}"
                f"(FROM {entry.from_type} TO {entry.to_type})"
            )


def test_closed_pulse_authority_has_16_types_69_pairs_and_unique_names() -> None:
    layout = PULSE_RELATIONSHIP_LAYOUT

    assert len(layout.logical_definitions) == 16
    assert len(layout.entries) == 69
    assert (
        sum(len(definition.endpoint_pairs) for definition in layout.logical_definitions)
        == 69
    )

    physical = tuple(entry.physical_table for entry in layout.entries)
    assert len(set(physical)) == 69
    assert max(len(name.encode("ascii")) for name in physical) == 38
    assert all("__" in name for name in physical)
    assert all(
        layout.resolve(entry.logical_type, entry.from_type, entry.to_type)
        == entry.physical_table
        for entry in layout.entries
    )


def test_same_logical_type_uses_a_distinct_table_for_each_endpoint_pair() -> None:
    decision = resolve_relationship_table("supersedes", "Decision", "Decision")
    criterion = resolve_relationship_table("supersedes", "Criterion", "Criterion")

    assert decision == "supersedes__Decision__Decision"
    assert criterion == "supersedes__Criterion__Criterion"
    assert decision != criterion


def test_unknown_pair_and_unsafe_identifier_fail_in_the_core_taxonomy() -> None:
    with pytest.raises(GraphCapabilityUnavailable) as unknown:
        resolve_relationship_table("supersedes", "Decision", "Bug")
    assert unknown.value.details == {
        "backend": "okto_grafx",
        "operation": "logical_relationship_layout",
        "reason": "unknown_endpoint_pair",
        "logical_type": "supersedes",
        "from_type": "Decision",
        "to_type": "Bug",
    }

    with pytest.raises(GraphCapabilityUnavailable) as unsafe:
        RelationshipLayout((("not-safe", "A", "B"),))
    assert unsafe.value.details["reason"] == "invalid_identifier"
    assert unsafe.value.details["field"] == "logical_type"


@pytest.mark.parametrize(
    "malformed",
    [
        (("a", "b"),),
        (("a", "b", "c", "d"),),
        ((123,),),
        ("abc",),
    ],
)
def test_malformed_endpoint_pairs_fail_in_the_core_taxonomy(malformed) -> None:
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        RelationshipLayout(malformed)

    assert captured.value.details["reason"] == "invalid_endpoint_pair"


def test_hostile_repr_does_not_escape_the_core_error_taxonomy() -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError("repr must not escape")

    with pytest.raises(GraphCapabilityUnavailable) as constructor:
        RelationshipLayout(((Hostile(), "A", "B"),))
    assert constructor.value.details["reason"] == "invalid_identifier"
    assert constructor.value.details["value"] == "<unrepresentable Hostile>"

    layout = RelationshipLayout((("edge", "A", "B"),))
    with pytest.raises(GraphCapabilityUnavailable) as resolver:
        layout.resolve(Hostile(), "A", "B")  # type: ignore[arg-type]
    assert resolver.value.details["reason"] == "invalid_identifier"
    assert resolver.value.details["value"] == "<unrepresentable Hostile>"


def test_layout_manifest_cannot_be_mutated_after_validation() -> None:
    layout = RelationshipLayout((("edge", "A", "B"),))

    with pytest.raises(TypeError):
        layout._by_key[("other", "A", "B")] = layout.entries[0]  # type: ignore[index]
    with pytest.raises(AttributeError):
        layout._entries = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del layout._logical_definitions  # type: ignore[misc]

    assert layout.resolve("edge", "A", "B") == "edge__A__B"


def test_physical_name_collision_is_refused_instead_of_being_overwritten() -> None:
    with pytest.raises(GraphCapabilityUnavailable) as captured:
        RelationshipLayout(
            (
                ("a__b", "c", "d"),
                ("a", "b__c", "d"),
            )
        )

    assert captured.value.details["reason"] == "physical_name_collision"
    assert captured.value.details["physical_table"] == "a__b__c__d"


def test_introspection_validates_all_pairs_and_hides_physical_names(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "logical-layout"
    database = okto_grafx.connect(database_path)
    _install_layout(database, PULSE_RELATIONSHIP_LAYOUT)

    before = introspect_logical_relationships(database)
    database.close()

    reopened = okto_grafx.connect(database_path)
    try:
        after = introspect_logical_relationships(reopened)
    finally:
        reopened.close()

    assert after == before == PULSE_RELATIONSHIP_LAYOUT.logical_definitions
    assert len(after) == 16
    rendered = repr(after)
    assert all(
        entry.physical_table not in rendered
        for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    )


def test_introspection_refuses_a_table_of_the_wrong_kind(tmp_path: Path) -> None:
    layout = RelationshipLayout((("edge", "A", "B"),))
    database = okto_grafx.connect(tmp_path / "wrong-kind")
    try:
        with database.begin("write") as schema:
            for name in ("A", "B", "edge__A__B"):
                schema.execute(f"CREATE NODE TABLE {name}(id STRING, PRIMARY KEY(id))")

        with pytest.raises(GraphCapabilityUnavailable) as captured:
            introspect_logical_relationships(database, layout=layout)
    finally:
        database.close()

    assert captured.value.details["reason"] == "physical_schema_mismatch"
    assert captured.value.details["observed_kind"] == "node"


def test_introspection_refuses_endpoint_drift(tmp_path: Path) -> None:
    layout = RelationshipLayout((("edge", "A", "B"),))
    database = okto_grafx.connect(tmp_path / "wrong-endpoints")
    try:
        with database.begin("write") as schema:
            for name in ("A", "B", "C"):
                schema.execute(f"CREATE NODE TABLE {name}(id STRING, PRIMARY KEY(id))")
        with database.begin("write") as schema:
            schema.execute("CREATE REL TABLE edge__A__B(FROM A TO C)")

        with pytest.raises(GraphCapabilityUnavailable) as captured:
            introspect_logical_relationships(database, layout=layout)
    finally:
        database.close()

    assert captured.value.details["reason"] == "physical_schema_mismatch"
    assert captured.value.details["observed_from"] == "A"
    assert captured.value.details["observed_to"] == "C"


def test_introspection_refuses_a_missing_or_closed_catalog(tmp_path: Path) -> None:
    layout = RelationshipLayout((("edge", "A", "B"),))
    database = okto_grafx.connect(tmp_path / "missing")
    with database.begin("write") as schema:
        for name in ("A", "B"):
            schema.execute(f"CREATE NODE TABLE {name}(id STRING, PRIMARY KEY(id))")

    with pytest.raises(GraphCapabilityUnavailable) as missing:
        introspect_logical_relationships(database, layout=layout)
    assert missing.value.details["operation"] == "logical_relationship_layout"
    assert missing.value.details["physical_table"] == "edge__A__B"

    database.close()
    with pytest.raises(GraphCapabilityUnavailable) as closed:
        introspect_logical_relationships(database, layout=layout)
    assert closed.value.details["operation"] == "logical_relationship_layout"


def test_complete_provider_uses_the_manifest_but_custom_authority_stays_compatible() -> (
    None
):
    complete = CommunityGrafxGraphTransaction(
        database_resolver=lambda _board_id: None,  # type: ignore[arg-type]
        revalidate_fence=lambda _board_id, _phase: None,
    )
    custom = CommunityGrafxGraphTransaction(
        database_resolver=lambda _board_id: None,  # type: ignore[arg-type]
        revalidate_fence=lambda _board_id, _phase: None,
        node_types=("A", "B"),
        relationship_pairs=(("edge", "A", "B"),),
    )

    assert (
        complete._relationship_table_resolver("supports", "Entity", "Entity")
        == "supports__Entity__Entity"
    )
    assert custom._relationship_table_resolver("edge", "A", "B") == "edge"
