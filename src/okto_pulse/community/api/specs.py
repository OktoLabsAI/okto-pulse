"""Spec API endpoints."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, ValidationError

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.knowledge_governance import (
    KnowledgeGovernanceInvalidMetadata,
    knowledge_governance_error_response,
)
from okto_pulse.community.api.knowledge_propagation import (
    KnowledgePropagationServiceError,
    knowledge_propagation_error_response,
    rollback_and_record_knowledge_error,
)
from okto_pulse.community.api.lookups import (
    lookup_page_request,
    lookup_response,
    validate_spec_lookup_query,
)
from okto_pulse.community.api.pagination import (
    board_scope,
    pagination_requested,
    project_page,
    record_fields,
    resolve_window,
    run_paginated_list,
    search_groups,
    validate_pagination_query,
)
from okto_pulse.community.api.quality_summary_projection import (
    load_quality_summaries_for_page,
    quality_summary_field,
)
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PageRequest,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgePropagationPortError,
)
from okto_pulse.core.application.use_cases import (
    AnswerSpecQuestionCommand,
    AnswerSpecQuestionUseCase,
    CommandValidationError,
    CreateSpecCommand,
    CreateSpecKnowledgeCommand,
    CreateSpecKnowledgeUseCase,
    CreateSpecQuestionCommand,
    CreateSpecQuestionUseCase,
    CreateSpecUseCase,
    DeleteSpecCommand,
    DeleteSpecKnowledgeCommand,
    DeleteSpecKnowledgeUseCase,
    DeleteSpecQuestionCommand,
    DeleteSpecQuestionUseCase,
    DeleteSpecUseCase,
    EntityNotFoundError,
    ExecuteTestScenarioEvidenceCommand,
    ExecuteTestScenarioEvidenceUseCase,
    GetSpecCommand,
    GetSpecKnowledgeCommand,
    GetSpecKnowledgeUseCase,
    GetSpecUseCase,
    LinkCardToSpecCommand,
    LinkCardToSpecUseCase,
    LinkTaskToIntegrationRequirementCommand,
    LinkTaskToIntegrationRequirementUseCase,
    LinkTaskToObservabilityRequirementCommand,
    LinkTaskToObservabilityRequirementUseCase,
    LinkTaskToScenarioCommand,
    LinkTaskToScenarioUseCase,
    ListSpecEvaluationsCommand,
    ListSpecEvaluationsUseCase,
    ListSpecHistoryCommand,
    ListSpecHistoryUseCase,
    ListSpecKnowledgeCommand,
    ListSpecKnowledgeUseCase,
    ListSpecQACommand,
    ListSpecQAUseCase,
    ListSpecsCommand,
    ListSpecsUseCase,
    MoveSpecCommand,
    MoveSpecUseCase,
    PermissionDeniedError,
    SetTestScenarioStatusCommand,
    SetTestScenarioStatusUseCase,
    SubmitSpecEvaluationCommand,
    SubmitSpecEvaluationUseCase,
    SubmitSpecValidationCommand,
    ListSpecValidationsCommand,
    ListSpecValidationsUseCase,
    RunStructuredSpecEntityCommand,
    RunStructuredSpecEntityUseCase,
    SubmitSpecValidationUseCase,
    UnlinkCardFromSpecCommand,
    UnlinkCardFromSpecUseCase,
    UnlinkTaskFromScenarioCommand,
    UnlinkTaskFromScenarioUseCase,
    UpdateSpecCommand,
    UpdateSpecUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.adapters.test_evidence import (
    normalize_test_scenario_evidence,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.models.schemas import (
    PageEnvelope,
    LookupResponse,
    SpecCreate,
    SpecKnowledgeCreate,
    SpecKnowledgeResponse,
    SpecKnowledgeSummary,
    SpecMove,
    SpecPageItem,
    SpecResponse,
    SpecSummary,
    SpecUpdate,
    TestScenarioEvidence,
)
from okto_pulse.core.models.schemas import SpecHistoryResponse, SpecQAAnswer, SpecQACreate, SpecQAResponse
from okto_pulse.core.application.errors import (
    CancellationReasonRequiredError,
    CardOperationError,
    QASelectionError,
    QASelfAnsweringNotAllowedError,
    ResourceGateError,
    ResourceLineageResolutionError,
    SpecLineagePreflightError,
    SprintOperationError,
)
from okto_pulse.core.services.gate_contracts import (
    GateContractError,
)
from okto_pulse.core.services.spec_structured_entities import (
    StructuredSpecEntityErrorCode,
)
from okto_pulse.core.services.test_scenario_lifecycle import StatusNotMutableError
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX

_SPEC_WRITE_BODY_MODEL = "__okto_pulse_spec_write_body_model__"


def _validate_spec_write_before_dependencies(
    model: type[BaseModel],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a route body for validation before FastAPI resolves dependencies."""

    def decorator(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        setattr(endpoint, _SPEC_WRITE_BODY_MODEL, model)
        return endpoint

    return decorator


class _PrevalidatedSpecWriteRoute(APIRoute):
    """Fail malformed spec writes before auth or UoW dependencies are entered.

    FastAPI normally accumulates request-body errors while continuing to resolve
    sibling dependencies. A generator dependency can therefore open a
    transaction for a request that will ultimately return 422. Marked write
    routes validate the same Pydantic request model at the outer route boundary;
    the regular handler still performs FastAPI's canonical validation on valid
    input.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        route_handler = super().get_route_handler()
        model = getattr(self.endpoint, _SPEC_WRITE_BODY_MODEL, None)
        if model is None:
            return route_handler

        async def prevalidated_route_handler(request: Request) -> Response:
            raw_body = await request.body()
            try:
                model.model_validate_json(raw_body)
            except ValidationError as exc:
                from okto_pulse.core.inbound.enum_error_envelope import (
                    canonical_scenario_type_error,
                )

                scenario_type_error = canonical_scenario_type_error(exc.errors())
                if scenario_type_error is not None:
                    return JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        content=scenario_type_error,
                    )
                errors = [
                    {**error, "loc": ("body", *error["loc"])}
                    for error in exc.errors()
                ]
                raise RequestValidationError(errors, body=raw_body) from exc
            return await route_handler(request)

        return prevalidated_route_handler


router = APIRouter(route_class=_PrevalidatedSpecWriteRoute)


async def _spec_knowledge_error_response(
    uow: PulseUnitOfWork,
    error: KnowledgePropagationPortError | KnowledgePropagationServiceError,
):
    if isinstance(error, KnowledgePropagationServiceError):
        await rollback_and_record_knowledge_error(uow, error)
    else:
        await uow.rollback()
    return knowledge_propagation_error_response(error)


class ScenarioStatusUpdate(BaseModel):
    """Request body for the scoped test-scenario status endpoint."""

    status: str
    evidence: TestScenarioEvidence | None = None


class ScenarioEvidenceExecutionRequest(BaseModel):
    """Run a server-owned replay; this endpoint does not mutate the scenario."""

    status: Literal["automated", "passed", "failed"]
    manifest_ref: str = Field(..., min_length=1)

STRUCTURED_SPEC_ENTITY_DEPRECATION_WARNING = (
    "Spec child entity edits should use /api/v1/specs/{spec_id}/structured-entities/"
    "{entity_type}; legacy whole-spec child list updates are compatibility-only."
)

_STRUCTURED_SPEC_ENTITY_UPDATE_FIELDS = {
    "functional_requirements",
    "acceptance_criteria",
    "technical_requirements",
    "business_rules",
    "api_contracts",
    "integration_requirements",
    "observability_requirements",
    "decisions",
}


def _prepare_spec_update_evidence(data: SpecUpdate) -> SpecUpdate:
    """Normalize/verify canonical V2 evidence on the whole-spec REST path."""

    fields_set = set(getattr(data, "model_fields_set", set()))
    if "test_scenarios" not in fields_set or data.test_scenarios is None:
        return data
    payload = data.model_dump(mode="python", exclude_unset=True)
    scenarios: list[dict[str, Any]] = []
    for raw_scenario in payload.get("test_scenarios") or []:
        scenario = dict(raw_scenario)
        raw_evidence = scenario.get("evidence") or scenario.get("latest_evidence")
        if raw_evidence is not None:
            evidence = normalize_test_scenario_evidence(
                raw_evidence,
                scenario_id=str(scenario.get("id") or ""),
                status=str(scenario.get("status") or "draft"),
            )
            target = "evidence" if scenario.get("evidence") is not None else "latest_evidence"
            scenario[target] = evidence
        scenarios.append(scenario)
    payload["test_scenarios"] = scenarios
    return SpecUpdate.model_validate(payload)


class StructuredSpecEntityMutationRequest(BaseModel):
    operation: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_spec_version: int | None = None
    task_id: str | None = None
    ack_token: str | None = None


def _structured_entity_status_code(error_code: str | None) -> int:
    return {
        StructuredSpecEntityErrorCode.AUTHORIZATION_DENIED: status.HTTP_403_FORBIDDEN,
        StructuredSpecEntityErrorCode.VERSION_CONFLICT: status.HTTP_409_CONFLICT,
        StructuredSpecEntityErrorCode.IMPACT_ACK_REQUIRED: status.HTTP_409_CONFLICT,
        StructuredSpecEntityErrorCode.IMPACT_ACK_INVALID: status.HTTP_409_CONFLICT,
        StructuredSpecEntityErrorCode.ENTITY_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        StructuredSpecEntityErrorCode.SPEC_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        StructuredSpecEntityErrorCode.SPEC_LOCKED: status.HTTP_409_CONFLICT,
        StructuredSpecEntityErrorCode.UNSUPPORTED_ENTITY_TYPE: status.HTTP_422_UNPROCESSABLE_CONTENT,
        StructuredSpecEntityErrorCode.UNSUPPORTED_OPERATION: status.HTTP_422_UNPROCESSABLE_CONTENT,
        StructuredSpecEntityErrorCode.VALIDATION_FAILED: status.HTTP_422_UNPROCESSABLE_CONTENT,
        StructuredSpecEntityErrorCode.LINK_TARGET_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
    }.get(error_code, status.HTTP_400_BAD_REQUEST)


def _resource_gate_detail(exc: ResourceGateError) -> dict:
    return {
        "error": exc.code,
        "message": str(exc),
        "details": exc.details,
    }


def _link_task_not_found_detail(exc: EntityNotFoundError) -> str:
    """Map the typed ``EntityNotFoundError`` from ``LinkTaskToScenarioUseCase``
    back to the exact legacy 404 detail per missing entity (spec / card /
    scenario)."""
    if exc.entity_type == "spec":
        return "Spec not found"
    if exc.entity_type == "card":
        return f"Card '{exc.entity_id}' not found — cannot link a non-existent card."
    return f"Scenario '{exc.entity_id}' not found in spec."


def _link_ir_not_found_detail(exc: EntityNotFoundError) -> str:
    """Map the typed ``EntityNotFoundError`` from
    ``LinkTaskToIntegrationRequirementUseCase`` back to the exact legacy 404
    detail per missing entity (spec / card / integration requirement)."""
    if exc.entity_type == "spec":
        return "Spec not found"
    if exc.entity_type == "card":
        return f"Card '{exc.entity_id}' not found — cannot link a non-existent card."
    return f"Integration requirement '{exc.entity_id}' not found in spec."


def _link_or_not_found_detail(exc: EntityNotFoundError) -> str:
    """Map the typed ``EntityNotFoundError`` from
    ``LinkTaskToObservabilityRequirementUseCase`` back to the exact legacy 404
    detail per missing entity (spec / card / observability requirement)."""
    if exc.entity_type == "spec":
        return "Spec not found"
    if exc.entity_type == "card":
        return f"Card '{exc.entity_id}' not found — cannot link a non-existent card."
    return f"Observability requirement '{exc.entity_id}' not found in spec."


async def _run_structured_spec_entity_command(
    *,
    uow: PulseUnitOfWork,
    user_id: str,
    spec_id: str,
    entity_type: str,
    operation: str,
    payload: dict[str, Any] | None = None,
    entity_id: str | None = None,
    expected_spec_version: int | None = None,
    task_id: str | None = None,
    ack_token: str | None = None,
    preview_only: bool = False,
) -> dict[str, Any]:
    """Spec R01A REST-FU3b-S1: thin REST mapping over
    ``RunStructuredSpecEntityUseCase``. The transport-free logic (spec lookup,
    permission resolution, ``StructuredSpecEntityService.apply``, commit/rollback)
    lives in the use case; this adapter only maps the result to HTTP — a missing
    spec to 404 and a service failure to the legacy ``error_code`` status."""
    try:
        result = await RunStructuredSpecEntityUseCase().execute(
            RunStructuredSpecEntityCommand(
                spec_id,
                entity_type,
                operation,
                payload=payload,
                entity_id=entity_id,
                expected_spec_version=expected_spec_version,
                task_id=task_id,
                ack_token=ack_token,
                preview_only=preview_only,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    structured = result.structured_result
    body = structured.as_dict()
    if not structured.success:
        raise HTTPException(
            status_code=_structured_entity_status_code(structured.error_code),
            detail=body,
        )
    return body


@router.post(
    "/boards/{board_id}/specs",
    response_model=SpecResponse,
    status_code=status.HTTP_201_CREATED,
)
@_validate_spec_write_before_dependencies(SpecCreate)
async def create_spec(
    board_id: str,
    data: SpecCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new spec in a board."""
    try:
        result = await CreateSpecUseCase().execute(
            CreateSpecCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found or not owned by user",
        )
    except (
        ResourceLineageResolutionError,
        SpecLineagePreflightError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.to_error_dict(),
        ) from exc
    return result.spec


@router.get(
    "/boards/{board_id}/specs",
    response_model=list[SpecSummary] | PageEnvelope[SpecPageItem],
    dependencies=[Depends(validate_pagination_query)],
)
async def list_specs(
    board_id: str,
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    include_archived: bool = Query(False, alias="include_archived"),
    offset: int | None = Query(None),
    limit: int | None = Query(None),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List specs for a board, optionally filtered by status.

    With ``offset``/``limit``: paginated envelope (spec 8b33f9a8); without:
    legacy shape unchanged (DR9).
    """
    if pagination_requested(offset, limit):
        command = ListSpecsCommand(
            board_id,
            status_filter=status_filter,
            include_archived=include_archived,
        )
        actor = RESTAdapterContract.actor(user_id)
        use_case = ListSpecsUseCase()
        try:
            resolved_offset, resolved_limit = resolve_window(offset, limit)
            filters: tuple[ApplicationFilter, ...] = ()
            if status_filter:
                filters = (ApplicationFilter("status", "eq", status_filter),)
            page = await run_paginated_list(
                uow,
                PageRequest(
                    surface="spec_list",
                    scope=board_scope(board_id, include_archived=include_archived),
                    offset=resolved_offset,
                    limit=resolved_limit,
                    filters=filters,
                    any_groups=search_groups(search, ("title", "description")),
                ),
                preflight=lambda: use_case.preflight(
                    command, actor=actor, uow=uow
                ),
            )
        except EntityNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Board not found"
            )
        quality_summaries = await load_quality_summaries_for_page(
            uow=uow,
            user_id=user_id,
            board_id=board_id,
            subject_type="spec",
            subject_ids=tuple(
                str(record.values["id"]) for record in page.items
            ),
        )
        return project_page(
            page,
            lambda record: SpecPageItem(
                **record_fields(
                    record,
                    (
                        "id",
                        "board_id",
                        "ideation_id",
                        "refinement_id",
                        "title",
                        "description",
                        "status",
                        "version",
                        "assignee_id",
                        "created_by",
                        "created_at",
                        "updated_at",
                        "labels",
                        "archived",
                    ),
                ),
                **quality_summary_field(
                    str(record.values["id"]),
                    quality_summaries,
                ),
            ),
        )
    try:
        result = await ListSpecsUseCase().execute(
            ListSpecsCommand(
                board_id, status_filter=status_filter, include_archived=include_archived
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.specs


@router.get(
    "/boards/{board_id}/specs/lookup",
    response_model=LookupResponse,
    dependencies=[Depends(validate_spec_lookup_query)],
)
async def lookup_specs(
    board_id: str,
    search: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(20),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    linked_to_cards: bool = Query(False),
    include_archived_cards: bool = Query(False),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return a compact, purpose-filtered spec typeahead page."""

    command = ListSpecsCommand(
        board_id,
        status_filter=None,
        include_archived=False,
    )
    actor = RESTAdapterContract.actor(user_id)
    try:
        page = await run_paginated_list(
            uow,
            lookup_page_request(
                "spec_lookup",
                board_id,
                statuses=status_filter,
                search=search,
                offset=offset,
                limit=limit,
                linked_to_cards=linked_to_cards,
                include_archived_cards=include_archived_cards,
            ),
            preflight=lambda: ListSpecsUseCase().preflight(
                command,
                actor=actor,
                uow=uow,
            ),
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        )
    return lookup_response(page)


@router.get("/specs/{spec_id}", response_model=SpecResponse)
async def get_spec(
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a spec by ID with its derived cards."""
    try:
        result = await GetSpecUseCase().execute(
            GetSpecCommand(spec_id), actor=RESTAdapterContract.actor(user_id), uow=uow
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _spec_knowledge_error_response(uow, exc)
    return result.spec


@router.patch("/specs/{spec_id}", response_model=SpecResponse)
@_validate_spec_write_before_dependencies(SpecUpdate)
async def update_spec(
    spec_id: str,
    data: SpecUpdate,
    response: Response,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a spec. Bumps version when content fields change.
    Rejects orphan `linked_*` references with 422 — see
    `_validate_spec_linked_refs` in services/main.py for the exact rules.
    """
    fields_set = set(
        getattr(data, "model_fields_set", None)
        or getattr(data, "__fields_set__", set())
    )
    if fields_set & _STRUCTURED_SPEC_ENTITY_UPDATE_FIELDS:
        response.headers["X-Okto-Pulse-Deprecation"] = (
            STRUCTURED_SPEC_ENTITY_DEPRECATION_WARNING
        )
    try:
        prepared_data = _prepare_spec_update_evidence(data)
        result = await UpdateSpecUseCase().execute(
            UpdateSpecCommand(spec_id, prepared_data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message)
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    except SpecLineagePreflightError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.to_error_dict(),
        ) from exc
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e),
        )
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _spec_knowledge_error_response(uow, exc)
    return result.spec


@router.post("/specs/{spec_id}/structured-entities/{entity_type}")
async def create_structured_spec_entity(
    spec_id: str,
    entity_type: str,
    data: StructuredSpecEntityMutationRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create one structured spec child entity through StructuredSpecEntityService."""
    return await _run_structured_spec_entity_command(
        uow=uow,
        user_id=user_id,
        spec_id=spec_id,
        entity_type=entity_type,
        operation="create",
        payload=data.payload,
        expected_spec_version=data.expected_spec_version,
        task_id=data.task_id,
        ack_token=data.ack_token,
    )


@router.patch("/specs/{spec_id}/structured-entities/{entity_type}/{entity_id}")
async def update_structured_spec_entity(
    spec_id: str,
    entity_type: str,
    entity_id: str,
    data: StructuredSpecEntityMutationRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update one structured spec child entity."""
    return await _run_structured_spec_entity_command(
        uow=uow,
        user_id=user_id,
        spec_id=spec_id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=data.operation or "update",
        payload=data.payload,
        expected_spec_version=data.expected_spec_version,
        task_id=data.task_id,
        ack_token=data.ack_token,
    )


@router.post("/specs/{spec_id}/structured-entities/{entity_type}/{entity_id}")
async def operate_structured_spec_entity(
    spec_id: str,
    entity_type: str,
    entity_id: str,
    data: StructuredSpecEntityMutationRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Run a structured operation such as revoke, supersede, restore, reorder, link_task or unlink_task."""
    if not data.operation:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="operation is required.",
        )
    return await _run_structured_spec_entity_command(
        uow=uow,
        user_id=user_id,
        spec_id=spec_id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=data.operation,
        payload=data.payload,
        expected_spec_version=data.expected_spec_version,
        task_id=data.task_id,
        ack_token=data.ack_token,
    )


@router.post("/specs/{spec_id}/structured-entities/{entity_type}/{entity_id}/impact-preview")
async def preview_structured_spec_entity_impact(
    spec_id: str,
    entity_type: str,
    entity_id: str,
    data: StructuredSpecEntityMutationRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Preview impact for destructive-like structured operations without mutating."""
    if not data.operation:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="operation is required.",
        )
    return await _run_structured_spec_entity_command(
        uow=uow,
        user_id=user_id,
        spec_id=spec_id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation=data.operation,
        payload=data.payload,
        expected_spec_version=data.expected_spec_version,
        task_id=data.task_id,
        ack_token=data.ack_token,
        preview_only=True,
    )


@router.post("/specs/{spec_id}/move", response_model=SpecResponse)
async def move_spec(
    spec_id: str,
    data: SpecMove,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Change spec status."""
    try:
        result = await MoveSpecUseCase().execute(
            MoveSpecCommand(spec_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except GateContractError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ResourceGateError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_resource_gate_detail(e),
        )
    except CancellationReasonRequiredError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.to_dict())
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _spec_knowledge_error_response(uow, exc)
    return result.spec


@router.delete("/specs/{spec_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_spec(
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a spec. Unlinks derived cards but doesn't delete them."""
    try:
        await DeleteSpecUseCase().execute(
            DeleteSpecCommand(spec_id), actor=RESTAdapterContract.actor(user_id), uow=uow
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")


@router.get("/specs/{spec_id}/history", response_model=list[SpecHistoryResponse])
async def list_spec_history(
    spec_id: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get detailed change history for a spec."""
    try:
        result = await ListSpecHistoryUseCase().execute(
            ListSpecHistoryCommand(spec_id, limit=limit),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    return result.history


@router.post("/specs/{spec_id}/link-card/{card_id}", status_code=status.HTTP_200_OK)
async def link_card_to_spec(
    spec_id: str,
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Link an existing card to a spec."""
    try:
        await LinkCardToSpecUseCase().execute(
            LinkCardToSpecCommand(spec_id, card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Spec or card not found, or they belong to different boards",
        )
    return {"success": True, "spec_id": spec_id, "card_id": card_id}


@router.post("/specs/{spec_id}/unlink-card/{card_id}", status_code=status.HTTP_200_OK)
async def unlink_card_from_spec(
    spec_id: str,
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Unlink a card from a spec."""
    try:
        await UnlinkCardFromSpecUseCase().execute(
            UnlinkCardFromSpecCommand(spec_id, card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found or not linked to any spec",
        )
    return {"success": True, "spec_id": spec_id, "card_id": card_id}


# ==================== LINK TASK TO SCENARIO ====================


@router.post("/specs/{spec_id}/scenarios/{scenario_id}/link-task/{card_id}", status_code=status.HTTP_200_OK)
async def link_task_to_scenario(
    spec_id: str,
    scenario_id: str,
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Link a task card to a test scenario (bidirectional).
    Validates upfront that both scenario and card exist before mutating
    either side, so a typo in card_id no longer leaves an orphan link.
    """
    try:
        await LinkTaskToScenarioUseCase().execute(
            LinkTaskToScenarioCommand(spec_id, scenario_id, card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_link_task_not_found_detail(exc),
        )
    except CardOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.to_dict(),
        ) from exc
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e),
        )
    return {"success": True, "spec_id": spec_id, "scenario_id": scenario_id, "card_id": card_id}


@router.post("/specs/{spec_id}/scenarios/{scenario_id}/unlink-task/{card_id}", status_code=status.HTTP_200_OK)
async def unlink_task_from_scenario(
    spec_id: str,
    scenario_id: str,
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Unlink a task card from a test scenario (bidirectional)."""
    try:
        await UnlinkTaskFromScenarioUseCase().execute(
            UnlinkTaskFromScenarioCommand(spec_id, scenario_id, card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        detail = "Spec not found" if exc.entity_type == "spec" else "Scenario not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return {"success": True, "spec_id": spec_id, "scenario_id": scenario_id, "card_id": card_id}


@router.post(
    "/specs/{spec_id}/scenarios/{scenario_id}/evidence/execute",
    status_code=status.HTTP_200_OK,
)
async def execute_test_scenario_evidence(
    spec_id: str,
    scenario_id: str,
    body: ScenarioEvidenceExecutionRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Execute an allowlisted local replay and return signed V2 evidence."""

    try:
        result = await ExecuteTestScenarioEvidenceUseCase().execute(
            ExecuteTestScenarioEvidenceCommand(
                spec_id,
                scenario_id,
                body.status,
                body.manifest_ref,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        detail = "Spec not found" if exc.entity_type == "spec" else "Scenario not found"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return {"evidence": result.evidence}


@router.patch(
    "/specs/{spec_id}/scenarios/{scenario_id}/status",
    status_code=status.HTTP_200_OK,
)
async def update_test_scenario_status(
    spec_id: str,
    scenario_id: str,
    body: ScenarioStatusUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Scoped status mutation for a single test scenario (spec 6f1e75bf, FR6).

    Applies the same leaf helpers as the MCP status tool
    (require_test_scenario_status_mutable + validate_test_scenario_evidence via
    SpecService.set_test_scenario_status) and mutates ONLY the target scenario —
    it does NOT use the full-list update_spec path and does NOT trigger the
    content-lock, so the other scenarios are preserved. Rejects gated status
    without evidence (422) and arbitrary status changes on validated/done specs
    (409). Post-lock regression evidence remains allowed when the target
    scenario is already linked to an executable test card; this is operational
    evidence, not a semantic spec edit.
    """
    try:
        evidence = (
            normalize_test_scenario_evidence(
                body.evidence.model_dump(mode="python", exclude_none=True),
                scenario_id=scenario_id,
                status=body.status,
            )
            if body.evidence is not None
            else None
        )
        outcome = await SetTestScenarioStatusUseCase().execute(
            SetTestScenarioStatusCommand(spec_id, scenario_id, body.status, evidence),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except StatusNotMutableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith("scenario_not_found"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=msg
        )
    result = outcome.result
    return {
        "id": spec_id,
        "scenario": {"id": result["scenario_id"], "status": result["new_status"]},
        "result": result,
    }


@router.post(
    "/specs/{spec_id}/integration-requirements/{requirement_id}/link-task/{card_id}",
    status_code=status.HTTP_200_OK,
)
async def link_task_to_integration_requirement(
    spec_id: str,
    requirement_id: str,
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Link a task card to an integration requirement."""
    try:
        await LinkTaskToIntegrationRequirementUseCase().execute(
            LinkTaskToIntegrationRequirementCommand(spec_id, requirement_id, card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_link_ir_not_found_detail(exc),
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e),
        )
    return {
        "success": True,
        "spec_id": spec_id,
        "requirement_id": requirement_id,
        "card_id": card_id,
    }


@router.post(
    "/specs/{spec_id}/observability-requirements/{requirement_id}/link-task/{card_id}",
    status_code=status.HTTP_200_OK,
)
async def link_task_to_observability_requirement(
    spec_id: str,
    requirement_id: str,
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Link a task card to an observability requirement."""
    try:
        await LinkTaskToObservabilityRequirementUseCase().execute(
            LinkTaskToObservabilityRequirementCommand(spec_id, requirement_id, card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_link_or_not_found_detail(exc),
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e),
        )
    return {
        "success": True,
        "spec_id": spec_id,
        "requirement_id": requirement_id,
        "card_id": card_id,
    }


# ==================== SPEC KNOWLEDGE BASE ====================


@router.get("/specs/{spec_id}/knowledge", response_model=list[SpecKnowledgeSummary])
async def list_spec_knowledge(
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all knowledge base items for a spec (without content)."""
    try:
        result = await ListSpecKnowledgeUseCase().execute(
            ListSpecKnowledgeCommand(spec_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _spec_knowledge_error_response(uow, exc)
    return result.items


@router.get("/specs/{spec_id}/knowledge/{knowledge_id}", response_model=SpecKnowledgeResponse)
async def get_spec_knowledge(
    spec_id: str,
    knowledge_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a knowledge base item with full content."""
    try:
        result = await GetSpecKnowledgeUseCase().execute(
            GetSpecKnowledgeCommand(spec_id, knowledge_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base item not found")
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _spec_knowledge_error_response(uow, exc)
    return result.knowledge


@router.post("/specs/{spec_id}/knowledge", response_model=SpecKnowledgeResponse, status_code=status.HTTP_201_CREATED)
async def create_spec_knowledge(
    spec_id: str,
    data: SpecKnowledgeCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Add a knowledge base item to a spec."""
    try:
        result = await CreateSpecKnowledgeUseCase().execute(
            CreateSpecKnowledgeCommand(spec_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    except KnowledgeGovernanceInvalidMetadata as exc:
        return knowledge_governance_error_response(exc)
    return result.knowledge


@router.delete("/specs/{spec_id}/knowledge/{knowledge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_spec_knowledge(
    spec_id: str,
    knowledge_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a knowledge base item."""
    try:
        await DeleteSpecKnowledgeUseCase().execute(
            DeleteSpecKnowledgeCommand(spec_id, knowledge_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base item not found")


# ==================== SPEC Q&A ====================


@router.get("/specs/{spec_id}/qa", response_model=list[SpecQAResponse])
async def list_spec_qa(
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all Q&A items for a spec."""
    try:
        result = await ListSpecQAUseCase().execute(
            ListSpecQACommand(spec_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    return result.items


@router.post("/specs/{spec_id}/qa", response_model=SpecQAResponse, status_code=status.HTTP_201_CREATED)
async def create_spec_question(
    spec_id: str,
    data: SpecQACreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Ask a question on a spec."""
    try:
        result = await CreateSpecQuestionUseCase().execute(
            CreateSpecQuestionCommand(spec_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    return result.qa


@router.post("/specs/{spec_id}/qa/{qa_id}/answer", response_model=SpecQAResponse)
async def answer_spec_question(
    spec_id: str,
    qa_id: str,
    data: SpecQAAnswer,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Answer a spec Q&A question."""
    try:
        result = await AnswerSpecQuestionUseCase().execute(
            AnswerSpecQuestionCommand(qa_id, data, spec_id=spec_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except QASelfAnsweringNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": exc.reason, "message": str(exc)},
        ) from exc
    except QASelectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.to_error_dict(),
        ) from exc
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Q&A item not found")
    return result.qa


@router.delete("/specs/{spec_id}/qa/{qa_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_spec_question(
    spec_id: str,
    qa_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a spec Q&A item."""
    try:
        await DeleteSpecQuestionUseCase().execute(
            DeleteSpecQuestionCommand(qa_id, spec_id=spec_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Q&A item not found")


# ---- Spec Validation Gate Endpoints ----


@router.post("/specs/{spec_id}/validation", status_code=status.HTTP_201_CREATED)
async def submit_spec_validation(
    spec_id: str,
    data: dict,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Submit a Spec Validation Gate record for a spec in 'approved' status.

    Runs deterministic coverage gates as pre-requisite. If they pass, computes
    outcome from thresholds + recommendation: failed if any threshold violated
    or recommendation=reject; success only if all thresholds OK and approve.
    On success, atomically promotes spec.status to validated.
    """
    # Thin REST adapter (spec #09): the field-shape validation moved into the
    # command, get_spec/not-found and the coverage-gate errors are surfaced as
    # transport-neutral errors and mapped to the SAME HTTP status/detail as before
    # (CommandValidationError→400, EntityNotFoundError→404, ResourceGateError→409
    # with {error,message,details}, ValueError→409). Spec R01A REST-FU3b-S1 fixes
    # the hybrid wiring: the gate now flows through the PulseUnitOfWork instead of
    # a raw AsyncSession passed as the uow.
    try:
        result = await SubmitSpecValidationUseCase().execute(
            SubmitSpecValidationCommand(spec_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except GateContractError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=e.to_dict(),
        ) from e
    except (
        CommandValidationError,
        EntityNotFoundError,
        ResourceGateError,
        ValueError,
    ) as e:
        raise RESTAdapterContract.http_error(e, not_found_detail="Spec not found") from e
    return result.payload


@router.get("/specs/{spec_id}/validations")
async def list_spec_validations(
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all Spec Validation Gate records in reverse chronological order.

    Returns current_validation_id and the validations array with an 'active'
    flag on each record indicating if it's the currently-active pointer.
    """
    try:
        result = await ListSpecValidationsUseCase().execute(
            ListSpecValidationsCommand(spec_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {"spec_id": spec_id, **result.data}


class SpecEvaluationSubmit(BaseModel):
    """Avaliação qualitativa de uma spec validated — gêmeo REST do MCP tool
    ``okto_pulse_submit_spec_evaluation`` (paridade de superfícies)."""

    breakdown_completeness: int = Field(..., ge=0, le=100)
    breakdown_justification: str = Field(..., min_length=10)
    granularity: int = Field(..., ge=0, le=100)
    granularity_justification: str = Field(..., min_length=10)
    dependency_coherence: int = Field(..., ge=0, le=100)
    dependency_justification: str = Field(..., min_length=10)
    test_coverage_quality: int = Field(..., ge=0, le=100)
    test_coverage_justification: str = Field(..., min_length=10)
    overall_score: int = Field(..., ge=0, le=100)
    overall_justification: str = Field(..., min_length=10)
    recommendation: Literal["approve", "request_changes", "reject"]


@router.post("/specs/{spec_id}/evaluations", status_code=status.HTTP_201_CREATED)
async def submit_spec_evaluation(
    spec_id: str,
    data: SpecEvaluationSubmit,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Submit a qualitative evaluation for a spec in 'validated' status.

    Gap fechado (paridade REST/MCP): este gate é pré-requisito de
    ``move_spec(validated→in_progress)``, mas só existia como MCP tool —
    usuários UI/REST ficavam presos em ``validated`` sem caminho de escrita.
    Mesma semântica do tool: múltiplos avaliadores, append-only, spec
    precisa estar em 'validated'.
    """
    try:
        result = await SubmitSpecEvaluationUseCase().execute(
            SubmitSpecEvaluationCommand(spec_id, data.model_dump()),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec not found")
    except GateContractError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return result.payload


@router.get("/specs/{spec_id}/evaluations")
async def list_spec_evaluations(
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List spec evaluations (newest first) — gêmeo REST de
    ``okto_pulse_list_spec_evaluations``."""
    try:
        result = await ListSpecEvaluationsUseCase().execute(
            ListSpecEvaluationsCommand(spec_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return result.data
