"""Internal installation of captured F2A authority; never a product endpoint.

Capture and installation use Core's frozen v0.3.4 authority evaluator. Removing
the live source registry must not change historical access or its replay check.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import uuid

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from okto_pulse.core import StorageProvider
from okto_pulse.core.ports.historical_archive import parse_archive_read_grant
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow
from okto_pulse.community.adapters.historical_archive_grants import _TABLE, _key, _where, _decode
from okto_pulse.community.adapters.sprint_retirement_access import capture_archive_access
from okto_pulse.community.adapters.sprint_retirement_archive import (
    HistoricalArchiveReference,
    verify_historical_archive,
)

_INSTALL_EVENT = "historical_archive.grants_installed"
_NAMESPACE = uuid.UUID("4a572353-cde7-52fd-9101-baa059f36c1c")


async def install_historical_archive_grants(
    engine: AsyncEngine, storage: StorageProvider, reference: HistoricalArchiveReference,
) -> int:
    """Install one verified archive's exact grants, atomically and fail closed.

    Call only from the fenced migration coordinator, never a product transport.
    No identity, preset, Board override or source entity is modified. Replays
    verify the original population and provenance but preserve current sections.
    A different archive for an already installed origin requires reconciliation.
    """
    if engine.dialect.name != "sqlite":
        raise ValueError("historical_archive_backend_unsupported")
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            result = await _install_historical_archive_grants(connection, storage, reference)
            await connection.commit()
            return result
        except BaseException:
            await connection.rollback()
            raise


async def _install_historical_archive_grants(
    connection: AsyncConnection, storage: StorageProvider, reference: HistoricalArchiveReference, *,
    require_existing: bool = False,
) -> int:
    """Shared cutover step; the caller owns the transaction and SQLite fence."""
    if not connection.in_transaction():
        raise ValueError("historical_archive_transaction_required")
    event = (await connection.execute(select(DomainEventRow.__table__).where(
        DomainEventRow.id == reference.event_id,
    ))).mappings().one_or_none()
    expected = {"format": "historical-relational-archive/v4",
        "migration_id": reference.migration_id, "storage_path": reference.storage_path,
        "sha256": reference.sha256, "size": reference.size,
        "counts": [list(pair) for pair in reference.counts]}
    if (event is None or event["event_type"] != "historical_archive.created"
            or event["board_id"] != reference.board_id or event["payload_json"] != expected):
        raise ValueError("historical_archive_committed_reference_required")
    document = await verify_historical_archive(storage, reference)
    grants = tuple(parse_archive_read_grant(value) for value in document["access"]["grants"])
    installation_id = str(uuid.uuid5(_NAMESPACE, reference.event_id))
    installation = (await connection.execute(select(DomainEventRow.__table__).where(
        DomainEventRow.id == installation_id,
    ))).mappings().one_or_none()
    payload = {"format": "historical-archive-grants/v1", "archive_id": reference.event_id,
        "archive_sha256": reference.sha256, "grant_count": len(grants)}
    replay = installation is not None
    if require_existing and not replay:
        raise ValueError("historical_archive_installation_required")
    if replay and (installation["event_type"] != _INSTALL_EVENT
            or installation["board_id"] != reference.board_id or installation["payload_json"] != payload):
        raise ValueError("historical_archive_installation_mismatch")
    if not replay:
        # The archive may have been captured in an earlier transaction.
        # Never materialize stale authority after a policy/Board change.
        current = await capture_archive_access(connection,
            origins={reference.board_id: tuple(sorted({g.scope.origin_id for g in grants}))},
            max_rows=100_000, max_bytes=64 * 1024 * 1024)
        if current[reference.board_id] != document["access"]:
            raise ValueError("historical_archive_authority_changed")
    population = (await connection.execute(select(func.count()).select_from(_TABLE).where(
        _TABLE.c.archive_id == reference.event_id,
    ))).scalar_one()
    if population != (len(grants) if replay else 0):
        raise ValueError("historical_archive_grant_population_mismatch")
    for grant in grants:
        key = _key(grant.scope, grant.actor_kind, grant.actor_id)
        row = (await connection.execute(select(_TABLE).where(*_where(key)))).mappings().one_or_none()
        if replay:
            if row is None:
                raise ValueError("historical_archive_grant_population_mismatch")
            state = _decode(row)
            if (state.captured != grant or state.archive_id != reference.event_id
                    or state.archive_sha256 != reference.sha256):
                raise ValueError("historical_archive_grant_provenance_mismatch")
        else:
            if row is not None:
                raise ValueError("historical_archive_origin_already_installed")
            await connection.execute(insert(_TABLE).values(**key,
                archive_id=reference.event_id, archive_sha256=reference.sha256,
                captured_sections=asdict(grant.sections), sections=asdict(grant.sections), revision=1))
    if not replay:
        await connection.execute(insert(DomainEventRow.__table__).values(
            id=installation_id, board_id=reference.board_id, event_type=_INSTALL_EVENT,
            actor_type="system", actor_id=None, occurred_at=datetime.now(timezone.utc),
            payload_json=payload,
        ))
    return len(grants)
