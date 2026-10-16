"""Read a closed Spec scope in one native transaction; no projection writes."""
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.ports.spec_coverage_query import (
    MAX_SPEC_COVERAGE_ITEMS, MAX_SPEC_COVERAGE_FACTS, SpecCoverageGraphFacts,
)
from okto_pulse.community.adapters.grafx_relationship_layout import PULSE_RELATIONSHIP_LAYOUT, resolve_relationship_table

NODE_TYPES = frozenset({'Entity', 'Bug', 'Requirement', 'Constraint', 'Criterion',
    'Decision', 'APIContract', 'TestScenario'})
RELATIONS = frozenset({'belongs_to', 'derives_from', 'tests', 'implements', 'supports'})


def read_spec_coverage_graph(reader, scope):
    from okto_pulse.community.adapters.grafx_graph_store import _rows
    if len(scope.nodes) > 2 * MAX_SPEC_COVERAGE_ITEMS + 1 or len(scope.relations) > MAX_SPEC_COVERAGE_FACTS:
        raise ValueError('spec_coverage_graph_scope_bound')
    expected = set(scope.nodes)
    if len(expected) != len(scope.nodes) or any(kind not in NODE_TYPES for kind, _ in expected):
        raise ValueError('spec_coverage_graph_scope_invalid')
    by_id = {}
    observed = set()
    for kind in sorted({kind for kind, _ in expected}):
        refs = sorted(ref for typ, ref in expected if typ == kind)
        rows = _rows(reader.execute(f'MATCH (n:{kind}) WHERE n.source_artifact_ref IN $refs '
            # Supersession preserves historical nodes with the same source ref.
            # Only the current head participates in this coverage observation;
            # two current heads still fail the identity check below.
            "AND (n.superseded_by IS NULL OR n.superseded_by = '') "
            f'AND {tpl.active_read_filter_clause("n")} '
            'RETURN n.id, n.source_artifact_ref ORDER BY n.id LIMIT $bound',
            {'refs': refs, 'bound': len(refs) + 1}))
        for identity, ref in rows:
            key = (kind, ref)
            if key not in expected or key in observed:
                raise ValueError('spec_coverage_ambiguous_projection')
            observed.add(key)
            by_id[kind, identity] = key
    # Every native label comes from a closed vocabulary/layout resolver.
    # Include closed layouts even when the last source link has disappeared;
    # deriving branches only from expected edges would conceal stale relations.
    layouts = {(entry.from_type, entry.logical_type, entry.to_type)
        for entry in PULSE_RELATIONSHIP_LAYOUT.entries
        if entry.from_type in NODE_TYPES and entry.to_type in NODE_TYPES and entry.logical_type in RELATIONS}
    edges = set()
    remaining = MAX_SPEC_COVERAGE_FACTS
    truncated = False
    for source_type, relation, target_type in sorted(layouts):
        if source_type not in NODE_TYPES or target_type not in NODE_TYPES or relation not in RELATIONS:
            raise ValueError('spec_coverage_graph_layout_invalid')
        source_ids = [identity for kind, identity in by_id if kind == source_type]
        target_ids = [identity for kind, identity in by_id if kind == target_type]
        if not source_ids or not target_ids:
            continue
        if not remaining:
            truncated = True
            break
        physical = resolve_relationship_table(relation, source_type, target_type)
        rows = _rows(reader.execute(
            f'MATCH (s:{source_type})-[r:{physical}]->(t:{target_type}) '
            'WHERE s.id IN $sources AND t.id IN $targets '
            f'AND {tpl.active_read_filter_clause("s")} AND {tpl.active_read_filter_clause("t")} '
            'RETURN s.id, t.id, r.rule_id, r.layer, r.created_by '
            'ORDER BY s.id, t.id, r.rule_id LIMIT $bound',
            {'sources': source_ids, 'targets': target_ids, 'bound': remaining + 1}))
        if len(rows) > remaining:
            rows = rows[:remaining]
            truncated = True
        remaining -= len(rows)
        for source_id, target_id, rule, layer, author in rows:
            edges.add((*by_id[source_type, source_id], relation, *by_id[target_type, target_id],
                str(rule or 'unclassified'), str(layer or 'unknown'), str(author or 'unknown')))
    return SpecCoverageGraphFacts(tuple(sorted(observed)), tuple(sorted(edges)), truncated=truncated)
