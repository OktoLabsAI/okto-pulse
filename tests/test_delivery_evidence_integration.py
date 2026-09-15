"""Isolated real SQL + signed Test Evidence integration (no production runtime)."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import httpx
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import select, update, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Spec,
    Card,
    DeliveryEvidenceRecordRow,
    ImplementationTargetRow,
    ImplementationTargetExecutionRecordRow,
    CodeInvestigationReceiptRow,
    CodeInvestigationRequestRow,
    CodeInvestigationReceiptRevocationRow,
)
from okto_pulse.community.adapters.sqlalchemy_code_traceability import (
    _receipt_row,
    _request_row,
)
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import (
    CommunityDeliveryEvidenceStore,
)
from okto_pulse.community.adapters.test_evidence import (
    CommunityTestEvidenceWriteVerifier,
)
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceCommand
from okto_pulse.core.domain.code_traceability import (
    code_investigation_observation_sha256,
)
from okto_pulse.core.ports.test_evidence import (
    register_test_evidence_write_verifier,
    reset_test_evidence_write_verifier_for_tests,
)
from test_code_traceability_persistence import _attestation_bundle
from test_evidence_v2_adapter import (
    _produce,
    BOARD_ID,
    SPEC_ID,
    SCENARIO,
    ACCEPTANCE_CRITERIA,
)


@pytest_asyncio.fixture
async def ledger(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delivery.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql(
            "INSERT INTO boards (id, name, owner_id, realm_id) VALUES (?, 'Delivery', 'owner', 'local')",
            (BOARD_ID,),
        )
        await conn.exec_driver_sql(
            "INSERT INTO specs (id, board_id, title, status, version, created_by) VALUES (?, ?, 'Spec', 'in_progress', 1, 'owner')",
            (SPEC_ID, BOARD_ID),
        )
        for card_id, card_type in (("task", "normal"), ("test", "test")):
            await conn.exec_driver_sql(
                "INSERT INTO cards (id, board_id, spec_id, title, status, position, created_by, card_type) VALUES (?, ?, ?, ?, 'done', 0, 'owner', ?)",
                (card_id, BOARD_ID, SPEC_ID, card_id, card_type),
            )
    session = async_sessionmaker(engine, expire_on_commit=False)()
    evidence_ledger, _, evidence = await _produce(tmp_path)
    register_test_evidence_write_verifier(
        CommunityTestEvidenceWriteVerifier(ledger=evidence_ledger)
    )
    await session.execute(
        update(Spec)
        .where(Spec.id == SPEC_ID)
        .values(
            acceptance_criteria=ACCEPTANCE_CRITERIA,
            test_scenarios=[{**SCENARIO, "status": "passed", "evidence": evidence}],
        )
    )
    await session.execute(
        update(Card).where(Card.id == "test").values(test_scenario_ids=[SCENARIO["id"]])
    )
    now = datetime(2026, 7, 14, 14, tzinfo=timezone.utc)
    request, consumed, receipt, _, workspace = _attestation_bundle(
        now, subject_id="task"
    )
    workspace = replace(workspace, declared_revision="a" * 40)
    request = replace(consumed, board_id=BOARD_ID)
    observation = code_investigation_observation_sha256(
        source_ref=receipt.source_ref,
        selector_scope_digest=receipt.selector_scope_digest,
        outcome=receipt.outcome,
        capabilities=receipt.capabilities,
        source_identity_digest=receipt.source_identity_digest,
        declared_revision=workspace.declared_revision,
        workspace_state=workspace,
        omission_manifest=(),
    )
    receipt = replace(
        receipt,
        board_id=BOARD_ID,
        declared_revision="a" * 40,
        workspace_state=workspace,
        observation_sha256=observation,
    )
    await session.execute(
        insert(CodeInvestigationRequestRow).values(**_request_row(request))
    )
    await session.execute(
        insert(CodeInvestigationReceiptRow).values(**_receipt_row(receipt))
    )
    session.add(
        ImplementationTargetRow(
            id="target",
            board_id=BOARD_ID,
            card_id="task",
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
    await session.flush()
    session.add(
        ImplementationTargetExecutionRecordRow(
            id="execution",
            board_id=BOARD_ID,
            card_id="task",
            target_id="target",
            target_revision=1,
            result_investigation_receipt_id=receipt.id,
            source_ref=receipt.source_ref,
            disposition="touched",
            result_declared_revision="a" * 40,
            result_workspace_state_id=workspace.workspace_state_id,
            actual_relative_path="src/file.py",
            justification="Delivered code",
            submitted_by="agent-1",
            received_at=now,
            payload_sha256="b" * 64,
            idempotency_key="execution",
        )
    )
    await session.commit()
    try:
        yield session, CommunityDeliveryEvidenceStore(session), evidence
    finally:
        await session.close()
        await engine.dispose()
        reset_test_evidence_write_verifier_for_tests()


def command(kind="implementation", **kwargs):
    values = dict(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        expected_edition=1,
        expected_version=1,
        idempotency_key=kind,
        kind=kind,
        obligation_refs=["ac:ac-about"],
        justification="The implemented About version is tested by this health assertion.",
    )
    if kind == "implementation":
        values.update(card_id="task", execution_id="execution")
    elif kind == "test":
        values.update(card_id="test", scenario_id=SCENARIO["id"])
    elif kind == "waiver":
        values.update(phase="implementation")
    elif kind == "revoke":
        values.update(obligation_refs=[])
    return DeliveryEvidenceCommand(**{**values, **kwargs})


async def record(store, data, human=False):
    return await store.record(
        data,
        actor_id="owner" if human else "agent-1",
        actor_kind="user" if human else "agent",
    )


@pytest.mark.asyncio
async def test_full_delivery_roundtrip_signed_test_replay_and_read_only_projection(
    ledger,
):
    session, store, _ = ledger
    assert not (await store.projection(BOARD_ID, SPEC_ID))["allowed"]
    impl = await record(store, command())
    assert (await record(store, command())) == {"id": impl["id"], "replayed": True}
    test = await record(store, command("test", implementation_ids=[impl["id"]]))
    await session.commit()
    before = len((await session.scalars(select(DeliveryEvidenceRecordRow))).all())
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert view["allowed"] and view["rows"][0]["test_ids"] == (test["id"],)
    assert view["rows"][0]["implementation_ids"] == (impl["id"],)
    assert (
        len((await session.scalars(select(DeliveryEvidenceRecordRow))).all()) == before
    )
    assert (await session.get(Spec, SPEC_ID)).status.value == "in_progress"
    assert {c["kind"] for c in view["candidates"]} == {"implementation", "test"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "failed",
        "tampered",
        "unlinked",
        "task_is_not_test",
        "archived",
        "requirement_changed",
        "target_changed",
        "receipt_revoked",
        "edition_changed",
    ],
)
async def test_currentness_is_rechecked_not_cached(ledger, mutation):
    session, store, evidence = ledger
    impl = await record(store, command())
    await record(store, command("test", implementation_ids=[impl["id"]]))
    spec = await session.get(Spec, SPEC_ID)
    spec_updates = {}
    if mutation == "failed":
        spec_updates["test_scenarios"] = [
            {**SCENARIO, "status": "failed", "evidence": evidence}
        ]
    elif mutation == "tampered":
        spec_updates["test_scenarios"] = [
            {
                **SCENARIO,
                "status": "passed",
                "evidence": {**evidence, "execution_receipt": "forged"},
            }
        ]
    elif mutation == "unlinked":
        await session.execute(
            update(Card).where(Card.id == "test").values(test_scenario_ids=[])
        )
    elif mutation == "task_is_not_test":
        await session.execute(
            update(Card).where(Card.id == "test").values(card_type="normal")
        )
    elif mutation == "archived":
        await session.execute(
            update(Card).where(Card.id == "task").values(archived=True)
        )
    elif mutation == "requirement_changed":
        spec_updates["acceptance_criteria"] = [
            {"id": "ac-about", "text": "Different behavior"}
        ]
    elif mutation == "target_changed":
        await session.execute(update(ImplementationTargetRow).values(revision=2))
    elif mutation == "receipt_revoked":
        session.add(
            CodeInvestigationReceiptRevocationRow(
                id="revocation",
                board_id=BOARD_ID,
                receipt_id="receipt-1",
                reason_code="invalid",
                justification="Invalid source claim",
                revoked_by="owner",
                revoked_at=datetime.now(timezone.utc),
            )
        )
    elif mutation == "edition_changed":
        spec_updates["edition"] = 2
    if spec_updates:
        await session.execute(
            update(Spec).where(Spec.id == spec.id).values(**spec_updates)
        )
    await session.commit()
    assert not (await store.projection(BOARD_ID, SPEC_ID))["allowed"]


@pytest.mark.asyncio
async def test_waiver_is_human_scoped_phase_specific_revocable_and_preserves_done(
    ledger,
):
    session, store, _ = ledger
    await session.execute(update(Spec).values(status="done"))
    with pytest.raises(ValueError, match="human_authorization"):
        await record(store, command("waiver"))
    first = await record(store, command("waiver"), human=True)
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert not view["allowed"] and view["rows"][0]["implementation_waiver_ids"]
    await record(
        store, command("waiver", phase="test", idempotency_key="waive-test"), human=True
    )
    assert (await store.projection(BOARD_ID, SPEC_ID))["allowed"]
    await record(store, command("revoke", record_id=first["id"]), human=True)
    view = await store.projection(BOARD_ID, SPEC_ID)
    assert not view["allowed"] and view["status"] == "done"
    assert len(view["records"]) == 3


@pytest.mark.asyncio
async def test_rejects_cross_scope_stale_version_unknown_obligation_and_replay_conflict(
    ledger,
):
    session, store, _ = ledger
    for data, error in (
        (command(expected_version=2), "version_conflict"),
        (command(expected_edition=2), "edition_conflict"),
        (command(obligation_refs=["fr:missing"]), "obligation_not_found"),
        (command(card_id="test"), "accepted_committed_task"),
        (command(spec_id="other"), "spec_not_found"),
    ):
        with pytest.raises(ValueError, match=error):
            await record(store, data)
    assert not (await session.scalars(select(DeliveryEvidenceRecordRow))).all()
    await record(store, command())
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await record(store, command(justification="Changed request"))
    with pytest.raises(ValueError, match="verified_test_and_implementation"):
        await record(store, command("test", implementation_ids=["foreign-delivery"]))


@pytest.mark.asyncio
async def test_core_use_case_denies_before_persistence_and_agents_cannot_waive(
    monkeypatch,
):
    from okto_pulse.core.application.use_cases import delivery_evidence as app
    from okto_pulse.core.application.use_cases.base import PermissionDeniedError

    store = SimpleNamespace(record=AsyncMock())
    uow = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store))
    actor = SimpleNamespace(actor_id="agent", actor_kind="agent")
    monkeypatch.setattr(
        app,
        "require_authorization",
        AsyncMock(side_effect=PermissionDeniedError("denied")),
    )
    with pytest.raises(PermissionDeniedError):
        await app.RecordDeliveryEvidenceUseCase().execute(
            command(), actor=actor, uow=uow
        )
    store.record.assert_not_awaited()
    monkeypatch.setattr(app, "require_authorization", AsyncMock())
    with pytest.raises(PermissionDeniedError, match="human_authorization"):
        await app.RecordDeliveryEvidenceUseCase().execute(
            command("waiver"), actor=actor, uow=uow
        )
    store.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_sqlite_race_has_one_audit_record_and_one_replay(ledger):
    session, _, _ = ledger
    factory = async_sessionmaker(session.bind, expire_on_commit=False)

    async def worker():
        async with factory() as db:
            result = await record(CommunityDeliveryEvidenceStore(db), command())
            await db.commit()
            return result

    results = await asyncio.gather(worker(), worker())
    assert results[0]["id"] == results[1]["id"]
    assert sorted(r["replayed"] for r in results) == [False, True]
    assert len((await session.scalars(select(DeliveryEvidenceRecordRow))).all()) == 1


@pytest.mark.asyncio
async def test_database_rejects_audit_rewriting(ledger):
    session, store, _ = ledger
    saved = await record(store, command())
    await session.commit()
    with pytest.raises(IntegrityError, match="delivery_audit_immutable"):
        await session.execute(
            update(DeliveryEvidenceRecordRow)
            .where(DeliveryEvidenceRecordRow.id == saved["id"])
            .values(actor_id="someone-else")
        )
    await session.rollback()
    assert (
        await session.get(DeliveryEvidenceRecordRow, saved["id"])
    ).actor_id == "agent-1"


@pytest.mark.asyncio
async def test_old_signed_test_cannot_be_associated_with_newer_source_observation(
    ledger,
):
    session, store, _ = ledger
    await session.execute(
        update(CodeInvestigationReceiptRow).values(
            observed_at=datetime(2026, 7, 15, tzinfo=timezone.utc)
        )
    )
    impl = await record(store, command())
    with pytest.raises(ValueError, match="verified_test_and_implementation"):
        await record(store, command("test", implementation_ids=[impl["id"]]))


@pytest.mark.asyncio
async def test_rest_roundtrip_closed_schema_and_domain_errors(ledger, monkeypatch):
    from test_code_traceability_rest import _projection_rest_app
    from okto_pulse.core.application.use_cases import delivery_evidence as app
    from okto_pulse.community.api import code_traceability as api
    from okto_pulse.core.ports.authentication import Principal

    session, store, _ = ledger
    uow = SimpleNamespace(
        services=SimpleNamespace(delivery_evidence=store),
        commit=AsyncMock(side_effect=session.commit),
    )
    # Transport principal is supplied by the existing authenticated REST fixture;
    # separate tests assert that denied authorization never reaches this store.
    monkeypatch.setattr(app, "require_authorization", AsyncMock())
    rest_app = _projection_rest_app(uow)
    rest_app.dependency_overrides[api.require_principal] = lambda: Principal(
        subject="owner", realm_id="local", actor_kind="human"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rest_app), base_url="http://test"
    ) as client:
        url = f"/boards/{BOARD_ID}/specs/{SPEC_ID}/delivery-evidence"
        response = await client.get(url)
        assert response.status_code == 200 and response.json()["allowed"] is False
        payload = command().model_dump(exclude={"board_id", "spec_id"})
        forged = await client.post(url, json={**payload, "verified": True})
        assert forged.status_code == 422
        saved = await client.post(url, json=payload)
        assert saved.status_code == 200, saved.text
        test_payload = command(
            "test", implementation_ids=[saved.json()["id"]]
        ).model_dump(exclude={"board_id", "spec_id"})
        tested = await client.post(url, json=test_payload)
        assert tested.status_code == 200, tested.text
        assert (await client.get(url)).json()["allowed"] is True
        stale = await client.post(
            url, json={**payload, "expected_version": 50, "idempotency_key": "stale"}
        )
        assert (
            stale.status_code == 409
            and stale.json()["detail"]["code"] == "delivery_version_conflict"
        )


@pytest.mark.asyncio
async def test_mcp_runs_same_store_closed_inputs_permissions_and_explicit_errors(
    ledger, monkeypatch
):
    from okto_pulse.core.application.use_cases import delivery_evidence as app
    from okto_pulse.core.mcp.catalog import CoreMcpCatalog
    from okto_pulse.core.mcp.code_traceability_tools import (
        register_code_traceability_tools,
    )

    session, store, _ = ledger
    auth = AsyncMock()
    monkeypatch.setattr(app, "require_authorization", auth)
    uow = SimpleNamespace(
        services=SimpleNamespace(delivery_evidence=store),
        commit=AsyncMock(side_effect=session.commit),
    )

    @asynccontextmanager
    async def scope(**kwargs):
        assert kwargs["actor"].actor_kind == "agent"
        yield uow

    async def agent(board_id):
        return SimpleNamespace(
            agent_id="agent-1",
            agent_name="Test Agent",
            board_id=board_id,
            realm_id="local",
            permissions=(),
        )

    catalog = CoreMcpCatalog(name="delivery-integration", version="1")
    register_code_traceability_tools(
        catalog,
        get_board_agent=agent,
        get_uow=lambda: scope,
        get_settings=SimpleNamespace,
    )
    write = await catalog.get_tool("okto_pulse_record_delivery_evidence")
    read = await catalog.get_tool("okto_pulse_get_delivery_evidence")
    bad = await write.fn(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        evidence={
            **command().model_dump(exclude={"board_id", "spec_id"}),
            "verified": True,
        },
    )
    assert bad.is_error and bad.code == "validation_failed"
    auth.assert_not_awaited()
    saved = await write.fn(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        evidence=command().model_dump(exclude={"board_id", "spec_id"}),
    )
    assert not saved.is_error, saved
    tested = await write.fn(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        evidence=command("test", implementation_ids=[saved.payload["id"]]).model_dump(
            exclude={"board_id", "spec_id"}
        ),
    )
    assert not tested.is_error, tested
    assert auth.await_args.args[1].operation == "spec.tests.execute"
    result = await read.fn(board_id=BOARD_ID, spec_id=SPEC_ID)
    assert result.payload["allowed"]
    stale = await write.fn(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        evidence=command(expected_version=90, idempotency_key="stale").model_dump(
            exclude={"board_id", "spec_id"}
        ),
    )
    assert stale.is_error and stale.code == "delivery_version_conflict"
    waiver = await write.fn(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        evidence=command("waiver").model_dump(exclude={"board_id", "spec_id"}),
    )
    assert waiver.is_error and waiver.code == "forbidden"


@pytest.mark.asyncio
async def test_upgrade_adds_only_delivery_table_and_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.sqlite'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        t
                        for t in Base.metadata.tables.values()
                        if t.name != "delivery_evidence_records"
                    ],
                )
            )
            await conn.exec_driver_sql(
                "INSERT INTO boards (id, name, owner_id, realm_id) VALUES ('old-board', 'Existing', 'owner', 'local')"
            )
            await conn.exec_driver_sql(
                "INSERT INTO specs (id, board_id, title, status, version, created_by) VALUES ('old-spec', 'old-board', 'Existing done Spec', 'done', 7, 'owner')"
            )
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(Base.metadata.create_all)
            current = (
                await conn.exec_driver_sql(
                    "SELECT status, version FROM specs WHERE id='old-spec'"
                )
            ).one()
            assert tuple(current) == ("done", 7)
            assert (
                await conn.exec_driver_sql(
                    "SELECT count(*) FROM delivery_evidence_records"
                )
            ).scalar_one() == 0
            assert (
                await conn.exec_driver_sql(
                    "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_delivery_evidence_%'"
                )
            ).scalar_one() == 3
    finally:
        await engine.dispose()
