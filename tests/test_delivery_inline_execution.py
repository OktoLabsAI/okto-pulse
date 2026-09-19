"""Real origin admission, receipt/binding/outbox rollback and exact replay."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, func, update

from test_delivery_progress import db as _db
from test_code_traceability_persistence import _attestation_bundle
from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
    CommunitySqlAlchemyCodeInvestigationStore,
    CommunitySqlAlchemyCodeTraceabilityStore,
    _request_row,
    _receipt_row,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    CodeInvestigationRequestRow,
    CodeInvestigationReceiptRow,
    CodeInvestigationHeadRow,
    ImplementationTargetRow,
    ImplementationTargetExecutionRecordRow,
    CardDeliveryEvidenceRecordRow,
    DomainEventRow,
    DomainEventHandlerExecution,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.code_traceability import (
    SubmitImplementationTargetExecutionUseCase,
)
from okto_pulse.core.application.use_cases.delivery_evidence import (
    RecordCardDeliveryEvidenceUseCase,
)
from okto_pulse.core.models.delivery_evidence import (
    CardDeliveryEvidenceBatchCommand,
    CardDeliveryEvidenceCommand,
    DeliveryBatchEntryError,
)
from okto_pulse.core.services.code_investigation import (
    CodeInvestigationService,
    selector_scope_digest_for_card_targets,
)
from okto_pulse.core.services.implementation_targets import ImplementationTargetService
from okto_pulse.core.domain.code_traceability import (
    code_investigation_observation_sha256,
)

db = _db


@pytest_asyncio.fixture
async def composed(db):
    _, session, store = db
    now = datetime.now(timezone.utc)
    _, request, receipt, head, workspace = _attestation_bundle(now, subject_id="c")
    workspace = replace(workspace, declared_revision="a" * 40)
    digest = selector_scope_digest_for_card_targets(
        board_id="b", card_id="c", card_version=1, targets=(("target", 1),)
    )
    request = replace(request, board_id="b", selector_scope_digest=digest)
    receipt = replace(
        receipt,
        board_id="b",
        selector_scope_digest=digest,
        workspace_state=workspace,
        declared_revision=workspace.declared_revision,
        observation_sha256=code_investigation_observation_sha256(
            source_ref=receipt.source_ref,
            selector_scope_digest=digest,
            outcome=receipt.outcome,
            capabilities=receipt.capabilities,
            source_identity_digest=receipt.source_identity_digest,
            declared_revision=workspace.declared_revision,
            workspace_state=workspace,
            omission_manifest=(),
        ),
    )
    await session.execute(
        insert(CodeInvestigationRequestRow).values(**_request_row(request))
    )
    await session.execute(
        insert(CodeInvestigationReceiptRow).values(**_receipt_row(receipt))
    )
    session.add(
        CodeInvestigationHeadRow(
            board_id="b",
            source_ref=head.source_ref,
            generation=1,
            latest_receipt_id=receipt.id,
            current_receipt_id=receipt.id,
            state="current",
            revision=1,
            updated_at=now,
        )
    )
    session.add(
        ImplementationTargetRow(
            id="target",
            board_id="b",
            card_id="c",
            source_ref=receipt.source_ref,
            selector_kind="file",
            relative_path_hint="src/file.py",
            role="modify",
            intent="Deliver",
            required=True,
            source_spec_version=1,
            lifecycle_status="active",
            revision=1,
            created_by="agent-1",
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()

    async def publish(event):
        await CommunitySqlAlchemyDomainEventPublisher().publish(
            session, event=event, handler_names=("test_observer",)
        )

    async def board(identity):
        return await session.get(Board, identity)

    async def card(identity):
        return await session.get(Card, identity)

    services = SimpleNamespace(
        delivery_evidence=store,
        code_investigations=CommunitySqlAlchemyCodeInvestigationStore(session),
        code_traceability=CommunitySqlAlchemyCodeTraceabilityStore(session),
        boards=SimpleNamespace(get_board=board),
        cards=SimpleNamespace(get_card=card),
        publish_domain_event=publish,
        agents=SimpleNamespace(agent_has_board_access=AsyncMock(return_value=True)),
    )
    uow = SimpleNamespace(
        services=services, commit=AsyncMock(side_effect=session.commit)
    )
    origin = SubmitImplementationTargetExecutionUseCase(
        CodeInvestigationService(), ImplementationTargetService()
    )
    actor = ActorContext(
        actor_id="agent-1",
        actor_kind="agent",
        source="mcp",
        board_id="b",
        permissions=[
            "code_traceability.target.execution_submit",
            "card.conclusion.write",
        ],
    )
    yield session, uow, RecordCardDeliveryEvidenceUseCase(origin), actor


def command(*, batch=True, second=False, **changes):
    entry = dict(
        kind="implementation",
        obligation_refs=["card:c"],
        justification="Implemented parser",
        execution_submission=dict(
            target_id="target",
            result_investigation_receipt_id="receipt-1",
            disposition="touched",
            actual_relative_path="src/file.py",
        ),
    )
    common = dict(
        board_id="b",
        card_id="c",
        spec_id="s",
        expected_card_version=1,
        expected_spec_edition=1,
        idempotency_key="inline",
        **changes,
    )
    if not batch:
        return CardDeliveryEvidenceCommand(**common, **entry)
    entries = [{**entry, "client_ref": "proof"}]
    if second:
        entries.append(
            dict(
                client_ref="bad",
                kind="progress",
                justification="Unknown target",
                progress=dict(
                    source_state={
                        "workspace_state": "unknown",
                        "recoverability": "unknown",
                    },
                    remaining="Next",
                    target_ids=["foreign"],
                ),
            )
        )
    return CardDeliveryEvidenceBatchCommand(
        **common,
        contract_version="card-delivery-batch/v1",
        expected_delivery_revision=0,
        entries=entries,
    )


async def counts(session):
    return [
        await session.scalar(select(func.count()).select_from(model))
        for model in (
            ImplementationTargetExecutionRecordRow,
            CardDeliveryEvidenceRecordRow,
            DomainEventRow,
            DomainEventHandlerExecution,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_inline_real_origin_commits_once_and_replay_has_same_ids(composed, batch):
    session, uow, use_case, actor = composed
    first = await use_case.execute(command(batch=batch), actor=actor, uow=uow)
    assert await counts(session) == [1, 1, 1, 1]
    uow.commit.assert_awaited_once()
    saved = first["entries"][0] if batch else first
    row = await session.get(
        ImplementationTargetExecutionRecordRow, saved["execution_id"]
    )
    assert row.submitted_by == "agent-1" and row.justification == "Implemented parser"
    binding = await session.get(CardDeliveryEvidenceRecordRow, saved["id"])
    assert binding.payload["execution_id"] == row.id
    assert "execution_submission" not in binding.payload
    second = await use_case.execute(command(batch=batch), actor=actor, uow=uow)
    assert second == {**first, "replayed": True}
    assert await counts(session) == [1, 1, 1, 1]


@pytest.mark.asyncio
async def test_later_failure_rolls_back_receipt_binding_event_and_handler(composed):
    session, uow, use_case, actor = composed
    with pytest.raises(DeliveryBatchEntryError) as error:
        await use_case.execute(command(second=True), actor=actor, uow=uow)
    assert error.value.entry_index == 1
    uow.commit.assert_not_awaited()
    await (
        session.commit()
    )  # Even a caller catching the failure cannot leak the origin write.
    assert await counts(session) == [0, 0, 0, 0]
    result = await use_case.execute(command(), actor=actor, uow=uow)
    assert result["entries"][0]["execution_id"]


@pytest.mark.asyncio
async def test_outer_rollback_also_reverts_single_inline(composed):
    session, uow, use_case, actor = composed
    uow.commit.side_effect = None
    await use_case.execute(command(batch=False), actor=actor, uow=uow)
    await session.rollback()
    assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["human", "user"])
async def test_inline_cannot_lend_agent_attestation_to_human(composed, kind):
    session, uow, use_case, actor = composed
    actor.actor_kind = kind
    with pytest.raises(ValueError):
        await use_case.execute(command(), actor=actor, uow=uow)
    assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
async def test_replay_rechecks_current_attestor_allowlist(composed):
    session, uow, use_case, actor = composed
    await use_case.execute(command(), actor=actor, uow=uow)
    await session.execute(
        update(Board)
        .where(Board.id == "b")
        .values(
            settings={
                "code_traceability": {
                    "accepted_attestor_policy": "granular_permission_and_board_allowlist"
                }
            }
        )
    )
    await session.commit()
    uow.services.agents.agent_has_board_access.return_value = False
    with pytest.raises(ValueError, match="attestor"):
        await use_case.execute(command(), actor=actor, uow=uow)
    assert await counts(session) == [1, 1, 1, 1]


@pytest.mark.asyncio
async def test_invalid_single_binding_rolls_back_accepted_execution(composed):
    session, uow, use_case, actor = composed
    payload = command(batch=False).model_dump()
    payload["obligation_refs"] = ["fr:foreign"]
    with pytest.raises(ValueError, match="obligation_not_found"):
        await use_case.execute(
            CardDeliveryEvidenceCommand.model_validate(payload), actor=actor, uow=uow
        )
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
async def test_invalid_origin_is_not_degraded_to_progress(composed):
    session, uow, use_case, actor = composed
    payload = command().model_dump()
    payload["entries"][0]["execution_submission"]["target_id"] = "foreign"
    with pytest.raises(DeliveryBatchEntryError) as error:
        await use_case.execute(
            CardDeliveryEvidenceBatchCommand.model_validate(payload),
            actor=actor,
            uow=uow,
        )
    assert error.value.details() == {
        "entry_index": 0,
        "client_ref": "proof",
        "cause_code": "delivery_entry_invalid",
    }
    await session.commit()
    assert await counts(session) == [0, 0, 0, 0]


@pytest.mark.asyncio
async def test_standalone_origin_keeps_its_commit_and_replay_contract(composed):
    from okto_pulse.core.models.code_traceability import (
        ImplementationTargetExecutionSubmission,
    )

    session, uow, _, actor = composed
    origin = SubmitImplementationTargetExecutionUseCase(
        CodeInvestigationService(), ImplementationTargetService()
    )
    entry = command(batch=False)
    submission = ImplementationTargetExecutionSubmission(
        board_id="b",
        card_id="c",
        idempotency_key="standalone",
        justification=entry.justification,
        **entry.execution_submission.model_dump(),
    )
    first = await origin.execute(submission, actor=actor, uow=uow)
    uow.commit.assert_awaited_once()
    assert await counts(session) == [1, 0, 1, 1]
    replay = await origin.execute(submission, actor=actor, uow=uow)
    assert replay.replayed and replay.record.id == first.record.id
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("declare_contribution", [False, True])
async def test_rest_inline_and_mcp_replay_use_origin_composition(
    composed, monkeypatch, declare_contribution
):
    from contextlib import asynccontextmanager
    import httpx
    from test_code_traceability_rest import _projection_rest_app
    from okto_pulse.community.api import code_traceability as api
    from okto_pulse.core.application.use_cases import delivery_evidence as delivery_app
    from okto_pulse.core.application.use_cases import code_traceability as origin_app
    from okto_pulse.core.ports.authentication import Principal
    from okto_pulse.core.mcp.catalog import CoreMcpCatalog
    from okto_pulse.core.mcp.code_traceability_tools import (
        register_code_traceability_tools,
    )

    session, uow, _, _ = composed
    # Transport mapping is isolated here; native permission/allowlist tests use
    # the actual authorizer in the same SQL fixture above.
    monkeypatch.setattr(delivery_app, "require_authorization", AsyncMock())
    monkeypatch.setattr(origin_app, "require_authorization", AsyncMock())
    rest = _projection_rest_app(uow)
    rest.dependency_overrides[api.require_principal] = lambda: Principal(
        subject="agent-1", realm_id="local", actor_kind="agent"
    )
    payload = command().model_dump(
        mode="json", exclude={"board_id", "card_id", "spec_id"}
    )
    if declare_contribution:
        payload["entries"][0].pop("obligation_refs")
        payload["entries"][0]["bindings"] = [
            {"obligation_ref": "card:c", "contribution": "partial"}
        ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest), base_url="http://test"
    ) as client:
        saved = await client.post(
            "/boards/b/cards/c/specs/s/delivery-evidence", json=payload
        )
    assert saved.status_code == 200, saved.text

    @asynccontextmanager
    async def scope(**kwargs):
        yield uow

    async def agent(board_id):
        return SimpleNamespace(
            agent_id="agent-1",
            agent_name="Agent",
            board_id=board_id,
            realm_id="local",
            permissions=(),
        )

    catalog = CoreMcpCatalog(name="inline", version="1")
    register_code_traceability_tools(
        catalog,
        get_board_agent=agent,
        get_uow=lambda: scope,
        get_settings=SimpleNamespace,
    )
    tool = await catalog.get_tool("okto_pulse_record_delivery_evidence")
    replay = await tool.fn(board_id="b", card_id="c", spec_id="s", evidence=payload)
    assert not replay.is_error, replay
    assert replay.payload == {**saved.json(), "replayed": True}
    assert await counts(session) == [1, 1, 1, 1]
    if declare_contribution:
        record = await session.get(
            CardDeliveryEvidenceRecordRow, saved.json()["entries"][0]["id"]
        )
        assert record.payload["contributions"] == payload["entries"][0]["bindings"]
