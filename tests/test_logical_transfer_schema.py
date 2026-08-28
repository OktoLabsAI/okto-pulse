"""The Board and Global logical schemas derive from the Community authorities.

These cover frozen-contract item 1 -- "derive from the existing Community
authorities and validate drift fail-closed" -- for the module the matrix tests
build on.  They are not new matrix cells.

The distinctions asserted here are the ones a convenient simplification would
lose: every Board type names its embedding property identically while belonging
to a different space, one relation name spans several endpoint pairs, and the
Global Board type is keyed differently from its siblings.
"""

from __future__ import annotations

import dataclasses

import pytest

from okto_pulse.community.adapters.logical_transfer_schema import (
    BOARD_CENSUS,
    GLOBAL_CENSUS,
    SchemaDerivationError,
    board_logical_schema,
    global_logical_schema,
    require_no_schema_drift,
)


class TestBoardScope:
    def test_the_derived_schema_matches_its_frozen_census(self) -> None:
        schema = board_logical_schema()
        assert len(schema.node_types) == BOARD_CENSUS.node_types == 12
        assert len(schema.relation_layouts) == BOARD_CENSUS.relation_layouts == 69
        assert len(schema.vector_spaces) == BOARD_CENSUS.vector_spaces == 11

    def test_board_meta_is_carried_with_its_own_key(self) -> None:
        meta = board_logical_schema().node_type("BoardMeta")
        assert meta.key == "board_id"
        assert len(meta.properties) == 5

    def test_every_type_names_its_embedding_the_same_and_lands_elsewhere(self) -> None:
        # The exact collision a name-derived space would flatten: eleven
        # properties called `embedding`, eleven different spaces.
        schema = board_logical_schema()
        typed = [n for n in schema.node_types if n.name != "BoardMeta"]
        assert len(typed) == 11
        spaces = {n.property_def("embedding").vector_space for n in typed}
        assert len(spaces) == 11

    def test_each_declared_space_exists_in_the_schema(self) -> None:
        schema = board_logical_schema()
        declared = {s.name for s in schema.vector_spaces}
        for node_type in schema.node_types:
            if node_type.name == "BoardMeta":
                continue
            assert node_type.property_def("embedding").vector_space in declared

    def test_one_layout_name_spans_several_endpoint_pairs(self) -> None:
        layouts = board_logical_schema().relation_layouts
        names = {layout.name for layout in layouts}
        assert len(layouts) == 69
        assert len(names) == 16
        supersedes = {
            layout.identity for layout in layouts if layout.name == "supersedes"
        }
        assert ("supersedes", "Decision", "Decision") in supersedes
        assert ("supersedes", "Alternative", "Alternative") in supersedes

    def test_every_layout_carries_the_relation_columns(self) -> None:
        for layout in board_logical_schema().relation_layouts:
            assert len(layout.properties) == 7

    def test_the_key_property_is_the_only_non_nullable_one(self) -> None:
        decision = board_logical_schema().node_type("Decision")
        not_null = [p.name for p in decision.properties if not p.nullable]
        assert not_null == [decision.key]


class TestGlobalScope:
    def test_the_derived_schema_matches_its_frozen_census(self) -> None:
        schema = global_logical_schema()
        assert len(schema.node_types) == GLOBAL_CENSUS.node_types == 4
        assert len(schema.relation_layouts) == GLOBAL_CENSUS.relation_layouts == 7
        assert len(schema.vector_spaces) == GLOBAL_CENSUS.vector_spaces == 4

    def test_the_board_type_is_keyed_differently_from_its_siblings(self) -> None:
        schema = global_logical_schema()
        assert schema.node_type("Board").key == "board_id"
        for name in ("Topic", "Entity", "DecisionDigest"):
            assert schema.node_type(name).key == "id"

    def test_entity_and_decision_digest_share_a_property_name(self) -> None:
        schema = global_logical_schema()
        entity = schema.node_type("Entity").property_def("embedding")
        digest = schema.node_type("DecisionDigest").property_def("embedding")
        assert entity.name == digest.name == "embedding"
        assert entity.vector_space == "entity_embedding_idx"
        assert digest.vector_space == "digest_embedding_idx"

    def test_only_the_self_relations_carry_weight(self) -> None:
        weighted = {
            layout.identity
            for layout in global_logical_schema().relation_layouts
            if layout.properties
        }
        assert weighted == {
            ("TOPIC_RELATES_TO", "Topic", "Topic"),
            ("ENTITY_RELATES_TO", "Entity", "Entity"),
        }

    def test_the_seven_layouts_are_the_declared_ones(self) -> None:
        identities = {
            layout.identity for layout in global_logical_schema().relation_layouts
        }
        assert identities == {
            ("HAS_TOPIC", "Board", "Topic"),
            ("MENTIONS_ENTITY", "Board", "Entity"),
            ("CONTAINS_DECISION", "Board", "DecisionDigest"),
            ("TOPIC_RELATES_TO", "Topic", "Topic"),
            ("ENTITY_RELATES_TO", "Entity", "Entity"),
            ("DECISION_MENTIONS_ENTITY", "DecisionDigest", "Entity"),
            ("DECISION_DERIVES_FROM", "DecisionDigest", "DecisionDigest"),
        }


class TestDriftIsFailClosed:
    """A derivation follows its authority anywhere; the census is what notices."""

    @pytest.mark.parametrize(
        "field",
        [
            "node_types",
            "relation_layouts",
            "vector_spaces",
            "node_property_defs",
            "relation_property_defs",
        ],
    )
    def test_a_census_that_disagrees_on_any_count_refuses(self, field: str) -> None:
        drifted = dataclasses.replace(
            BOARD_CENSUS, **{field: getattr(BOARD_CENSUS, field) + 1}
        )
        with pytest.raises(SchemaDerivationError) as caught:
            require_no_schema_drift(board_logical_schema(), drifted)
        assert "drifted" in str(caught.value)

    def test_the_two_scopes_do_not_satisfy_each_other(self) -> None:
        with pytest.raises(SchemaDerivationError):
            require_no_schema_drift(global_logical_schema(), BOARD_CENSUS)

    def test_a_matching_census_is_accepted(self) -> None:
        require_no_schema_drift(board_logical_schema(), BOARD_CENSUS)
        require_no_schema_drift(global_logical_schema(), GLOBAL_CENSUS)
