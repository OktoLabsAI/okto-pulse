"""Read committed context links without turning them into destination authority."""

import json

from sqlalchemy import LargeBinary, cast, func, select

from okto_pulse.core import get_storage_provider
from okto_pulse.core.ports.context_disposition import ContextDispositionPlan, ContextTarget
from okto_pulse.core.ports.historical_archive import ArchiveSection, ArchiveSourceScope
from okto_pulse.core.ports.historical_archive_read import ArchiveBoardScope, ArchiveReadLimitExceeded, ArchiveReadRequest, ArchiveReadUnavailable
from okto_pulse.core.ports.historical_context import HistoricalContextBinding, HistoricalContextItem, HistoricalContextRequest, context_target_permission_allows_read
from okto_pulse.community.adapters import context_disposition_retirement as evidence
from okto_pulse.community.adapters.historical_archive_projection import project_historical_context_record
from okto_pulse.community.adapters.historical_archive_reader import CommunityHistoricalArchiveReader
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sprint_retirement_preflight import SprintContextCandidate
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, DomainEventRow, Spec

_TABLE = DomainEventRow.__table__
_MAX_BYTES = 64 * 1024 * 1024


def _binding(row, realm_id):
    payload = row["payload_json"]
    if (row["event_type"] != evidence._BINDING or row["actor_type"] != "system" or row["actor_id"] is not None
            or payload["format"] != evidence._FORMAT):
        raise ValueError("historical_context_binding_invalid")
    return HistoricalContextBinding(row["id"], ArchiveSourceScope(realm_id, row["board_id"],
        payload["origin_kind"], payload["origin_id"]), ContextTarget.model_validate(payload["target"]),
        payload["archive_id"], payload["archive_sha256"], ArchiveSection(payload["selection"]["section"]),
        payload["selection"]["field"])


class CommunityHistoricalContextReader:
    def __init__(self, session, storage=None):
        self._session, self._storage = session, storage
        self._archives = CommunityHistoricalArchiveReader(session, storage)

    async def _target(self, target, scope):
        table = Spec.__table__ if target.kind == "spec" else Card.__table__
        found = await self._session.scalar(select(table.c.board_id).join(Board.__table__, Board.id == table.c.board_id).where(
            table.c.id == target.identity, Board.realm_id == scope.realm_id))
        if found != scope.board_id:
            raise ArchiveReadUnavailable("historical_context_target_unavailable")

    async def has_current_target_access(self, *, request, actor_kind, actor_id):
        if not await self._archives.has_current_board_access(scope=request.scope, actor_kind=actor_kind, actor_id=actor_id):
            return False
        await self._target(request.target, request.scope)
        if actor_kind == "agent":
            resolved = await CommunityAgentAuthenticationGateway(self._session).resolve_agent_permission_context(
                actor_id, board_id=request.scope.board_id)
            return resolved is not None and context_target_permission_allows_read(request.target, resolved.permissions)
        return actor_kind == "human"

    async def _rows(self, *where):
        count, size = (await self._session.execute(select(func.count(), func.coalesce(func.sum(
            func.length(cast(_TABLE.c.payload_json, LargeBinary))), 0)).where(*where))).one()
        if count > 100_000 or size > _MAX_BYTES:
            raise ArchiveReadLimitExceeded("historical_context_candidate_limit")
        return [{key: value for key, value in row.items() if key != "occurred_at"}
            for row in (await self._session.execute(select(_TABLE).where(*where))).mappings()]

    async def list_bindings(self, *, request, actor_kind, actor_id):
        if not await self.has_current_target_access(request=request, actor_kind=actor_kind, actor_id=actor_id):
            raise ArchiveReadUnavailable("historical_context_authority_changed")
        try:
            rows = await self._rows(_TABLE.c.board_id == request.scope.board_id,
                _TABLE.c.event_type == evidence._BINDING,
                _TABLE.c.payload_json["target"]["kind"].as_string() == request.target.kind,
                _TABLE.c.payload_json["target"]["identity"].as_string() == request.target.identity)
            return tuple(_binding(row, request.scope.realm_id) for row in rows)
        except (KeyError, ValueError, TypeError, IndexError, RecursionError) as exc:
            raise ArchiveReadUnavailable("historical_context_binding_unavailable") from exc

    async def read_binding(self, *, binding, grant):
        request = ArchiveReadRequest(binding.scope, binding.section, 0, 1)
        await self._archives._require_snapshot()
        if binding.archive_id != grant.archive_id or binding.archive_sha256 != grant.archive_sha256:
            raise ArchiveReadUnavailable("historical_context_archive_mismatch")
        target_request = HistoricalContextRequest(ArchiveBoardScope(binding.scope.realm_id, binding.scope.board_id), binding.target)
        if not await self.has_current_target_access(request=target_request,
                actor_kind=grant.captured.actor_kind, actor_id=grant.captured.actor_id):
            raise ArchiveReadUnavailable("historical_context_authority_changed")
        # Rechecks the exact current grant + Board before opening ANY private file.
        reference, document = await self._archives._authorized_document(request=request, grant=grant)
        try:
            rows = await self._rows(_TABLE.c.id == binding.identity, _TABLE.c.board_id == binding.scope.board_id,
                _TABLE.c.event_type == evidence._BINDING)
            if len(rows) != 1 or _binding(rows[0], binding.scope.realm_id) != binding:
                raise ValueError("historical_context_binding_changed")
            row, = rows
            payload = row["payload_json"]
            if reference.event_id != binding.archive_id or reference.sha256 != binding.archive_sha256:
                raise ValueError("historical_context_archive_mismatch")
            manifests = await self._rows(_TABLE.c.id == payload["audit_id"],
                _TABLE.c.board_id == binding.scope.board_id, _TABLE.c.event_type == evidence._COMMITTED)
            records, _ = await evidence._evidence(self._storage or get_storage_provider(), manifests, (reference,))
            selected = [record for record in records if record["event_type"] == evidence._EVENT
                and record["payload_json"]["decision"]["candidate_sha256"] == payload["candidate_sha256"]]
            if len(selected) != 1:
                raise ValueError("historical_context_candidate_missing")
            record, = selected
            source = record["payload_json"]
            raw = source["candidate"]
            candidate = SprintContextCandidate(**{**raw,
                "key": tuple(tuple(pair) for pair in raw["key"]), "path": tuple(raw["path"])})
            plan = ContextDispositionPlan.model_validate_json(json.dumps({"migration_id": reference.migration_id,
                "decision_reference": source["decision_reference"], "decisions": [source["decision"]]}))
            _, expected = evidence._records((reference,), {reference.board_id: document}, (candidate,), plan)
            if record != expected[0] or row not in evidence._bindings(expected):
                raise ValueError("historical_context_evidence_mismatch")
            projection = project_historical_context_record(document, request, reference.event_id, payload["selection"])
            return HistoricalContextItem(binding, projection)
        except (KeyError, ValueError, TypeError, IndexError, OSError, RecursionError) as exc:
            raise ArchiveReadUnavailable("historical_context_source_unavailable") from exc
