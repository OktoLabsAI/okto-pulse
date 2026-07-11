"""Shared FastAPI inbound dependencies (SaaS Refactor spec #04; R01B FR3).

``get_unit_of_work`` is the request-scoped persistence dependency for migrated
REST flows. The edition-owned ``UnitOfWorkFactory`` resolves both the realm and
the transaction lifecycle. The inbound adapter receives only the public UoW;
no database session, engine or repository implementation crosses this boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request

from okto_pulse.core.ports.scheduler import SchedulerControl
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.runtime_registry import resolve_unit_of_work_factory


def scheduler_control_from_request(request: Request) -> SchedulerControl | None:
    """Return the edition-owned scheduler port from the request composition."""
    composition = getattr(request.app.state, "runtime_composition", None)
    return (
        getattr(composition, "scheduler_control", None)
        if composition is not None
        else None
    )


async def get_unit_of_work(
    request: Request,
) -> AsyncIterator[PulseUnitOfWork]:
    """Yield one edition-owned UoW for the complete HTTP request."""
    composition = getattr(request.app.state, "runtime_composition", None)
    preferred = (
        getattr(composition, "uow_factory", None) if composition is not None else None
    )
    try:
        factory = resolve_unit_of_work_factory(preferred=preferred)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "persistence_provider_not_configured",
                "message": str(exc),
            },
        ) from exc
    realm_resolver = getattr(factory, "resolve_realm_scope", None)
    if not callable(realm_resolver):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "realm_provider_not_configured",
                "message": "The edition UoW factory cannot resolve a realm scope.",
            },
        )
    try:
        realm_scope = realm_resolver()
        async with factory(realm_scope=realm_scope) as uow:
            yield uow
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "persistence_provider_not_configured",
                "message": str(exc),
            },
        ) from exc
