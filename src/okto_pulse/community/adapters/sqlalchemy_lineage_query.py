"""Bounded column reads for source lineage, using the existing dependency closure."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from sqlalchemy import or_, select
from sqlalchemy.orm import aliased
from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision, Card, CardDependency, Ideation, Refinement, Spec, SpecDependency, Story, StoryIdeationLink,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout
from okto_pulse.core.ports.lineage_query import (
    LineageNode, LineageRelation, LineageSnapshot, MAX_LINEAGE_EDGES, MAX_LINEAGE_NODES,
)
from okto_pulse.core.ports.traceability import TraceabilityReadError

_MODELS = {'spec': Spec, 'card': Card, 'refinement': Refinement, 'ideation': Ideation,
    'story': Story, 'amendment_hotfix_revision': AmendmentHotfixRevision}
_EXTRA = {'spec': ('ideation_id', 'refinement_id'), 'card': ('spec_id', 'origin_task_id', 'card_type'),
    'refinement': ('ideation_id',), 'ideation': (), 'story': (),
    'amendment_hotfix_revision': ('original_spec_id', 'revision_spec_id', 'origin_bug_id', 'origin_task_ids',
        'affected_task_ids', 'regression_test_task_ids', 'lineage_state')}


def _text(value):
    return str(getattr(value, 'value', value))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()


async def _collect(db, query):
    from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import build_dependency_graph
    kind, identity = query.subject_ref.split(':')
    nodes, pending, source_rows = {}, {query.subject_ref}, []
    edges = set()
    missing = set()
    dependency_scope = None
    if kind in {'spec', 'card'}:
        dependency_scope = await build_dependency_graph(db, query.board_id, entity_type=kind, entity_id=identity)
        ref_by_old = {}
        for node in dependency_scope['nodes']:
            ref = f"{'spec' if node['entity_type'] == 'spec' else 'card'}:{node['entity_id']}"
            ref_by_old[node['id']] = ref
            pending.add(ref)
        for edge in dependency_scope['edges']:
            edges.add(LineageRelation(ref_by_old[edge['source']], ref_by_old[edge['target']],
                'precedes', f"dependency:{edge['dependency_id']}"))
    dependency_checked = set(pending) if dependency_scope is not None else set()
    seen, amendments_checked = set(), set()
    while pending:
        batch = pending - seen
        if not batch:
            break
        if len(seen | batch) > MAX_LINEAGE_NODES:
            raise ValueError('lineage_source_node_bound')
        seen.update(batch)
        pending = set()
        for family, model in _MODELS.items():
            ids = sorted(ref.split(':')[1] for ref in batch if ref.startswith(family + ':'))
            if not ids:
                continue
            fields = ['id', 'status', *_EXTRA[family]]
            if family != 'amendment_hotfix_revision':
                fields.append('title')
            rows = (await db.execute(select(*(getattr(model, name) for name in fields))
                .where(model.board_id == query.board_id, model.id.in_(ids))
                .order_by(model.id).limit(MAX_LINEAGE_NODES + 1))).mappings().all()
            found = {str(row['id']) for row in rows}
            missing.update(f'{family}:{item}' for item in set(ids) - found)
            for row in rows:
                value = dict(row)
                source_rows.append((family, value))
                ref = f"{family}:{row['id']}"
                entity_type = _text(row['card_type']) if family == 'card' else family
                title = f"Amendment {row['id']}" if family == 'amendment_hotfix_revision' else str(row['title'])
                nodes[ref] = LineageNode(ref, entity_type, title[:240], _text(row['status']))
                declarations = []
                if family == 'spec':
                    declarations = [('refinement', row['refinement_id'], 'derived_from', 'refinement_id'),
                        ('ideation', row['ideation_id'], 'derived_from', 'ideation_id')]
                elif family == 'card':
                    declarations = [('spec', row['spec_id'], 'belongs_to', 'spec_id')]
                    if row['origin_task_id']:
                        origin = f"card:{row['origin_task_id']}"
                        pending.add(origin)
                        edges.add(LineageRelation(origin, ref, 'originates_bug', f'{ref}:origin_task_id'))
                elif family == 'refinement':
                    declarations = [('ideation', row['ideation_id'], 'derived_from', 'ideation_id')]
                elif family == 'amendment_hotfix_revision':
                    declarations = [('spec', row['original_spec_id'], 'amendment_of', 'original_spec_id'),
                        ('card', row['origin_bug_id'], 'derived_from', 'origin_bug_id')]
                    if row['revision_spec_id']:
                        revision = f"spec:{row['revision_spec_id']}"
                        pending.add(revision)
                        edges.add(LineageRelation(revision, ref, 'derived_from', f'{ref}:revision_spec_id'))
                    for field, relation in [('origin_task_ids', 'derived_from'), ('affected_task_ids', 'affects'),
                            ('regression_test_task_ids', 'regression_test')]:
                        values = row[field] or []
                        if (not isinstance(values, list) or len(values) > MAX_LINEAGE_NODES
                                or any(type(item) is not str or not item or ':' in item for item in values)):
                            raise ValueError('lineage_amendment_scope_invalid')
                        for item in values:
                            target = f'card:{item}'
                            pending.add(target)
                            edges.add(LineageRelation(ref, target, relation, f'{ref}:{field}'))
                for parent_type, parent_id, relation, field in declarations:
                    if parent_id:
                        parent = f'{parent_type}:{parent_id}'
                        pending.add(parent)
                        edges.add(LineageRelation(ref, parent, relation, f'{ref}:{field}'))
                if len(edges) > MAX_LINEAGE_EDGES:
                    raise ValueError('lineage_source_edge_bound')
                if len(set(nodes) | pending | batch) > MAX_LINEAGE_NODES:
                    raise ValueError('lineage_source_node_bound')
            if family == 'ideation' and found:
                links = (await db.execute(select(StoryIdeationLink.story_id, StoryIdeationLink.ideation_id)
                    .join(Story, Story.id == StoryIdeationLink.story_id)
                    .where(StoryIdeationLink.board_id == query.board_id, Story.board_id == query.board_id,
                        StoryIdeationLink.ideation_id.in_(found), Story.archived.is_(False))
                    .order_by(StoryIdeationLink.story_id, StoryIdeationLink.ideation_id)
                    .limit(MAX_LINEAGE_EDGES + 1))).all()
                if len(links) > MAX_LINEAGE_EDGES:
                    raise ValueError('lineage_source_edge_bound')
                for story_id, ideation_id in links:
                    story, ideation = f'story:{story_id}', f'ideation:{ideation_id}'
                    pending.add(story)
                    edges.add(LineageRelation(story, ideation, 'feeds_ideation', f'{story}:{ideation}:declared_link'))
            if family == 'story' and found:
                links = (await db.execute(select(StoryIdeationLink.story_id, StoryIdeationLink.ideation_id)
                    .join(Ideation, Ideation.id == StoryIdeationLink.ideation_id)
                    .where(StoryIdeationLink.board_id == query.board_id, Ideation.board_id == query.board_id,
                        StoryIdeationLink.story_id.in_(found))
                    .order_by(StoryIdeationLink.story_id, StoryIdeationLink.ideation_id)
                    .limit(MAX_LINEAGE_EDGES + 1))).all()
                if len(links) > MAX_LINEAGE_EDGES:
                    raise ValueError('lineage_source_edge_bound')
                for story_id, ideation_id in links:
                    story, ideation = f'story:{story_id}', f'ideation:{ideation_id}'
                    pending.add(ideation)
                    edges.add(LineageRelation(story, ideation, 'feeds_ideation', f'{story}:{ideation}:declared_link'))
            # Walk declared descendants as well as parents. Restricting the read
            # to ancestry would silently omit siblings/downstream derivations.
            children = {'spec': ((Card, 'card', 'spec_id'),),
                'card': ((Card, 'card', 'origin_task_id'),),
                'refinement': ((Spec, 'spec', 'refinement_id'),),
                'ideation': ((Refinement, 'refinement', 'ideation_id'), (Spec, 'spec', 'ideation_id'))}
            if found:
                for child_model, child_kind, field in children.get(family, ()):
                    child_ids = (await db.execute(select(child_model.id)
                        .where(child_model.board_id == query.board_id, getattr(child_model, field).in_(found))
                        .order_by(child_model.id).limit(MAX_LINEAGE_NODES + 1))).scalars().all()
                    if len(child_ids) > MAX_LINEAGE_NODES:
                        raise ValueError('lineage_source_node_bound')
                    pending.update(f'{child_kind}:{item}' for item in child_ids)
            if family in {'spec', 'card'}:
                seeds = found - {ref.split(':')[1] for ref in dependency_checked if ref.startswith(family + ':')}
                if seeds:
                    dependency_checked.update(f'{family}:{item}' for item in seeds)
                    for dependency_id, prerequisite, dependent in await _dependency_relations(db, query.board_id, family, seeds):
                        source, target = f'{family}:{prerequisite}', f'{family}:{dependent}'
                        pending.update((source, target))
                        edges.add(LineageRelation(source, target, 'precedes', f'dependency:{dependency_id}'))
        specs = {ref[5:] for ref in nodes if ref.startswith('spec:')} - amendments_checked
        if specs:
            amendments_checked.update(specs)
            amendments = (await db.execute(select(AmendmentHotfixRevision.id)
                .where(AmendmentHotfixRevision.board_id == query.board_id,
                    or_(AmendmentHotfixRevision.original_spec_id.in_(specs), AmendmentHotfixRevision.revision_spec_id.in_(specs)))
                .order_by(AmendmentHotfixRevision.id).limit(MAX_LINEAGE_NODES + 1))).scalars().all()
            if len(amendments) > MAX_LINEAGE_NODES:
                raise ValueError('lineage_source_node_bound')
            pending.update(f'amendment_hotfix_revision:{item}' for item in amendments)
        if len(edges) > MAX_LINEAGE_EDGES:
            raise ValueError('lineage_source_edge_bound')
    if query.subject_ref not in nodes:
        raise TraceabilityReadError('entity_not_found', 'Lineage subject not found or unavailable', status_code=404)
    # Dangling/foreign refs are not returned, including their identity or count.
    # The source observation remains incomplete instead of inventing endpoints.
    relations = tuple(sorted((edge for edge in edges if edge.source_ref in nodes and edge.target_ref in nodes),
        key=lambda edge: (edge.source_ref, edge.target_ref, edge.relation, edge.provenance_ref)))
    ordered_nodes = tuple(nodes[ref] for ref in sorted(nodes))
    revision = hashlib.sha256(_canonical([dependency_scope, sorted(source_rows, key=lambda row: (row[0], row[1]['id'])),
        [asdict(edge) for edge in relations], bool(missing)])).hexdigest()
    return ordered_nodes, relations, revision, bool(missing)


async def _dependency_relations(db, board_id, family, seeds):
    """Reuse the established recursive closure query for newly reached work."""
    from okto_pulse.community.adapters.sqlalchemy_traceability_read_model import _dependency_closure_edge_query
    model = Spec if family == 'spec' else Card
    dependency = SpecDependency if family == 'spec' else CardDependency
    dependent = aliased(model, name='lineage_dependent')
    prerequisite = aliased(model, name='lineage_prerequisite')
    dependent_id = dependency.dependent_spec_id if family == 'spec' else dependency.card_id
    prerequisite_id = dependency.prerequisite_spec_id if family == 'spec' else dependency.depends_on_id
    edge_query = (select(dependency.id.label('dependency_id'), prerequisite_id.label('prerequisite_id'),
        dependent_id.label('dependent_id')).select_from(dependency)
        .join(dependent, dependent.id == dependent_id).join(prerequisite, prerequisite.id == prerequisite_id)
        .where(dependent.board_id == board_id, prerequisite.board_id == board_id))
    if family == 'spec':
        edge_query = edge_query.where(dependency.board_id == board_id, dependency.active.is_(True))
    rows = (await db.execute(_dependency_closure_edge_query(
        seed_query=select(model.id.label('entity_id')).where(model.board_id == board_id, model.id.in_(seeds)),
        edge_query=edge_query))).all()
    if len(rows) > MAX_LINEAGE_EDGES:
        raise ValueError('lineage_source_edge_bound')
    return rows


async def read_lineage_snapshot(db, query, *, timeout_ms):
    try:
        async with asyncio.timeout(timeout_ms / 1000), db.begin_nested():
            first = await _collect(db, query)
            second = await _collect(db, query)
            if first != second:
                raise ValueError('lineage_source_changed')
        nodes, relations, revision, missing = second
        limitations = ['workflow_source_lineage_only', 'graph_projection_not_read']
        if missing:
            limitations.append('source_endpoint_unavailable')
        return LineageSnapshot(query.board_id, query.subject_ref, query.actor_scope_ref,
            revision, datetime.now(timezone.utc), nodes, relations, not missing, tuple(limitations))
    except TimeoutError as exc:
        raise GraphQueryTimeout('Lineage query timed out') from exc
