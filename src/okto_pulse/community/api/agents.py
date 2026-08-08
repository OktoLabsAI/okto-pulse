"""Agent API endpoints.

Spec R01A (REST strangler): every endpoint routes through a transport-free
application use case via ``get_unit_of_work`` — no raw ``AsyncSession``/``get_db``
in this adapter. The MCP permission-cache invalidation stays in the adapter and
ONLY on the two proven invalidation points (``update_agent`` /
``update_board_overrides``, ac_8e695cf2); grant/revoke/delete intentionally do
NOT invalidate.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.permission_errors import permission_denied_http_error
from okto_pulse.core.application.use_cases import (
    ConflictError,
    CreateAgentCommand,
    CreateAgentUseCase,
    DeleteAgentCommand,
    DeleteAgentUseCase,
    EntityNotFoundError,
    GetAgentCommand,
    GetAgentUseCase,
    GrantBoardAccessCommand,
    GrantBoardAccessUseCase,
    ListAgentsForBoardCommand,
    ListAgentsForBoardUseCase,
    ListAgentsForUserCommand,
    ListAgentsForUserUseCase,
    PermissionDeniedError,
    RegenerateAgentKeyCommand,
    RegenerateAgentKeyUseCase,
    RevokeBoardAccessCommand,
    RevokeBoardAccessUseCase,
    UpdateAgentCommand,
    UpdateAgentUseCase,
    UpdateBoardOverridesCommand,
    UpdateBoardOverridesUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.models import (
    AgentBoardOverridesUpdate,
    AgentBoardResponse,
    AgentCreate,
    AgentRevealResponse,
    AgentResponse,
    AgentSummary,
    AgentUpdate,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


async def _execute_authorized(use_case, command, *, actor, uow):
    """Project the shared Core permission outcome at the REST boundary."""
    try:
        return await use_case.execute(command, actor=actor, uow=uow)
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc


# ---------------------------------------------------------------------------
# Global agent CRUD (ownership via created_by)
# ---------------------------------------------------------------------------


@router.post("", response_model=AgentRevealResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new global agent and reveal its credential once."""
    result = await _execute_authorized(
        CreateAgentUseCase(),
        CreateAgentCommand(data),
        actor=RESTAdapterContract.actor_from_principal(principal),
        uow=uow,
    )
    return AgentRevealResponse(
        agent=AgentResponse.model_validate(result.agent),
        reveal_once_secret=result.reveal_once_secret,
    )


@router.get("", response_model=list[AgentResponse])
async def list_my_agents(
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all agents owned by the current user without credentials."""
    result = await _execute_authorized(
        ListAgentsForUserUseCase(),
        ListAgentsForUserCommand(),
        actor=RESTAdapterContract.actor_from_principal(principal),
        uow=uow,
    )
    return result.agents


@router.get("/board/{board_id}", response_model=list[AgentSummary])
async def list_agents_for_board(
    board_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all agents with access to a board."""
    try:
        result = await _execute_authorized(
            ListAgentsForBoardUseCase(),
            ListAgentsForBoardCommand(board_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal,
                board_id=board_id,
            ),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.agents


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get an agent by ID (owner only)."""
    try:
        result = await _execute_authorized(
            GetAgentUseCase(),
            GetAgentCommand(agent_id),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return result.agent


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    data: AgentUpdate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update an agent (owner only).

    Spec R01A IMP4: routes through the transport-free use case via
    ``get_unit_of_work`` (no raw ``AsyncSession``). The MCP permission-cache
    invalidation stays here in the REST adapter, after a successful update — the
    proven invalidation point (ac_8e695cf2) is preserved exactly.
    """
    try:
        result = await _execute_authorized(
            UpdateAgentUseCase(),
            UpdateAgentCommand(agent_id, data),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    from okto_pulse.core.mcp import invalidate_agent_cache
    invalidate_agent_cache(agent_id)

    return result.agent


@router.post("/{agent_id}/regenerate-key", response_model=AgentRevealResponse)
async def regenerate_agent_key(
    agent_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Regenerate an agent's API key and reveal the new credential once."""
    try:
        result = await _execute_authorized(
            RegenerateAgentKeyUseCase(),
            RegenerateAgentKeyCommand(agent_id),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    return AgentRevealResponse(
        agent=AgentResponse.model_validate(result.agent),
        reveal_once_secret=result.reveal_once_secret,
        message="API key regenerated",
    )


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete an agent (owner only). No cache invalidation (not a proven point)."""
    try:
        await _execute_authorized(
            DeleteAgentUseCase(),
            DeleteAgentCommand(agent_id),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")


# ---------------------------------------------------------------------------
# Board access grant / revoke
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/boards/{board_id}",
    response_model=AgentBoardResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_board_access(
    agent_id: str,
    board_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Grant an agent access to a board. Requires owning both the agent and the board.

    No cache invalidation (not a proven invalidation point — ac_8e695cf2)."""
    try:
        result = await _execute_authorized(
            GrantBoardAccessUseCase(),
            GrantBoardAccessCommand(agent_id, board_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal,
                board_id=board_id,
            ),
            uow=uow,
        )
    except ConflictError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Access already granted")
    except EntityNotFoundError as exc:
        detail = "Agent not found" if exc.entity_type == "agent" else "Board not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return result.grant


@router.patch("/{agent_id}/boards/{board_id}", response_model=AgentBoardResponse)
async def update_board_overrides(
    agent_id: str,
    board_id: str,
    data: AgentBoardOverridesUpdate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update permission overrides for an agent on a board (ceiling model).

    Spec R01A IMP4: routes through the transport-free use case via
    ``get_unit_of_work`` (no raw ``AsyncSession``). The MCP permission-cache
    invalidation stays here in the REST adapter, after a successful update — the
    proven invalidation point (ac_8e695cf2) is preserved exactly.
    """
    try:
        result = await _execute_authorized(
            UpdateBoardOverridesUseCase(),
            UpdateBoardOverridesCommand(agent_id, board_id, data.permission_overrides),
            actor=RESTAdapterContract.actor_from_principal(
                principal,
                board_id=board_id,
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        if exc.entity_type == "agent":
            detail = "Agent not found"
        elif exc.entity_type == "board":
            detail = "Board not found"
        else:
            detail = "Board access not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)

    from okto_pulse.core.mcp import invalidate_agent_cache
    invalidate_agent_cache(agent_id)

    return result.agent_board


@router.delete("/{agent_id}/boards/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_board_access(
    agent_id: str,
    board_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Revoke access only when the actor owns both the agent and the board.

    No cache invalidation (not a proven invalidation point — ac_8e695cf2)."""
    try:
        await _execute_authorized(
            RevokeBoardAccessUseCase(),
            RevokeBoardAccessCommand(agent_id, board_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal,
                board_id=board_id,
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        if exc.entity_type == "agent":
            detail = "Agent not found"
        elif exc.entity_type == "board":
            detail = "Board not found"
        else:
            detail = "Access not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
