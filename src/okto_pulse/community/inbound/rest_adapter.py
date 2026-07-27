"""RESTAdapterContract (spec #09, tr_5ba8fbac).

Maps a REST request to an ``ActorContext`` and use case errors to
``HTTPException``, with NO business rule. ``http_error`` reproduces the exact
status codes / detail payloads the legacy first-cut handlers produced
(CommandValidationError→400, EntityNotFoundError→404, AmbiguityGateError→400,
ResourceGateError→409 with the {error,message,details} envelope, ValueError→409).
Each route catches only the error types it currently handles and routes them
through ``http_error`` so its observable behavior is preserved; anything else
propagates unchanged.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from okto_pulse.core.application.use_cases import (
    ActorContext,
    CommandValidationError,
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.base import actor_context_from_principal
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.application.errors import AmbiguityGateError
from okto_pulse.core.services.resource_gate import ResourceGateError


class RESTAdapterContract:
    """Thin REST inbound adapter contract."""

    source = "rest"

    @staticmethod
    def actor(
        user_id: str,
        *,
        realm_id: str | None = None,
        board_id: str | None = None,
        permissions=None,
        roles: tuple[str, ...] = (),
    ) -> ActorContext:
        """Build the transport-neutral actor from REST auth dependencies."""
        return ActorContext(
            user_id,
            "rest",
            board_id=board_id,
            # Community is local-first by product definition. Other editions
            # provide their own inbound adapter and must pass an explicit realm.
            realm_id=realm_id or LOCAL_REALM_ID,
            permissions=permissions,
            roles=roles,
        )

    @staticmethod
    def actor_from_principal(
        principal: Principal,
        *,
        board_id: str | None = None,
    ) -> ActorContext:
        """Build an actor from the normalized authentication contract."""
        return actor_context_from_principal(
            principal,
            source="rest",
            board_id=board_id,
        )

    @staticmethod
    def http_error(exc: Exception, *, not_found_detail: str = "Not found") -> HTTPException:
        """Map a transport-neutral / domain error to the legacy HTTPException.

        Order matters: AmbiguityGateError and ResourceGateError are ValueError
        subclasses and must be matched before the plain ValueError fallback.
        """
        if isinstance(exc, CommandValidationError):
            return HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        if isinstance(exc, EntityNotFoundError):
            return HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=not_found_detail
            )
        if isinstance(exc, PermissionDeniedError):
            return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
        if isinstance(exc, AmbiguityGateError):
            return HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        if isinstance(exc, ResourceGateError):
            return HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": exc.code, "message": str(exc), "details": exc.details},
            )
        if isinstance(exc, ValueError):
            return HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )
        # Not an error this contract owns — re-raise for the framework (500).
        raise exc
