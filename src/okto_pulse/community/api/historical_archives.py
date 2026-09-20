"""Read-only historical sections; authority and projection remain behind Core ports."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.application.use_cases.historical_archive import DiscoverHistoricalArchivesUseCase, ReadHistoricalArchiveSectionUseCase
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.historical_archive import ArchiveSection, ArchiveSourceScope
from okto_pulse.core.ports.historical_archive_read import ArchiveBoardScope, ArchiveDiscoveryRequest, ArchiveReadLimitExceeded, ArchiveReadRequest, ArchiveReadUnavailable
from okto_pulse.core.repositories.interfaces.unit_of_work import ConsistentReadContractError, UnitOfWorkFactory
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work_factory
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract


router = APIRouter()


@router.get("/boards/{board_id}/historical-archives")
async def discover_historical_archives(
    response: Response,
    board_id: str = Path(min_length=1, max_length=255),
    offset: int = Query(0, ge=0, le=100_000),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_principal),
    factory: UnitOfWorkFactory = Depends(get_unit_of_work_factory),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    actor = RESTAdapterContract.actor_from_principal(principal, board_id=board_id)
    try:
        request = ArchiveDiscoveryRequest(ArchiveBoardScope(actor.require_realm_scope().realm_id, board_id), offset, limit)
    except ValueError as exc:
        raise HTTPException(422, detail="Historical archive request invalid") from exc
    try:
        async with factory(realm_scope=factory.resolve_realm_scope(), actor=actor) as uow:
            page = await DiscoverHistoricalArchivesUseCase().execute(request, actor=actor, uow=uow)
        return {"format": "historical-archive-discovery/v1", "board_id": board_id,
            "items": [{"origin": {"kind": item.scope.origin_kind, "id": item.scope.origin_id},
                       "archive_id": item.archive_id, "sections": [section.value for section in item.sections]}
                      for item in page.items], "next_offset": page.next_offset}
    except EntityNotFoundError as exc:
        raise HTTPException(404, detail="Historical archive not found", headers={"Cache-Control": "no-store"}) from exc
    except ArchiveReadLimitExceeded as exc:
        raise HTTPException(413, detail="Historical archive discovery limit exceeded", headers={"Cache-Control": "no-store"}) from exc
    except (ArchiveReadUnavailable, ConsistentReadContractError) as exc:
        raise HTTPException(503, detail="Historical archive unavailable", headers={"Cache-Control": "no-store"}) from exc


@router.get("/boards/{board_id}/historical-archives/{origin_kind}/{origin_id}/{section}")
async def read_historical_archive_section(
    response: Response,
    board_id: str = Path(min_length=1, max_length=255),
    origin_kind: str = Path(min_length=1, max_length=255),
    origin_id: str = Path(min_length=1, max_length=255),
    section: ArchiveSection = Path(),
    offset: int = Query(0, ge=0, le=100_000),
    limit: int = Query(100, ge=1, le=200),
    principal: Principal = Depends(require_principal),
    factory: UnitOfWorkFactory = Depends(get_unit_of_work_factory),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    actor = RESTAdapterContract.actor_from_principal(principal, board_id=board_id)
    try:
        request = ArchiveReadRequest(ArchiveSourceScope(actor.require_realm_scope().realm_id,
            board_id, origin_kind, origin_id), section, offset, limit)
    except ValueError as exc:
        raise HTTPException(422, detail="Historical archive request invalid") from exc
    try:
        async with factory(realm_scope=factory.resolve_realm_scope(), actor=actor) as uow:
            page = await ReadHistoricalArchiveSectionUseCase().execute(request, actor=actor, uow=uow)
        return {"format": "historical-archive-section/v1", "board_id": board_id,
            "origin": {"kind": origin_kind, "id": origin_id}, "section": section.value,
            "archive_id": page.archive_id, "records": page.records(), "next_offset": page.next_offset}
    except EntityNotFoundError as exc:
        raise HTTPException(404, detail="Historical archive not found", headers={"Cache-Control": "no-store"}) from exc
    except ArchiveReadLimitExceeded as exc:
        raise HTTPException(413, detail="Historical archive page limit exceeded", headers={"Cache-Control": "no-store"}) from exc
    except (ArchiveReadUnavailable, ConsistentReadContractError) as exc:
        raise HTTPException(503, detail="Historical archive unavailable", headers={"Cache-Control": "no-store"}) from exc
