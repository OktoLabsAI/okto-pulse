"""Current ACL checks and verified historical projection inside one UoW snapshot."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.core import StorageProvider, get_storage_provider
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.ports.historical_archive import ArchiveGrantState, ArchiveSourceScope, archive_section_is_readable
from okto_pulse.core.ports.historical_archive_read import ArchiveBoardScope, ArchiveReadRequest, ArchiveReadUnavailable, ArchiveSectionPage
from okto_pulse.core.ports.permission_policy import board_membership_allows_read
from okto_pulse.community.adapters.historical_archive_grants import CommunityHistoricalArchiveGrants
from okto_pulse.community.adapters.historical_archive_projection import project_historical_archive_section
from okto_pulse.community.adapters.relational_application import CommunityAgentAuthenticationGateway
from okto_pulse.community.adapters.sqlalchemy_models import Board, BoardShare, DomainEventRow
from okto_pulse.community.adapters.sprint_retirement_archive import HistoricalArchiveReference, verify_historical_archive
from okto_pulse.community.auth import LocalAuthProvider


class CommunityHistoricalArchiveReader:
    def __init__(self, session: AsyncSession, storage: StorageProvider | None = None):
        self._session = session
        self._storage = storage

    async def _require_snapshot(self):
        connection = await self._session.connection()
        if connection.dialect.name != "sqlite" or not await connection.run_sync(
            lambda sync: bool(sync.connection.driver_connection.in_transaction)
        ):
            raise ArchiveReadUnavailable("historical_archive_snapshot_required")

    async def has_current_board_access(self, *, scope: ArchiveSourceScope | ArchiveBoardScope, actor_kind: str, actor_id: str) -> bool:
        await self._require_snapshot()
        if scope.realm_id != LOCAL_REALM_ID:
            return False
        board = (await self._session.execute(select(Board.realm_id, Board.owner_id).where(
            Board.id == scope.board_id,
        ))).one_or_none()
        if board is None or (board.realm_id or LOCAL_REALM_ID) != scope.realm_id:
            return False
        if actor_kind == "agent":
            resolved = await CommunityAgentAuthenticationGateway(self._session).resolve_agent_permission_context(
                actor_id, board_id=scope.board_id,
            )
            return resolved is not None and not resolved.permissions.owner_review_required
        if actor_kind != "human":
            return False
        principal = await LocalAuthProvider().authenticate(None)
        if principal.subject != actor_id or principal.realm_id != scope.realm_id:
            return False
        if board_membership_allows_read(owner_id=board.owner_id, actor_id=actor_id):
            return True
        share = (await self._session.execute(select(BoardShare.permission).where(
            BoardShare.board_id == scope.board_id, BoardShare.realm_id == scope.realm_id,
            BoardShare.user_id == actor_id,
        ))).scalar_one_or_none()
        return board_membership_allows_read(owner_id=board.owner_id, actor_id=actor_id, share_permission=share)

    async def list_grants(self, *, scope: ArchiveBoardScope, actor_kind: str, actor_id: str) -> tuple[ArchiveGrantState, ...]:
        await self._require_snapshot()
        if not await self.has_current_board_access(scope=scope, actor_kind=actor_kind, actor_id=actor_id):
            raise ArchiveReadUnavailable("historical_archive_authority_changed")
        return await CommunityHistoricalArchiveGrants(self._session).list_for_board(
            scope=scope, actor_kind=actor_kind, actor_id=actor_id)

    async def read_section(self, *, request: ArchiveReadRequest, grant: ArchiveGrantState) -> ArchiveSectionPage:
        _, document = await self._authorized_document(request=request, grant=grant)
        try:
            return project_historical_archive_section(document, request, grant.archive_id)
        except (KeyError, ValueError, TypeError, IndexError, RecursionError) as exc:
            raise ArchiveReadUnavailable("historical_archive_source_unavailable") from exc

    async def _authorized_document(self, *, request: ArchiveReadRequest, grant: ArchiveGrantState):
        """Adapter-local verified source, never part of the Core public port."""
        await self._require_snapshot()
        # Defense in depth for composed server callers: a detached old state or
        # forged grant cannot bypass current scoped authority through this port.
        subject = grant.captured
        current = await CommunityHistoricalArchiveGrants(self._session).get(scope=request.scope,
            actor_kind=subject.actor_kind, actor_id=subject.actor_id)
        if (current != grant or not archive_section_is_readable(subject, scope=request.scope,
                actor_kind=subject.actor_kind, actor_id=subject.actor_id, section=request.section,
                current_board_access=await self.has_current_board_access(scope=request.scope,
                    actor_kind=subject.actor_kind, actor_id=subject.actor_id),
                current_section_permission=grant.sections.allows(request.section))):
            raise ArchiveReadUnavailable("historical_archive_authority_changed")
        try:
            event = (await self._session.execute(select(DomainEventRow.__table__).where(
                DomainEventRow.id == grant.archive_id, DomainEventRow.board_id == request.scope.board_id,
                DomainEventRow.event_type == "historical_archive.created",
            ))).mappings().one_or_none()
            if event is None:
                raise ValueError("historical_archive_committed_reference_required")
            payload = event["payload_json"]
            if (set(payload) != {"format", "migration_id", "storage_path", "sha256", "size", "counts"}
                    or payload["format"] != "historical-relational-archive/v4"
                    or payload["sha256"] != grant.archive_sha256):
                raise ValueError("historical_archive_source_mismatch")
            reference = HistoricalArchiveReference(grant.archive_id, request.scope.board_id,
                payload["migration_id"], payload["storage_path"], payload["sha256"], payload["size"],
                tuple(tuple(pair) for pair in payload["counts"]))
            document = await verify_historical_archive(self._storage or get_storage_provider(), reference)
            from okto_pulse.core.ports.historical_archive import parse_archive_read_grant

            captured = [parse_archive_read_grant(value) for value in document["access"]["grants"]]
            if subject not in captured:
                raise ValueError("historical_archive_grant_source_mismatch")
            return reference, document
        except (KeyError, ValueError, TypeError, IndexError, OSError, RecursionError) as exc:
            # No path, raw SQL, permission manifest or content in transport errors.
            raise ArchiveReadUnavailable("historical_archive_source_unavailable") from exc
