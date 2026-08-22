"""End-to-end REST/MCP parity at the source-blind Code Traceability seam."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
import inspect
from types import SimpleNamespace
import textwrap
from typing import Any, get_args, get_type_hints

from fastapi import FastAPI
import httpx
from pydantic import BaseModel, ValidationError
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.sqlalchemy_code_traceability_event_effects import (
    CommunitySqlAlchemyCodeTraceabilityEventEffects,
)
from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog, Base
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.community.api import code_traceability as rest_api
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.code_traceability import (
    CodeInvestigationActorKindRequired,
    ContextualInvestigationOutcomeV2,
    CodeTraceabilityContextScope,
    CodeTraceabilityProjectionProfile,
    CodeTraceabilitySubjectType,
    DeliveryContext,
    code_investigation_observation_sha256_v2,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.mcp import code_traceability_tools as mcp_tools
from okto_pulse.core.mcp import server as mcp_server
from okto_pulse.core.mcp.outcome import McpOutcomeKind
from okto_pulse.core.ports.authentication import (
    AuthenticationPort,
    Credential,
    Principal,
)
from okto_pulse.core.ports.code_investigation import (
    CodeInvestigationRequestCreateResult,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
    reset_relational_application_adapter_for_tests,
)
from okto_pulse.core.services.code_investigation import (
    CodeInvestigationService,
    HmacCodeInvestigationChallengePolicy,
)
from test_code_traceability_persistence import _attestation_bundle


NOW = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)
AGENT_CREDENTIAL = Credential("parity-agent-key", source="test")
START_BODY = {
    "subject_type": "spec",
    "subject_id": "spec-1",
    "expected_subject_version": 3,
    "idempotency_key": "start-parity-1",
}


TRANSPORT_CAPABILITIES = (
    pytest.param(
        "okto_pulse_start_code_investigation",
        "start_code_investigation",
        "StartCodeInvestigationUseCase",
        frozenset({"board_id"}),
        frozenset(),
        id="start-investigation",
    ),
    pytest.param(
        "okto_pulse_submit_code_investigation_receipt",
        "submit_code_investigation_receipt",
        "SubmitCodeInvestigationReceiptUseCase",
        frozenset({"board_id", "request_id"}),
        frozenset(
            {
                "source_ref",
                "subject_id",
                "subject_version",
                "generation",
                "predecessor_receipt_id",
                "trust_level",
                "acceptance_status",
                "received_at",
            }
        ),
        id="submit-investigation-receipt",
    ),
    pytest.param(
        "okto_pulse_get_code_investigation_receipt",
        "get_code_investigation_receipt",
        "GetCodeInvestigationReceiptUseCase",
        frozenset({"board_id", "receipt_id"}),
        frozenset(),
        id="get-investigation-receipt",
    ),
    pytest.param(
        "okto_pulse_submit_code_evidence",
        "submit_code_evidence",
        "SubmitCodeEvidenceUseCase",
        frozenset({"board_id"}),
        frozenset(
            {
                "source_ref",
                "parent_version",
                "workspace_state",
                "excerpt_omitted_reason",
                "attestation_state",
                "attestation_basis",
                "lifecycle_status",
                "supersedes_evidence_id",
                "revocation_reason",
                "submitted_by",
                "received_at",
                "payload_sha256",
            }
        ),
        id="submit-evidence",
    ),
    pytest.param(
        "okto_pulse_get_code_evidence",
        "get_code_evidence",
        "GetCodeEvidenceUseCase",
        frozenset({"board_id", "evidence_id"}),
        frozenset(),
        id="get-evidence",
    ),
    pytest.param(
        "okto_pulse_list_code_evidence",
        "list_code_evidence",
        "ListCodeEvidenceUseCase",
        frozenset({"board_id"}),
        frozenset(),
        id="list-evidence",
    ),
    pytest.param(
        "okto_pulse_supersede_code_evidence",
        "supersede_code_evidence",
        "SupersedeCodeEvidenceUseCase",
        frozenset({"board_id", "evidence_id"}),
        frozenset(
            {
                "source_ref",
                "parent_version",
                "workspace_state",
                "excerpt_omitted_reason",
                "attestation_state",
                "attestation_basis",
                "lifecycle_status",
                "revocation_reason",
                "submitted_by",
                "received_at",
                "payload_sha256",
            }
        ),
        id="supersede-evidence",
    ),
    pytest.param(
        "okto_pulse_link_code_evidence",
        "link_code_evidence",
        "LinkCodeEvidenceToSpecUseCase",
        frozenset({"board_id", "spec_id"}),
        frozenset(),
        id="link-evidence",
    ),
    pytest.param(
        "okto_pulse_unlink_code_evidence",
        "unlink_code_evidence",
        "UnlinkCodeEvidenceFromSpecUseCase",
        frozenset({"board_id", "spec_id", "link_id"}),
        frozenset(),
        id="unlink-evidence",
    ),
    pytest.param(
        "okto_pulse_set_code_evidence_disposition",
        "set_code_evidence_disposition",
        "SetCodeEvidenceDispositionUseCase",
        frozenset({"board_id", "spec_id", "evidence_id"}),
        frozenset(),
        id="set-evidence-disposition",
    ),
    pytest.param(
        "okto_pulse_create_implementation_target",
        "create_implementation_target",
        "CreateImplementationTargetUseCase",
        frozenset({"board_id", "card_id"}),
        frozenset(
            {
                "revision",
                "current_resolution_id",
                "last_change_reason_sha256",
                "created_by",
                "created_at",
                "updated_at",
            }
        ),
        id="create-target",
    ),
    pytest.param(
        "okto_pulse_update_implementation_target",
        "update_implementation_target",
        "UpdateImplementationTargetUseCase",
        frozenset({"board_id", "card_id", "target_id"}),
        frozenset(
            {
                "current_resolution_id",
                "last_change_reason_sha256",
                "created_by",
                "created_at",
                "updated_at",
            }
        ),
        id="update-target",
    ),
    pytest.param(
        "okto_pulse_list_implementation_targets",
        "list_implementation_targets",
        "ListImplementationTargetsUseCase",
        frozenset({"board_id"}),
        frozenset(),
        id="list-targets",
    ),
    pytest.param(
        "okto_pulse_submit_implementation_target_resolution",
        "submit_implementation_target_resolution",
        "SubmitImplementationTargetResolutionUseCase",
        frozenset({"board_id", "card_id", "target_id"}),
        frozenset(
            {
                "source_ref",
                "receipt_generation",
                "subject_version",
                "target_revision",
                "workspace_state",
                "selector_fingerprint",
                "candidate_count",
                "submitted_by",
                "received_at",
                "payload_sha256",
            }
        ),
        id="submit-target-resolution",
    ),
    pytest.param(
        "okto_pulse_get_implementation_overlaps",
        "get_implementation_overlaps",
        "GetImplementationOverlapsUseCase",
        frozenset({"board_id", "card_id"}),
        frozenset(),
        id="get-overlaps",
    ),
    pytest.param(
        "okto_pulse_acknowledge_implementation_overlap",
        "acknowledge_implementation_overlap",
        "AcknowledgeImplementationOverlapUseCase",
        frozenset({"board_id", "card_id"}),
        frozenset({"created_by", "created_at"}),
        id="acknowledge-overlap",
    ),
    pytest.param(
        "okto_pulse_submit_implementation_target_execution_receipt",
        "submit_implementation_target_execution",
        "SubmitImplementationTargetExecutionUseCase",
        frozenset({"board_id", "card_id", "target_id"}),
        frozenset(
            {
                "target_revision",
                "source_ref",
                "result_declared_revision",
                "result_workspace_state_id",
                "submitted_by",
                "received_at",
                "payload_sha256",
            }
        ),
        id="submit-target-execution",
    ),
    pytest.param(
        "okto_pulse_mark_code_traceability_not_applicable",
        "mark_code_traceability_not_applicable",
        "MarkCodeTraceabilityNotApplicableUseCase",
        frozenset({"board_id"}),
        frozenset({"active", "created_by", "created_at", "cleared_by", "cleared_at"}),
        id="mark-waiver",
    ),
    pytest.param(
        "okto_pulse_clear_code_traceability_not_applicable",
        "clear_code_traceability_not_applicable",
        "ClearCodeTraceabilityNotApplicableUseCase",
        frozenset({"board_id", "waiver_id"}),
        frozenset({"cleared_by", "cleared_at"}),
        id="clear-waiver",
    ),
)

COMMON_SERVER_OWNED_FIELDS = frozenset(
    {
        "actor_id",
        "actor_kind",
        "attestor_actor_id",
        "generation",
        "predecessor_receipt_id",
        "trust_level",
        "acceptance_status",
        "received_at",
        "payload_sha256",
        "current_resolution_id",
        "receipt_generation",
    }
)


class StableIds:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        count = self._counts.get(prefix, 0) + 1
        self._counts[prefix] = count
        return f"{prefix}-{count}"


class InjectedAgentAuthentication:
    """Edition-injected identity; it does not acquire or inspect source."""

    def __init__(self) -> None:
        self.calls: list[Credential | None] = []

    async def authenticate(self, credential: Credential | None) -> Principal | None:
        self.calls.append(credential)
        if credential != AGENT_CREDENTIAL:
            return None
        return Principal(
            subject="agent-1",
            realm_id=LOCAL_REALM_ID,
            actor_kind="agent",
            claims={
                "name": "Parity Agent",
                "permissions": ("code_traceability.investigation.start",),
            },
        )


class InvestigationStore:
    def __init__(self) -> None:
        self.requests: dict[str, object] = {}

    async def resolve_request_replay(self, **_kwargs: object) -> None:
        return None

    async def get_current_head(self, **_kwargs: object) -> None:
        return None

    async def create_request_if_below_open_limit(
        self,
        *,
        request: object,
        at: datetime,
        max_open_requests: int,
    ) -> CodeInvestigationRequestCreateResult:
        assert at == NOW
        assert max_open_requests > 0
        request_id = str(getattr(request, "id"))
        self.requests[request_id] = request
        return CodeInvestigationRequestCreateResult(request=request)  # type: ignore[arg-type]


class BoardService:
    async def get_board(self, board_id: str) -> object | None:
        if board_id != "board-1":
            return None
        return SimpleNamespace(
            id=board_id,
            settings={"code_traceability": {"mode": "advisory"}},
        )


class SpecService:
    async def get_spec(self, spec_id: str) -> object | None:
        if spec_id != "spec-1":
            return None
        return SimpleNamespace(id=spec_id, board_id="board-1", version=3)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.store = InvestigationStore()
        self.events: list[object] = []
        self.commit_count = 0
        self.services = SimpleNamespace(
            boards=BoardService(),
            specs=SpecService(),
            code_investigations=self.store,
            publish_domain_event=self._publish_domain_event,
        )

    async def _publish_domain_event(self, event: object) -> None:
        self.events.append(event)

    async def commit(self) -> None:
        self.commit_count += 1

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class BoundUnitOfWorkFactory:
    def __init__(self, uow: FakeUnitOfWork) -> None:
        self.uow = uow
        self.actors: list[object] = []

    def __call__(self, *, actor: object) -> FakeUnitOfWork:
        self.actors.append(actor)
        return self.uow


class ToolRegistry:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def tool(self):
        def register(handler):
            self.handlers[handler.__name__] = handler
            return handler

        return register


def investigation_service() -> CodeInvestigationService:
    return CodeInvestigationService(
        challenge_policy=HmacCodeInvestigationChallengePolicy(
            keys={"parity-v1": b"p" * 32},
            active_key_id="parity-v1",
        ),
        clock=lambda: NOW,
        id_factory=StableIds(),
    )


def rest_app(
    authentication: AuthenticationPort,
    uow: FakeUnitOfWork,
) -> FastAPI:
    app = FastAPI()
    app.include_router(rest_api.router)

    async def principal() -> Principal:
        resolved = await authentication.authenticate(AGENT_CREDENTIAL)
        assert resolved is not None
        return resolved

    async def unit_of_work() -> FakeUnitOfWork:
        return uow

    app.dependency_overrides[rest_api.require_principal] = principal
    app.dependency_overrides[rest_api.get_unit_of_work] = unit_of_work
    return app


def mcp_registry(
    authentication: AuthenticationPort,
    uow: FakeUnitOfWork,
) -> tuple[ToolRegistry, BoundUnitOfWorkFactory]:
    registry = ToolRegistry()
    factory = BoundUnitOfWorkFactory(uow)

    async def board_agent(board_id: str) -> object | None:
        assert board_id == "board-1"
        principal = await authentication.authenticate(AGENT_CREDENTIAL)
        if principal is None:
            return None
        return SimpleNamespace(
            agent_id=principal.subject,
            agent_name=principal.claims.get("name"),
            permissions=principal.claims.get("permissions"),
            realm_id=principal.realm_id,
        )

    mcp_tools.register_code_traceability_tools(
        registry,
        get_board_agent=board_agent,
        get_uow=lambda: factory,
        get_settings=lambda: SimpleNamespace(),
    )
    return registry, factory


@pytest.fixture(scope="module")
def registered_mcp_handlers() -> dict[str, Any]:
    registry, _factory = mcp_registry(
        InjectedAgentAuthentication(),
        FakeUnitOfWork(),
    )
    return registry.handlers


@pytest.fixture
def registered_relational_application_adapter() -> None:
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    try:
        yield
    finally:
        reset_relational_application_adapter_for_tests()


def _called_use_case_names(handler: Any) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr if isinstance(function, ast.Attribute) else None
        )
        if name is not None and name.endswith("UseCase"):
            names.add(name)
    return names


def _nested_model_types(annotation: object) -> tuple[type[BaseModel], ...]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    return tuple(
        nested
        for argument in get_args(annotation)
        for nested in _nested_model_types(argument)
    )


def _assert_closed_model_tree(root: type[BaseModel]) -> None:
    pending = [root]
    visited: set[type[BaseModel]] = set()
    while pending:
        model = pending.pop()
        if model in visited:
            continue
        visited.add(model)
        assert model.model_config.get("extra") == "forbid", model.__name__
        for field in model.model_fields.values():
            pending.extend(_nested_model_types(field.annotation))


def _rest_external_inputs(
    handler: Any,
) -> tuple[set[str], tuple[type[BaseModel], ...], set[str]]:
    hints = get_type_hints(handler)
    external: set[str] = set()
    body_models: tuple[type[BaseModel], ...] = ()
    body_fields: set[str] = set()
    for name, parameter in inspect.signature(handler).parameters.items():
        assert parameter.kind not in {
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        }
        if name in {"principal", "uow"}:
            continue
        if name != "body":
            external.add(name)
            continue
        annotation = hints[name]
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            body_models = (annotation,)
        else:
            body_models = tuple(
                candidate
                for candidate in get_args(annotation)
                if isinstance(candidate, type) and issubclass(candidate, BaseModel)
            )
            assert body_models and len(body_models) == len(get_args(annotation))
        body_fields = set().union(*(set(model.model_fields) for model in body_models))
        external.update(body_fields)

    if "selector" in external:
        selector_model = next(
            model.model_fields["selector"].annotation
            for model in body_models
            if "selector" in model.model_fields
        )
        assert isinstance(selector_model, type) and issubclass(
            selector_model,
            BaseModel,
        )
        selector_fields = set(selector_model.model_fields)
        selector_fields.remove("kind")
        selector_fields.add("selector_kind")
        external.remove("selector")
        external.update(selector_fields)
    if handler is rest_api.list_code_evidence:
        external.remove("lifecycle_status")
        external.add("status")
    if handler is rest_api.supersede_code_evidence:
        external.remove("evidence_id")
        external.add("supersedes_evidence_id")
    return external, body_models, body_fields


@pytest.mark.parametrize(
    (
        "mcp_name",
        "rest_name",
        "expected_use_case",
        "rest_path_owned",
        "operation_server_owned",
    ),
    TRANSPORT_CAPABILITIES,
)
def test_all_19_capabilities_have_closed_rest_mcp_input_and_core_use_case_parity(
    registered_mcp_handlers: dict[str, Any],
    mcp_name: str,
    rest_name: str,
    expected_use_case: str,
    rest_path_owned: frozenset[str],
    operation_server_owned: frozenset[str],
) -> None:
    """AC-13: both transports are closed adapters over one Core use-case family."""

    mcp_handler = registered_mcp_handlers[mcp_name]
    rest_handler = getattr(rest_api, rest_name)
    mcp_signature = inspect.signature(mcp_handler)
    assert getattr(mcp_handler, "__mcp_closed_schema__", False) is True
    assert all(
        parameter.kind
        not in {inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL}
        for parameter in mcp_signature.parameters.values()
    )

    rest_inputs, body_models, body_fields = _rest_external_inputs(rest_handler)
    for body_model in body_models:
        _assert_closed_model_tree(body_model)
    assert rest_path_owned.isdisjoint(body_fields)

    mcp_inputs = set(mcp_signature.parameters)
    assert rest_inputs == mcp_inputs
    server_owned = COMMON_SERVER_OWNED_FIELDS | operation_server_owned
    assert server_owned.isdisjoint(mcp_inputs)
    assert server_owned.isdisjoint(body_fields)
    assert _called_use_case_names(rest_handler) == {expected_use_case}
    assert _called_use_case_names(mcp_handler) == {expected_use_case}


@pytest.mark.asyncio
@pytest.mark.parametrize("source_role", ("existing_scaffold", "reference_pattern"))
async def test_contextual_interpretation_limit_rejection_is_transport_identical_and_write_free(
    source_role: str,
) -> None:
    body_payload = {
        "contract_version": 2,
        "investigation_receipt_id": "receipt-1",
        "parent_type": "card",
        "parent_id": "card-1",
        "evidence_type": "structure",
        "claim": "A contextual source that requires a human-readable limit.",
        "selector": {"kind": "file", "relative_path": "src/context.py"},
        "declared_source_content_sha256": "a" * 64,
        "idempotency_key": f"missing-limit-{source_role}",
        "source_role": source_role,
        "relevance_summary": "This source shapes the bounded implementation.",
        "scope_relation": "Inside the accepted delivery scope.",
        "source_origin": "Accepted repository baseline.",
        "baseline_provenance": {
            "presence": "committed_snapshot",
            "workspace_state_id": "workspace-1",
        },
    }

    rest_uow = FakeUnitOfWork()
    rest_body = rest_api.CodeEvidenceBodyV2.model_validate(body_payload)
    with pytest.raises(ValidationError) as rest_error:
        await rest_api.submit_code_evidence(
            "board-1",
            rest_body,
            Principal(
                subject="agent-1",
                realm_id=LOCAL_REALM_ID,
                actor_kind="agent",
            ),
            rest_uow,  # type: ignore[arg-type]
        )

    authentication = InjectedAgentAuthentication()
    mcp_uow = FakeUnitOfWork()
    registry, factory = mcp_registry(authentication, mcp_uow)
    mcp_error = await registry.handlers["okto_pulse_submit_code_evidence"](
        board_id="board-1",
        investigation_receipt_id="receipt-1",
        parent_type="card",
        parent_id="card-1",
        evidence_type="structure",
        claim="A contextual source that requires a human-readable limit.",
        selector_kind="file",
        relative_path="src/context.py",
        declared_source_content_sha256="a" * 64,
        idempotency_key=f"missing-limit-{source_role}",
        contract_version=2,
        source_role=source_role,
        relevance_summary="This source shapes the bounded implementation.",
        scope_relation="Inside the accepted delivery scope.",
        source_origin="Accepted repository baseline.",
        baseline_provenance={
            "presence": "committed_snapshot",
            "workspace_state_id": "workspace-1",
        },
    )

    assert "code_evidence_interpretation_limit_required" in str(rest_error.value)
    assert mcp_error.kind is McpOutcomeKind.ERROR
    assert mcp_error.code == "validation_failed"
    assert "code_evidence_interpretation_limit_required" in (
        mcp_error.details["errors"][0]["msg"]
    )
    assert rest_error.value.errors()[0]["type"] == (
        mcp_error.details["errors"][0]["type"]
    )
    assert rest_uow.store.requests == mcp_uow.store.requests == {}
    assert rest_uow.events == mcp_uow.events == []
    assert rest_uow.commit_count == mcp_uow.commit_count == 0
    assert authentication.calls == []
    assert factory.actors == []


@pytest.mark.asyncio
async def test_projection_output_parity_covers_every_subject_profile_and_scope(
    monkeypatch,
) -> None:
    from okto_pulse.core.application.use_cases.code_traceability import (
        GetCodeTraceabilityProjectionUseCase,
    )

    class Projection:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def as_dict(self) -> dict[str, object]:
            return self._payload

    async def execute(_self, query, *, actor, uow):
        assert actor is not None
        assert uow is not None
        editable = (
            query.subject_type is CodeTraceabilitySubjectType.REFINEMENT
            and query.profile
            in {
                CodeTraceabilityProjectionProfile.DETAIL,
                CodeTraceabilityProjectionProfile.FULL,
            }
            and query.context_scope is CodeTraceabilityContextScope.DEFAULT
        )
        return Projection(
            {
                "subject_type": query.subject_type.value,
                "subject_id": query.subject_id,
                "subject_version": query.subject_version,
                "profile": query.profile.value,
                "context_scope": query.context_scope.value,
                "source_context_classification_inputs": (
                    [
                        {
                            "evidence_id": "legacy-1",
                            "expected_evidence_payload_sha256": "a" * 64,
                            "expected_classification_revision": 0,
                        }
                    ]
                    if editable
                    else []
                ),
                "contextual_evidence_coverage": {
                    "total": 2,
                    "linked": 1,
                    "dispositioned": 0,
                    "pending": 1,
                    "pending_ids": ["technical_requirement:tr-2"],
                    "unresolved_applicability_count": 0,
                    "coverage_pct": 50.0,
                    "projection_complete": True,
                },
            }
        )

    monkeypatch.setattr(GetCodeTraceabilityProjectionUseCase, "execute", execute)
    authentication = InjectedAgentAuthentication()
    rest_uow = FakeUnitOfWork()
    cases = tuple(
        (subject_type, profile, scope)
        for subject_type in CodeTraceabilitySubjectType
        for profile, scope in (
            (
                CodeTraceabilityProjectionProfile.SUMMARY,
                CodeTraceabilityContextScope.DEFAULT,
            ),
            (
                CodeTraceabilityProjectionProfile.DETAIL,
                CodeTraceabilityContextScope.DEFAULT,
            ),
            (
                CodeTraceabilityProjectionProfile.FULL,
                CodeTraceabilityContextScope.DEFAULT,
            ),
            (
                CodeTraceabilityProjectionProfile.FULL,
                CodeTraceabilityContextScope.GATE,
            ),
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest_app(authentication, rest_uow)),
        base_url="http://test",
    ) as client:
        for subject_type, profile, scope in cases:
            subject_id = f"{subject_type.value}-1"
            rest_response = await client.get(
                "/boards/board-1/code-traceability-projection",
                params={
                    "subject_type": subject_type.value,
                    "subject_id": subject_id,
                    "subject_version": 3,
                    "profile": profile.value,
                    "context_scope": scope.value,
                },
            )
            mcp_payload = await mcp_server._mcp_code_traceability_projection(
                uow=SimpleNamespace(),
                actor=SimpleNamespace(),
                board_id="board-1",
                subject_type=subject_type.value,
                subject_id=subject_id,
                subject_version=3,
                profile=profile.value,
                context_scope=scope.value,
            )

            assert rest_response.status_code == 200
            assert rest_response.json() == mcp_payload
            editable = (
                subject_type is CodeTraceabilitySubjectType.REFINEMENT
                and profile
                in {
                    CodeTraceabilityProjectionProfile.DETAIL,
                    CodeTraceabilityProjectionProfile.FULL,
                }
                and scope is CodeTraceabilityContextScope.DEFAULT
            )
            assert bool(mcp_payload["source_context_classification_inputs"]) is editable
            assert mcp_payload["contextual_evidence_coverage"]["coverage_pct"] == 50.0


@pytest.mark.asyncio
async def test_ts_comm_10_greenfield_absence_has_real_rest_mcp_parity(
    tmp_path,
    registered_relational_application_adapter,
) -> None:
    database_path = tmp_path / "ts-comm-10-greenfield-absence.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    request, consumed, receipt, head, _workspace = _attestation_bundle(
        now,
        subject_type=CodeTraceabilitySubjectType.REFINEMENT,
        subject_id="refinement-greenfield",
        subject_version=3,
    )
    contextual_receipt = replace(
        receipt,
        context_contract_version=2,
        delivery_context=DeliveryContext.GREENFIELD,
        contextual_outcome=(
            ContextualInvestigationOutcomeV2.NO_RELEVANT_EXISTING_IMPLEMENTATION
        ),
        observation_sha256=code_investigation_observation_sha256_v2(
            source_ref=receipt.source_ref,
            selector_scope_digest=receipt.selector_scope_digest,
            delivery_context=DeliveryContext.GREENFIELD,
            outcome=(
                ContextualInvestigationOutcomeV2.NO_RELEVANT_EXISTING_IMPLEMENTATION
            ),
            capabilities=receipt.capabilities,
            source_identity_digest=receipt.source_identity_digest,
            declared_revision=receipt.declared_revision,
            workspace_state=receipt.workspace_state,
            omission_manifest=receipt.omission_manifest,
        ),
    )

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            ("board-1", "Board", "owner-1", "local"),
        )
        await connection.exec_driver_sql(
            "INSERT INTO ideations "
            "(id, board_id, title, status, edition, version, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ideation-1", "board-1", "Idea", "done", 1, 1, "owner-1"),
        )
        await connection.exec_driver_sql(
            "INSERT INTO refinements "
            "(id, ideation_id, board_id, title, delivery_context, status, "
            "edition, version, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "refinement-greenfield",
                "ideation-1",
                "board-1",
                "Greenfield refinement",
                "greenfield",
                "done",
                1,
                3,
                "owner-1",
            ),
        )

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        investigations = CommunityRelationalApplicationAdapter().code_investigations(
            session
        )
        await investigations.create_request(request)
        await investigations.consume_request_append_receipt_and_advance_head(
            request=consumed,
            receipt=contextual_receipt,
            head=head,
            expected_head_revision=None,
        )
        await session.commit()

    permissions = (
        "code_traceability.investigation.read",
        "code_traceability.evidence.read",
        "code_traceability.target.read",
        "code_traceability.overlap.read",
    )
    principal = Principal(
        subject="owner-1",
        realm_id=LOCAL_REALM_ID,
        actor_kind="human",
        claims={"permissions": permissions},
    )

    class Authentication:
        async def authenticate(self, credential: Credential | None) -> Principal | None:
            assert credential == AGENT_CREDENTIAL
            return principal

    async with sessions() as rest_session, sessions() as mcp_session:
        rest_uow = CommunityUnitOfWork(rest_session)
        mcp_uow = CommunityUnitOfWork(mcp_session)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=rest_app(Authentication(), rest_uow)  # type: ignore[arg-type]
            ),
            base_url="http://test",
        ) as client:
            rest_response = await client.get(
                "/boards/board-1/code-traceability-projection",
                params={
                    "subject_type": "refinement",
                    "subject_id": "refinement-greenfield",
                    "subject_version": 3,
                    "profile": "detail",
                    "context_scope": "default",
                },
            )
        mcp_payload = await mcp_server._mcp_code_traceability_projection(
            uow=mcp_uow,
            actor=ActorContext(
                "agent-1",
                "mcp",
                actor_kind="agent",
                board_id="board-1",
                realm_id=LOCAL_REALM_ID,
            ),
            board_id="board-1",
            subject_type="refinement",
            subject_id="refinement-greenfield",
            subject_version=3,
            profile="detail",
            context_scope="default",
        )

    assert rest_response.status_code == 200
    rest_payload = rest_response.json()
    assert rest_payload == mcp_payload
    assert rest_payload["source_context"]["delivery_context"] == "greenfield"
    assert rest_payload["source_context"]["investigation_outcome"] == (
        "no_relevant_existing_implementation"
    )
    assert rest_payload["source_context"]["evidence_applicable"] is False
    assert rest_payload["evidence"] == []
    assert rest_payload["source_context_items"] == []
    assert rest_payload["source_context_classification_inputs"] == []
    assert rest_payload["waivers"] == []
    assert rest_payload["coverage"]["skipped"] is False
    contextual_coverage = rest_payload["contextual_evidence_coverage"]
    assert contextual_coverage == {
        "total": 0,
        "linked": 0,
        "dispositioned": 0,
        "pending": 0,
        "pending_ids": [],
        "unresolved_applicability_count": 0,
        "coverage_pct": None,
        "projection_complete": True,
    }
    assert "status" not in contextual_coverage
    assert "not_applicable" not in contextual_coverage.values()
    await engine.dispose()


def event_snapshot(event: object) -> dict[str, object]:
    payload = event.model_dump(  # type: ignore[attr-defined]
        mode="json",
        exclude={"event_id", "occurred_at"},
    )
    return {
        "event_type": str(getattr(event, "event_type")),
        **payload,
    }


async def activity_snapshot(tmp_path, events: tuple[object, object]) -> list[dict]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'transport-parity.sqlite3').as_posix()}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, ?, ?, ?)",
            ("board-1", "Board", "owner-1", "local"),
        )

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    effects = CommunitySqlAlchemyCodeTraceabilityEventEffects()
    async with sessions() as session:
        for event in events:
            await effects.apply(session, event)
        await session.commit()

    async with sessions() as session:
        rows = tuple((await session.execute(select(ActivityLog))).scalars().all())
        snapshots = []
        for row in rows:
            details = dict(row.details)
            details.pop("event_id")
            snapshots.append(
                {
                    "action": row.action,
                    "actor_type": row.actor_type,
                    "actor_id": row.actor_id,
                    "board_id": row.board_id,
                    "card_id": row.card_id,
                    "details": details,
                }
            )
    await engine.dispose()
    return snapshots


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_agent_start_has_rest_mcp_mutation_event_activity_and_error_parity(
    monkeypatch,
    tmp_path,
) -> None:
    authentication = InjectedAgentAuthentication()
    assert isinstance(authentication, AuthenticationPort)
    monkeypatch.setattr(rest_api, "_investigation_service", investigation_service)
    monkeypatch.setattr(
        mcp_tools,
        "_challenge_service",
        lambda _get_settings: investigation_service(),
    )

    rest_uow = FakeUnitOfWork()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest_app(authentication, rest_uow)),
        base_url="http://test",
    ) as client:
        rest_success = await client.post(
            "/boards/board-1/code-investigations",
            json=START_BODY,
        )
    assert rest_success.status_code == 201

    mcp_uow = FakeUnitOfWork()
    registry, mcp_uow_factory = mcp_registry(authentication, mcp_uow)
    mcp_success = await registry.handlers["okto_pulse_start_code_investigation"](
        board_id="board-1",
        subject_type=CodeTraceabilitySubjectType.SPEC,
        subject_id="spec-1",
        expected_subject_version=3,
        idempotency_key="start-parity-1",
        source_ref=None,
    )

    assert mcp_success.kind is McpOutcomeKind.SUCCESS
    assert rest_success.json() == mcp_success.payload
    assert tuple(rest_uow.store.requests.values()) == tuple(
        mcp_uow.store.requests.values()
    )
    assert rest_uow.commit_count == mcp_uow.commit_count == 1
    assert len(rest_uow.events) == len(mcp_uow.events) == 1
    assert event_snapshot(rest_uow.events[0]) == event_snapshot(mcp_uow.events[0])
    assert len(mcp_uow_factory.actors) == 1
    assert getattr(mcp_uow_factory.actors[0], "actor_id") == "agent-1"

    activities = await activity_snapshot(
        tmp_path,
        (rest_uow.events[0], mcp_uow.events[0]),
    )
    assert len(activities) == 2
    assert activities[0] == activities[1]

    rest_error_uow = FakeUnitOfWork()
    invalid_body = {**START_BODY, "expected_subject_version": 4}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest_app(authentication, rest_error_uow)),
        base_url="http://test",
    ) as client:
        rest_error = await client.post(
            "/boards/board-1/code-investigations",
            json=invalid_body,
        )

    mcp_error_uow = FakeUnitOfWork()
    error_registry, _ = mcp_registry(authentication, mcp_error_uow)
    mcp_error = await error_registry.handlers["okto_pulse_start_code_investigation"](
        board_id="board-1",
        subject_type=CodeTraceabilitySubjectType.SPEC,
        subject_id="spec-1",
        expected_subject_version=4,
        idempotency_key="start-parity-1",
        source_ref=None,
    )

    assert rest_error.status_code == 409
    rest_detail = rest_error.json()["detail"]
    assert mcp_error.kind is McpOutcomeKind.ERROR
    assert mcp_error.code == rest_detail["code"]
    assert mcp_error.message == rest_detail["message"]
    assert {
        key: value for key, value in mcp_error.details.items() if key != "remediation"
    } == rest_detail["details"]
    assert mcp_error.details["remediation"] == rest_detail["remediation"]
    assert rest_error_uow.store.requests == mcp_error_uow.store.requests == {}
    assert rest_error_uow.events == mcp_error_uow.events == []
    assert rest_error_uow.commit_count == mcp_error_uow.commit_count == 0


@pytest.mark.asyncio
async def test_local_rest_keeps_agent_only_start_human_and_returns_typed_403(
    monkeypatch,
) -> None:
    monkeypatch.setattr(rest_api, "_investigation_service", investigation_service)
    authentication = LocalAuthProvider()
    local_principal = await authentication.authenticate(None)
    assert local_principal.actor_kind == "human"

    uow = FakeUnitOfWork()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest_app(authentication, uow)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/boards/board-1/code-investigations",
            json=START_BODY,
        )

    expected = CodeInvestigationActorKindRequired().to_error_dict()
    assert response.status_code == 403
    assert response.json() == {"detail": expected}
    assert uow.store.requests == {}
    assert uow.events == []
    assert uow.commit_count == 0


@pytest.mark.parametrize(
    ("path", "body"),
    (
        pytest.param(
            "/boards/board-1/code-investigations/request-1/receipts",
            {
                "challenge_token": "agent-owned-single-use-token",
                "outcome": "unavailable",
                "capabilities": [],
                "omission_manifest": [
                    {
                        "reason_code": "permission_denied",
                        "affected_scope_digest": "a" * 64,
                        "count": 1,
                    }
                ],
                "tooling": {
                    "tool_id": "external-agent",
                    "tool_version": "1",
                    "method_id": "source-preflight/v1",
                },
                "observed_at": NOW.isoformat(),
                "idempotency_key": "receipt-human-denied",
            },
            id="investigation-receipt",
        ),
        pytest.param(
            "/boards/board-1/code-evidence",
            {
                "investigation_receipt_id": "receipt-1",
                "parent_type": "spec",
                "parent_id": "spec-1",
                "evidence_type": "structure",
                "claim": "Agent-submitted observation.",
                "selector": {
                    "kind": "file",
                    "relative_path": "src/module.py",
                },
                "declared_source_content_sha256": "b" * 64,
                "idempotency_key": "evidence-human-denied",
            },
            id="code-evidence",
        ),
        pytest.param(
            "/boards/board-1/code-evidence/evidence-1/supersede",
            {
                "investigation_receipt_id": "receipt-1",
                "parent_type": "spec",
                "parent_id": "spec-1",
                "evidence_type": "structure",
                "claim": "Agent-submitted replacement observation.",
                "selector": {
                    "kind": "file",
                    "relative_path": "src/module.py",
                },
                "declared_source_content_sha256": "c" * 64,
                "idempotency_key": "supersession-human-denied",
                "supersession_reason": "Agent observed a newer revision.",
            },
            id="code-evidence-supersession",
        ),
        pytest.param(
            "/boards/board-1/cards/card-1/implementation-targets/target-1/"
            "resolution-receipts",
            {
                "investigation_receipt_id": "receipt-1",
                "state": "unavailable",
                "reason_code": "source_unavailable",
                "tooling": {
                    "tool_id": "external-agent",
                    "tool_version": "1",
                    "method_id": "target-resolution/v1",
                },
                "agent_observed_at": NOW.isoformat(),
                "idempotency_key": "resolution-human-denied",
            },
            id="target-resolution",
        ),
        pytest.param(
            "/boards/board-1/cards/card-1/implementation-targets/target-1/"
            "execution-receipts",
            {
                "result_investigation_receipt_id": "receipt-1",
                "disposition": "touched",
                "actual_relative_path": "src/module.py",
                "justification": "Agent declared the target as touched.",
                "idempotency_key": "execution-human-denied",
            },
            id="target-execution",
        ),
    ),
)
@pytest.mark.asyncio
async def test_local_rest_rejects_every_other_agent_submission_family(
    monkeypatch,
    path: str,
    body: dict[str, object],
) -> None:
    monkeypatch.setattr(rest_api, "_investigation_service", investigation_service)
    authentication = LocalAuthProvider()
    local_principal = await authentication.authenticate(None)
    assert local_principal.actor_kind == "human"

    uow = FakeUnitOfWork()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest_app(authentication, uow)),
        base_url="http://test",
    ) as client:
        response = await client.post(path, json=body)

    expected = CodeInvestigationActorKindRequired().to_error_dict()
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": expected}
    assert uow.store.requests == {}
    assert uow.events == []
    assert uow.commit_count == 0
