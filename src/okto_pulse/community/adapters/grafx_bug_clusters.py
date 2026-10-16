"""Bounded native observations for the Bug cluster read port; no graph writes."""
from datetime import datetime, timezone

from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.ports.bug_clusters import (
    BugClusterAssociation, BugClusterGraphFacts, MAX_CLUSTER_BUGS, MAX_CLUSTER_ASSOCIATIONS,
)
from okto_pulse.core.ports.card_projection import bug_origin_proxy_read_metadata, BUG_ORIGIN_PROXY_FAMILIES
from okto_pulse.community.adapters.grafx_relationship_layout import resolve_relationship_table


def _instant(value):
    if not value:
        return None
    instant = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return instant.replace(tzinfo=timezone.utc) if instant.tzinfo is None else instant.astimezone(timezone.utc)


def read_bug_cluster_graph(reader, bugs, *, group_by):
    from okto_pulse.community.adapters.grafx_graph_store import _rows
    if group_by not in {'proxy', 'spec', 'learning', 'severity'}:
        raise ValueError('bug_clusters_grouping_invalid')
    if type(bugs) is not tuple or len(bugs) > MAX_CLUSTER_BUGS:
        raise ValueError('bug_clusters_source_bound')
    if not bugs:
        return BugClusterGraphFacts((), ())
    expected = {f'card:{bug.bug_id}': bug for bug in bugs}
    if len(expected) != len(bugs):
        raise ValueError('bug_clusters_duplicate_source')
    rows = _rows(reader.execute(
        'MATCH (b:Bug) WHERE b.source_artifact_ref IN $refs '
        f'AND {tpl.active_read_filter_clause("b")} '
        'RETURN b.id, b.source_artifact_ref, b.source_created_at, b.source_status, b.severity '
        'ORDER BY b.id LIMIT $bound', {'refs': list(expected), 'bound': len(bugs) + 1}))
    observed = {}
    seen = set()
    for node_id, ref, created_at, status, severity in rows:
        if ref in seen:
            raise ValueError('bug_clusters_ambiguous_projection')
        seen.add(ref)
        fact = expected.get(ref)
        if fact is None:
            raise ValueError('bug_clusters_projection_scope_invalid')
        if (_instant(created_at), status, severity) == (fact.source_created_at, fact.status, fact.severity):
            observed[node_id] = fact.bug_id
    if not observed or group_by in {'spec', 'severity'}:
        return BugClusterGraphFacts(tuple(sorted(observed.values())), ())
    associations = []
    truncated = False
    # One transaction, a constant number of bounded branches, no per-Bug query.
    branches = ('Requirement', 'Constraint', 'Criterion') if group_by == 'proxy' else ('Learning',)
    remaining = MAX_CLUSTER_ASSOCIATIONS
    for target_type in branches:
        if not remaining:
            truncated = True
            break
        if group_by == 'proxy':
            physical = resolve_relationship_table('violates', 'Bug', target_type)
            pattern = f'(b:Bug)-[r:{physical}]->(t:{target_type})'
        else:
            physical = resolve_relationship_table('validates', 'Learning', 'Bug')
            pattern = f'(t:Learning)-[r:{physical}]->(b:Bug)'
        rows = _rows(reader.execute(
            f'MATCH {pattern} WHERE b.id IN $ids '
            f'AND {tpl.active_read_filter_clause("b")} AND {tpl.active_read_filter_clause("t")} '
            'RETURN b.id, t.id, t.title, t.source_artifact_ref, r.rule_id, r.layer, r.created_by, r.fallback_reason '
            'ORDER BY b.id, t.id, r.rule_id LIMIT $bound',
            {'ids': list(observed), 'bound': remaining + 1}))
        if len(rows) > remaining:
            rows = rows[:remaining]
            truncated = True
        remaining -= len(rows)
        for node_id, target_id, title, source_ref, rule, layer, author, fallback in rows:
            if group_by == 'proxy':
                metadata = bug_origin_proxy_read_metadata(rule_id=rule, layer=layer,
                    created_by=author, fallback_reason=fallback)
                if not metadata or not source_ref:
                    continue
                if not any(family.owns_writer(rule_id=rule, layer=layer, created_by=author)
                    and family.owns_endpoints(owner_id=observed[node_id], source_type='Bug', target_type=target_type,
                        source_ref='card:' + observed[node_id], target_ref=source_ref)
                    for family in BUG_ORIGIN_PROXY_FAMILIES):
                    continue
                target_ref = str(source_ref)
                provenance_ref = metadata['rule_id']
            else:
                target_ref = f'learning:{target_id}'
                # Preserve the observed writer, without promoting an edge to an
                # authenticated current applicability result.
                provenance_ref = f'graph:validates:{target_id}:{node_id}:{rule or "unclassified"}'
            associations.append(BugClusterAssociation(observed[node_id], group_by, target_ref,
                str(title or target_ref)[:240], provenance_ref, 'unknown'))
    return BugClusterGraphFacts(tuple(sorted(observed.values())), tuple(associations), truncated=truncated)
