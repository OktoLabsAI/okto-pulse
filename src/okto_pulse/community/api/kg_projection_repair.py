"""Bounded deterministic projection repair; never starts cognitive closeout."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.community.api.deps import get_unit_of_work, scheduler_control_from_request
from okto_pulse.community.api.kg_routes import require_kg_board_writer_actor
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import (
    ActorContext, CommandValidationError, ConflictError, EntityNotFoundError, PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.deterministic_projection_repair import (
    RepairSpecProjectionCommand, RepairSpecProjectionUseCase,
)
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter(prefix="/kg/boards/{board_id}/deterministic-projection")


class RepairSpecProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec_ids: list[str] = Field(min_length=1, max_length=25)
    reason: str = Field(min_length=3, max_length=1000)


@router.post("/repair", status_code=202)
async def repair_spec_projection(
    board_id: str, body: RepairSpecProjectionRequest, request: Request,
    actor: ActorContext = Depends(require_kg_board_writer_actor),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, object]:
    """Queue exact canonical-eligible specs through the normal deterministic worker.

    HTTP 202 is admission, not completion. Cognitive ledger entries remain open.
    Active, paused, fenced and exact-rebuild rows are not taken over by repair.
    """
    try:
        return await RepairSpecProjectionUseCase().execute(
            RepairSpecProjectionCommand(
                board_id, tuple(body.spec_ids), body.reason,
                scheduler_control_from_request(request),
            ),
            actor=actor, uow=uow,
        )
    except (EntityNotFoundError, PermissionDeniedError) as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CommandValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
