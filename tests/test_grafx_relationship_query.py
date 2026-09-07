"""Logical relationship reads use the same physical layout as writes."""

import pytest

from okto_pulse.community.adapters.grafx_relationship_query import (
    translate_logical_relationships,
)


@pytest.mark.parametrize(
    "query, logical, physical",
    [
        (
            "MATCH (d:Decision)-[r:relates_to]->(a:Alternative) RETURN a",
            "relates_to",
            "relates_to__Decision__Alternative",
        ),
        (
            "MATCH (:Constraint)<-[:violates]-(b:Bug) RETURN b",
            "violates",
            "violates__Bug__Constraint",
        ),
        (
            "MATCH (n:Decision {id:$id}) WITH n OPTIONAL MATCH (n)<-[c:contradicts]-() RETURN c",
            "contradicts",
            "contradicts__Decision__Decision",
        ),
        (
            "MATCH (a:Decision), (b:Entity) CREATE (a)-[:belongs_to {layer:$layer}]->(b)",
            "belongs_to",
            "belongs_to__Decision__Entity",
        ),
    ],
)
def test_proven_endpoint_pair(query, logical, physical):
    assert translate_logical_relationships(query) == query.replace(
        ":" + logical, ":" + physical
    )


def test_chain_translates_both_relationships():
    query = "MATCH (a:Decision)-[:supersedes]->(b:Decision)-[:supersedes]->(c:Decision) RETURN a"
    assert (
        translate_logical_relationships(query).count(":supersedes__Decision__Decision")
        == 2
    )


def test_single_table_logical_type_preserves_incompatible_endpoint_semantics():
    query = "MATCH (n:Entity) OPTIONAL MATCH (n)<-[:contradicts]-() RETURN n"
    assert translate_logical_relationships(query) == query.replace(
        ":contradicts", ":contradicts__Decision__Decision"
    )


def test_comments_literals_and_parameters_are_preserved():
    query = (
        "// MATCH (d:Decision)-[:supersedes]->(e:Decision)\n"
        "MATCH (d:Decision)-[r:supersedes]->(e:Decision) "
        "WHERE d.title = '(d:Decision)-[:supersedes]->(e:Decision)' "
        "/* (d:Decision)-[:supersedes]->(e:Decision) */ RETURN $supersedes"
    )
    expected = query.replace("[r:supersedes]", "[r:supersedes__Decision__Decision]")
    assert translate_logical_relationships(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n:Decision) MATCH (n:Entity) OPTIONAL MATCH (n)<-[:contradicts]-() RETURN n",
        "MATCH (n:Decision) WITH 1 AS n OPTIONAL MATCH (n)<-[:contradicts]-() RETURN n",
        "MATCH (n:Decision), (m:Entity) WITH m MATCH (n)-[:belongs_to]->() RETURN n",
        "MATCH (n:Decision), (m:Entity) WITH coalesce(m,n,m) AS v MATCH (n)-[:belongs_to]->() RETURN n",
        "MATCH (n:Decision) RETURN n UNION MATCH (n)<-[:contradicts]-() RETURN n",
        "MATCH (n:Decision) CALL { MATCH (n)<-[:contradicts]-() RETURN n } RETURN n",
        "MATCH (n:Decision)-[:relates_to]->() RETURN n",
        "MATCH (n)-[:contradicts]->(m) RETURN n",
        "MATCH (n:Decision)-[:supersedes]-(m:Decision) RETURN n",
        "MATCH (n:Decision)-[:supersedes*1..3]->(m:Decision) RETURN n",
        "MATCH (n:Decision)-[:supersedes|contradicts]->(m:Decision) RETURN n",
        "MATCH (`n`:Decision)-[:supersedes]->(m:Decision) RETURN m",
        "MATCH (n:Decision)-[:supersedes__Decision__Decision]->(m:Decision) RETURN n",
    ],
)
def test_unproven_or_unsupported_patterns_are_not_narrowed(query):
    assert translate_logical_relationships(query) == query


def test_caller_relationship_authority_and_resolver_are_honored():
    query = "MATCH (a:A)-[:edge]->(b:B) RETURN b"
    assert translate_logical_relationships(query, relationship_pairs=()) == query
    assert translate_logical_relationships(
        query,
        relationship_pairs=(("edge", "A", "B"),),
        resolver=lambda *_: "custom_table",
    ) == query.replace(":edge", ":custom_table")
