"""Transaction-bound source inventory plus optional bounded Grafx observations."""
from datetime import datetime, timezone
import hashlib
import json
import time

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from okto_pulse.community.adapters.sqlalchemy_models import Card, DomainEventRow, Spec
from okto_pulse.core.application.kg_runtime_access import (
    resolve_bug_clusters_graph_read, resolve_graph_query_execution,
)
from okto_pulse.core.kg.blocking_io import run_blocking_graph_io
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable, GraphUnavailable, GraphQueryTimeout
from okto_pulse.core.ports.bug_clusters import BugClustersSnapshot, ClusterBugFact, MAX_CLUSTER_BUGS
from okto_pulse.core.ports.consolidation import CardLifecycleTransition, card_resolution_timestamp
from okto_pulse.core.ports.card_projection import bug_origin_proxy_target_spec


def _utc(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _value(value):
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    return getattr(value, 'value', value)


class CommunityBugClustersReader:
    def __init__(self, session, *, graph_reader=None, query_execution=None):
        self._session = session
        self._graph_reader = graph_reader
        self._query_execution = query_execution

    async def _inventory(self, query):
        # The window count and bounded rows share a single SQL statement. No
        # full Card bodies, validation histories or reports cross this boundary.
        filters = [Card.board_id == query.board_id, Card.card_type == 'bug',
            func.julianday(Card.created_at) >= func.julianday(query.window.from_inclusive),
            func.julianday(Card.created_at) < func.julianday(query.window.to_exclusive)]
        if query.status is not None:
            filters.append(Card.status == query.status)
        if query.severity is not None:
            filters.append(Card.severity == query.severity)
        origin = aliased(Card)
        direct_spec = aliased(Spec)
        rows = (await self._session.execute(select(
            Card.id, Card.title, Card.created_at, Card.status, Card.severity, origin.spec_id.label('spec_id'),
            Card.policy_version, Card.updated_at, Card.origin_task_id, Card.archived,
            func.count().over().label('scope_count'), Spec.board_id.label('spec_board_id'),
            origin.board_id.label('origin_board_id'), origin.policy_version.label('origin_policy_version'),
            origin.updated_at.label('origin_updated_at'), Card.spec_id.label('bug_spec_id'),
            direct_spec.board_id.label('bug_spec_board_id'),
        ).select_from(Card).outerjoin(origin, Card.origin_task_id == origin.id)
            .outerjoin(Spec, origin.spec_id == Spec.id).outerjoin(direct_spec, Card.spec_id == direct_spec.id)
            .where(*filters).order_by(Card.id).limit(MAX_CLUSTER_BUGS))).all()
        if any((row.spec_id is not None and row.spec_board_id != query.board_id)
               or (row.origin_task_id is not None and row.origin_board_id != query.board_id)
               or (row.bug_spec_id is not None and row.bug_spec_board_id != query.board_id) for row in rows):
            raise ValueError('bug_clusters_source_link_outside_scope')
        return tuple(tuple(_value(value) for value in row) for row in rows)

    async def _resolutions(self, board_id, ids):
        if not ids:
            return {}
        card_id = DomainEventRow.payload_json['card_id'].as_string()
        ranked = select(DomainEventRow.id, DomainEventRow.occurred_at,
            card_id.label('card_id'), DomainEventRow.payload_json['from_status'].as_string().label('from_status'),
            DomainEventRow.payload_json['to_status'].as_string().label('to_status'),
            func.row_number().over(partition_by=card_id,
                order_by=(DomainEventRow.occurred_at.desc(), DomainEventRow.id.desc())).label('rank'),
        ).where(DomainEventRow.board_id == board_id, DomainEventRow.event_type == 'card.moved', card_id.in_(ids)).subquery()
        rows = (await self._session.execute(select(ranked).where(ranked.c.rank <= 2)
            .order_by(ranked.c.card_id, ranked.c.rank))).all()
        grouped = {}
        for row in rows:
            grouped.setdefault(row.card_id, []).append(CardLifecycleTransition(
                row.id, _utc(row.occurred_at), row.from_status, row.to_status))
        return {key: _utc(card_resolution_timestamp('done', tuple(transitions)))
                for key, transitions in grouped.items()}

    async def read(self, query, *, timeout_ms):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30000:
            raise ValueError('query_timeout_ms_requires_1_to_30000')
        deadline = time.monotonic() + timeout_ms / 1000

        def remaining():
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0:
                raise GraphQueryTimeout('Bug cluster read deadline exceeded.')
            return left

        inventory = await self._inventory(query)
        remaining()
        resolutions = await self._resolutions(query.board_id, tuple(row[0] for row in inventory if row[3] == 'done'))
        remaining()
        bugs = tuple(ClusterBugFact(row[0], row[1], _utc(row[2]), row[3], row[4],
            f'spec:{row[5]}' if row[5] else None,
            hashlib.sha256(json.dumps(row, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
            resolutions.get(row[0])) for row in inventory)
        count = inventory[0][10] if inventory else 0
        graph = None
        available = True
        if query.group_by in {'proxy', 'learning'}:
            try:
                reader = self._graph_reader or resolve_bug_clusters_graph_read()
                execution = self._query_execution or resolve_graph_query_execution()
                def read_graph():
                    with execution.scope(query.board_id, timeout_ms=remaining()):
                        return reader.read_bug_cluster_graph(query.board_id, bugs, group_by=query.group_by)
                # Existing bridge drains the owned worker on cancellation. The
                # native provider receives the same finite execution deadline.
                graph = await run_blocking_graph_io(read_graph, task_name='community.bug_clusters.read')
                if query.group_by == 'proxy':
                    parents = sorted({bug_origin_proxy_target_spec(item.target_ref) for item in graph.associations})
                    for offset in range(0, len(parents), 400):
                        remaining()
                        wanted = parents[offset:offset + 400]
                        allowed = set((await self._session.execute(select(Spec.id).where(
                            Spec.board_id == query.board_id, Spec.id.in_(wanted)))).scalars())
                        if allowed != set(wanted):
                            raise ValueError('bug_clusters_target_outside_scope')
            except (GraphCapabilityUnavailable, GraphUnavailable):
                available = False
        remaining()
        if await self._inventory(query) != inventory:
            raise ValueError('bug_clusters_source_changed')
        remaining()
        return BugClustersSnapshot(query.board_id, query.actor_scope_ref, datetime.now(timezone.utc),
            bugs, count, count == len(bugs),
            associations=graph.associations if graph else (), graph_available=available,
            graph_generation=graph.graph_generation if graph else None,
            projected_bug_ids=graph.projected_bug_ids if graph else (),
            associations_truncated=graph.truncated if graph else False)
