"""Issuance must reserve before execution without retaining a REST writer.

Use the real REST dependency, Community UoW, local authentication and reservation
provider over a file-backed native schema. No dependency overrides are installed.
"""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import traceback

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select, text, update

from okto_pulse.core import configure_auth
from okto_pulse.core.domain.guideline_policy import PolicyEntityType
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.relational_application import register_relational_application_adapter
from okto_pulse.core.ports.test_evidence import (
    register_test_evidence_execution_issuer,
    register_test_evidence_write_verifier,
    register_test_verification_report_issuer,
)
from okto_pulse.core.ports.verification_currentness import (
    register_verification_currentness_store_factory,
    register_verification_reservation_port,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract, initialize_current_schema,
)
from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine, build_community_session_factory, install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Spec, VerificationAcceptanceRow, VerificationBasisHeadRow,
    VerificationReservationRow, VerificationScenarioStateRow,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWorkFactory
from okto_pulse.community.adapters.sqlalchemy_verification_currentness import (
    CommunityVerificationCurrentnessStore, CommunityVerificationReservationPort,
)
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityHttpManifestExecutor,
    CommunityTestEvidenceExecutionIssuer, CommunityTestEvidenceWriteVerifier,
    CommunityTestVerificationReportIssuer,
)
from okto_pulse.community.api.specs import router
from test_evidence_v2_adapter import _manifest
from verification_report_fixtures import report


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,reset_during_issuance", [
    ("execute", False), ("reports", False), ("reports", True),
])
async def test_real_rest_issuance_releases_writer_before_independent_reservation(
    tmp_path, operation, reset_during_issuance,
):
    engine = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'issuance.sqlite'}")
    install_community_sqlite_pragmas(engine)
    # Bound the isolated regression's lock wait; transaction/gate behavior is
    # unchanged. Production's 30-second timeout must not slow a known deadlock.
    @event.listens_for(engine.sync_engine, "connect")
    def bounded_lock_wait(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout=100")
        cursor.close()

    sessions = build_community_session_factory(engine)
    sql = []
    database_errors = []
    calls = []
    external_observations = []
    checked_out = {}
    phase = "issuance"
    prior_count = int(reset_during_issuance)
    prior_acceptance_id = None
    try:
        await initialize_current_schema(engine, current_schema_contract())
        configure_auth(LocalAuthProvider())
        register_relational_application_adapter(CommunityRelationalApplicationAdapter())
        method = "automated_test" if operation == "execute" else "inspection"
        scenario = dict(id="ts", title="Observe isolated runtime", status="ready",
            scenario_type="integration", verification_method=method,
            given="The isolated subject is available", when="Its health is observed",
            then="Version is 0.4.3", linked_criteria=["ac-1"])
        criteria = [dict(id="ac-1", text="Version is 0.4.3")]
        async with sessions() as session:
            session.add(Board(id="board", name="Issuance regression", owner_id="local-user", realm_id="local"))
            await session.flush()
            session.add(Spec(id="spec", board_id="board", title="Isolated issuance",
                created_by="local-user", status="draft", edition=1,
                architecture_adoption={}, test_scenarios=[scenario], acceptance_criteria=criteria))
            await session.flush()
            # Seal the authored fixture through the same native authority as
            # normal scenario creation, so publication exercises real gates.
            for kind, identity in [(PolicyEntityType.SPEC, "spec"), (PolicyEntityType.TEST_SCENARIO, "ts")]:
                await CommunitySqlAlchemySemanticGuidelineAssessment(session).record_semantic_subject_mutation(
                    board_id="board", entity_type=kind, subject_id=identity, actor_id="local-user",
                    idempotency_key="issuance-fixture-" + identity,
                    request_digest=canonical_sha256({"scenario": scenario, "criteria": criteria}),
                    changed_at=datetime.now(timezone.utc))
            await session.commit()

        app = FastAPI()
        app.state.runtime_composition = SimpleNamespace(uow_factory=CommunityUnitOfWorkFactory(sessions))
        app.include_router(router, prefix="/api/v1")

        @app.get("/health")
        async def health():
            calls.append("health")
            await observe_external_stage()
            return {"version": "0.4.3"}

        ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
        ledger.manifest_root.mkdir(parents=True)
        digest = compute_test_scenario_semantic_sha256(board_id="board", spec_id="spec",
            scenario=scenario, acceptance_criteria=criteria)
        manifest = _manifest(expected="0.4.3", board_id="board", spec_id="spec",
            scenario_id="ts", scenario_sha256=digest)
        (ledger.manifest_root / "health.json").write_text(json.dumps(manifest), encoding="utf-8")
        register_verification_reservation_port(CommunityVerificationReservationPort(sessions))
        register_verification_currentness_store_factory(CommunityVerificationCurrentnessStore)
        register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=ledger))

        body = {"status": "passed", "manifest_ref": "health.json"}
        if operation == "reports":
            value = report("inspection")
            value["observed_at"] = datetime.now(timezone.utc).isoformat()
            body = {"report": value}
        if reset_during_issuance:
            # A ready -> ready PATCH is intentionally a no-op. Establish an
            # actual passed result through public routes before exercising reset.
            register_test_verification_report_issuer(CommunityTestVerificationReportIssuer(ledger=ledger))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                initial = await client.post("/api/v1/specs/spec/scenarios/ts/evidence/reports", json=body)
                assert initial.status_code == 200, initial.text
                initial_publication = await client.patch("/api/v1/specs/spec/scenarios/ts/status",
                    json={"status": "passed", "evidence": initial.json()["evidence"]})
                assert initial_publication.status_code == 200, initial_publication.text
                prior_acceptance_id = initial_publication.json()["result"]["accepted_result_id"]

        class ObservedReportIssuer(CommunityTestVerificationReportIssuer):
            async def admit(self, request):
                await observe_external_stage()
                return await super().admit(request)

        register_test_verification_report_issuer(ObservedReportIssuer(ledger=ledger))
        register_test_evidence_execution_issuer(CommunityTestEvidenceExecutionIssuer(
            ledger=ledger, executor=CommunityHttpManifestExecutor(base_url="http://127.0.0.1:8100",
                transport=httpx.ASGITransport(app=app))))

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def capture_sql(connection, _cursor, statement, _parameters, _context, _many):
            if statement.strip().upper().startswith("BEGIN"):
                sql.append({"connection": id(connection), "statement": statement, "phase": phase})

        @event.listens_for(engine.sync_engine, "checkout")
        def capture_checkout(_connection, record, _proxy):
            checked_out[id(record)] = record.driver_connection

        @event.listens_for(engine.sync_engine, "checkin")
        def capture_checkin(_connection, record):
            checked_out.pop(id(record), None)

        @event.listens_for(engine.sync_engine, "handle_error")
        def capture_error(context):
            database_errors.append({"statement": context.statement,
                "error": str(context.original_exception),
                "traceback": "".join(traceback.format_exception(context.original_exception))})

        async def observe_external_stage():
            nonlocal phase
            # Observe real SQLite transactions, not SQLAlchemy's logical
            # autobegin. No read snapshot/writer may survive into external work.
            active = [key for key, driver in checked_out.items() if driver.in_transaction]
            assert active == []
            phase = "external_probe"
            async with sessions() as observer:
                await observer.execute(text("BEGIN IMMEDIATE"))
                reserved = list(await observer.scalars(select(VerificationReservationRow).order_by(VerificationReservationRow.ordinal)))
                assert len(reserved) == 1 + prior_count  # Visible from another connection: committed.
                assert reserved[-1].actor_id == "local-user"
                assert reserved[-1].actor_kind == "human"
                assert reserved[-1].ordinal == 1 + prior_count
                assert await observer.scalar(select(func.count()).select_from(VerificationAcceptanceRow)) == prior_count
                assert len(list(ledger.receipt_root.glob("*.json"))) == prior_count
                await observer.execute(update(Board).where(Board.id == "board").values(name="Observed externally"))
                await observer.commit()
                external_observations.append({"active_transactions": active,
                    "committed_reservation": reserved[-1].id, "ordinal": reserved[-1].ordinal})
            if reset_during_issuance:
                phase = "concurrent_reset"
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as reset_client:
                    reset = await reset_client.patch("/api/v1/specs/spec/scenarios/ts/status", json={"status": "ready"})
                assert reset.status_code == 200, reset.text
            phase = "issuance"

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/v1/specs/spec/scenarios/ts/evidence/{operation}", json=body)

        async with sessions() as observer:
            reservations = list(await observer.scalars(select(VerificationReservationRow).order_by(VerificationReservationRow.ordinal)))
            accepted = await observer.scalar(select(func.count()).select_from(VerificationAcceptanceRow))
            persisted = await observer.get(Spec, "spec")
            diagnostic = {"operation": operation, "status": response.status_code,
                "response": response.json(), "begins": sql, "database_errors": database_errors,
                "subject_calls": calls, "reservations": len(reservations), "acceptances": accepted,
                "external_observations": external_observations,
                "scenario_status": persisted.test_scenarios[0]["status"],
                "receipt_count": len(list(ledger.receipt_root.glob("*.json")))}
            (tmp_path / "issuance-observation.json").write_text(
                json.dumps(diagnostic, indent=2), encoding="utf-8")
            assert response.status_code == 200, json.dumps(diagnostic, indent=2)
            evidence = response.json()["evidence"]
            assert evidence["execution_receipt"].startswith("ev2r.")
            assert len(reservations) == 1 + prior_count
            assert reservations[-1].actor_id == "local-user" and reservations[-1].actor_kind == "human"
            assert evidence["verification_context"]["reservation_id"] == reservations[-1].id
            assert accepted == prior_count and persisted.test_scenarios[0]["status"] == "ready"
            assert diagnostic["receipt_count"] == 1 + prior_count
            assert calls == (["health"] if operation == "execute" else [])
            assert len(external_observations) == 1
            assert len([item for item in sql if item["phase"] == "issuance"]) == 1
            assert not database_errors

        if operation == "execute":
            # This regression stops at real replay issuance. Publishing an HTTP
            # manifest requires the separate execution-basis gap to be resolved.
            return

        phase = "publication"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            published = await client.patch("/api/v1/specs/spec/scenarios/ts/status",
                json={"status": "passed", "evidence": evidence})
        assert len([item for item in sql if item["phase"] == "publication"
                    and item["statement"] == "BEGIN IMMEDIATE"]) == 1
        async with sessions() as observer:
            accepted = list(await observer.scalars(select(VerificationAcceptanceRow)))
            persisted = await observer.get(Spec, "spec")
            if reset_during_issuance:
                assert published.status_code == 422, published.text
                assert "verification_context_stale" in published.text
                assert [item.id for item in accepted] == [prior_acceptance_id]
                assert persisted.test_scenarios[0]["status"] == "ready"
                current = await observer.scalar(select(VerificationScenarioStateRow))
                assert current.generation > evidence["verification_context"]["generation"]
                assert await observer.scalar(select(func.count()).select_from(VerificationBasisHeadRow)
                    .where(VerificationBasisHeadRow.generation == current.generation)) == 0
                assert await observer.scalar(select(func.count()).select_from(VerificationBasisHeadRow)) == prior_count
            else:
                assert published.status_code == 200, published.text
                assert len(accepted) == 1
                assert published.json()["result"]["accepted_result_id"] == accepted[0].id
                assert published.json()["result"]["verification_current"] is True
                assert persisted.test_scenarios[0]["status"] == "passed"
                assert persisted.test_scenarios[0]["evidence"] == evidence
        assert len(external_observations) == 1  # Publication never re-executes issuance.
        diagnostic.update(reset_during_issuance=reset_during_issuance,
            publication={"status": published.status_code, "response": published.json()},
            final_acceptances=len(accepted), final_scenario_status=persisted.test_scenarios[0]["status"])
        (tmp_path / "issuance-observation.json").write_text(
            json.dumps(diagnostic, indent=2), encoding="utf-8")
    finally:
        await engine.dispose()
