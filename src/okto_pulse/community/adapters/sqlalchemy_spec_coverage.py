"""One authorized Spec snapshot and the existing delivery-gate rollup, read-only."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time
from types import SimpleNamespace

from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    Spec, Card, DeliveryEvidenceRecordRow, CardDeliveryEvidenceRecordRow,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout, GraphCapabilityUnavailable, GraphUnavailable
from okto_pulse.core.kg.blocking_io import run_blocking_graph_io
from okto_pulse.core.application.kg_runtime_access import resolve_spec_coverage_graph_read, resolve_graph_query_execution
from okto_pulse.core.ports.spec_coverage_query import (
    MAX_SPEC_COVERAGE_ITEMS, MAX_SPEC_COVERAGE_FACTS, SpecCoverageSnapshot, SpecCoverageGraphFacts,
)


_SPEC_FIELDS = ('id', 'board_id', 'edition', 'version', 'title', 'status', 'archived',
    'test_scenarios', 'skip_test_coverage', 'skip_rules_coverage', 'skip_decisions_coverage',
    'skip_ir_coverage', 'skip_or_coverage', *(name for _, name in COLLECTIONS))
_CARD_FIELDS = ('id', 'board_id', 'spec_id', 'card_type', 'status', 'archived', 'policy_version', 'test_scenario_ids')


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return getattr(value, 'value', value)


def _revision(source, cards):
    return hashlib.sha256(json.dumps([source, cards], default=_json_value,
        sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


class CommunitySpecCoverageReader:
    def __init__(self, session, *, delivery_store=None, graph_reader=None, query_execution=None):
        self._session = session
        self._delivery = delivery_store if delivery_store is not None else CommunityDeliveryEvidenceStore(session)
        self._graph_reader = graph_reader
        self._query_execution = query_execution

    async def read_graph(self, query, scope, source_revision, *, timeout_ms):
        if not query.read_graph:
            raise ValueError('spec_coverage_graph_outside_authority')
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30000:
            raise ValueError('query_timeout_ms_requires_1_to_30000')
        deadline = time.monotonic() + timeout_ms / 1000

        def remaining():
            value = int((deadline - time.monotonic()) * 1000)
            if value <= 0:
                raise GraphQueryTimeout('Spec coverage graph deadline exceeded.')
            return value

        async def check_source():
            try:
                async with asyncio.timeout(remaining() / 1000):
                    if _revision(*(await self._source(query))) != source_revision:
                        raise ValueError('spec_coverage_source_changed')
            except TimeoutError as exc:
                raise GraphQueryTimeout('Spec coverage source deadline exceeded.') from exc

        await check_source()
        try:
            reader = self._graph_reader or resolve_spec_coverage_graph_read()
            execution = self._query_execution or resolve_graph_query_execution()

            def observe():
                with execution.scope(query.board_id, timeout_ms=remaining()):
                    return reader.read_spec_coverage_graph(query.board_id, scope)

            observed = await run_blocking_graph_io(observe, task_name='community.spec_coverage.read')
        except (GraphCapabilityUnavailable, GraphUnavailable):
            observed = SpecCoverageGraphFacts(state='unavailable')
        await check_source()
        remaining()
        return observed

    async def _source(self, query):
        spec = (await self._session.execute(select(*(getattr(Spec, key) for key in _SPEC_FIELDS))
            .where(Spec.board_id == query.board_id, Spec.id == query.spec_id, Spec.archived.is_(False)))).one_or_none()
        if spec is None:
            raise EntityNotFoundError('spec', query.spec_id)
        cards = (await self._session.execute(select(*(getattr(Card, key) for key in _CARD_FIELDS))
            .where(Card.board_id == query.board_id, Card.spec_id == query.spec_id)
            .order_by(Card.id).limit(MAX_SPEC_COVERAGE_ITEMS + 1))).all()
        source = dict(spec._mapping)
        collections = [source.get(name) or [] for _, name in COLLECTIONS] + [source.get('test_scenarios') or []]
        if any(type(items) is not list for items in collections):
            raise ValueError('spec_coverage_source_invalid')
        if len(cards) > MAX_SPEC_COVERAGE_ITEMS or sum(map(len, collections)) > MAX_SPEC_COVERAGE_ITEMS:
            raise ValueError('spec_coverage_source_bound')
        return source, [dict(card._mapping) for card in cards]

    async def _proof_bound(self, query, edition):
        count = 0
        for model in (DeliveryEvidenceRecordRow, CardDeliveryEvidenceRecordRow):
            edition_column = model.edition if model is DeliveryEvidenceRecordRow else model.spec_edition
            count += await self._session.scalar(select(func.count()).select_from(model).where(
                model.board_id == query.board_id, model.spec_id == query.spec_id,
                edition_column == edition))
        if count > MAX_SPEC_COVERAGE_FACTS:
            raise ValueError('spec_coverage_delivery_bound')

    async def read(self, query, *, timeout_ms):
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30000:
            raise ValueError('query_timeout_ms_requires_1_to_30000')
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                # A read savepoint pins the SQLite snapshot without a Board
                # no-op UPDATE, semantic fence or any projection/repair write.
                # Its cleanup is awaited before a timeout leaves this method.
                async with self._session.begin_nested():
                    source, cards = await self._source(query)
                    scope = DeliveryScope(query.board_id, query.spec_id, source['edition'])
                    proof = None
                    if query.read_delivery:
                        await self._proof_bound(query, scope.edition)
                        # Same rollup as require_spec_delivery, including the
                        # legacy/adopted dispatch and authenticated proof heads.
                        proof, _ = await self._delivery.load_rollup_snapshot(query.board_id, query.spec_id)
                    if (await self._source(query)) != (source, cards):
                        raise ValueError('spec_coverage_source_changed')
                    revision = _revision(source, cards)
                    return SpecCoverageSnapshot(scope, query.actor_scope_ref, revision, datetime.now(timezone.utc),
                        SimpleNamespace(**source), tuple(SimpleNamespace(**row) for row in cards), True,
                        proof, 'available' if query.read_delivery else 'restricted')
        except TimeoutError as exc:
            raise GraphQueryTimeout('Spec coverage read deadline exceeded.') from exc
