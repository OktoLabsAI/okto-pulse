"""Issuance must reserve before execution without retaining a REST writer.

Use the real REST dependency, Community UoW, local authentication and reservation
provider over a file-backed native schema. No dependency overrides are installed.
"""
from datetime import datetime, timezone
import json
import subprocess
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
from okto_pulse.community.adapters.external_test_runner import profile_root, register_profile
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


def _registered_git_runner(tmp_path, ledger):
    """Real host registration and immutable project, confined to this fixture."""
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "executions.txt"
    (project / "labels.py").write_text(
        "def normalize_labels(values):\n"
        "    return list(dict.fromkeys(value.strip() for value in values if value.strip()))\n",
        encoding="utf-8",
    )
    (project / "test_labels.py").write_text(
        "from pathlib import Path\nimport unittest\nfrom labels import normalize_labels\n"
        "class LabelsTests(unittest.TestCase):\n"
        "    def test_normalization(self):\n"
        f"        with Path({str(marker)!r}).open('a', encoding='utf-8') as out:\n"
        "            out.write('executed\\n')\n"
        "        self.assertEqual(normalize_labels([' a ', '', 'b', 'a']), ['a', 'b'])\n",
        encoding="utf-8",
    )

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(project), *args], stderr=subprocess.PIPE, timeout=20,
        ).decode().strip()

    git("init")
    git("add", ".")
    git("-c", "user.name=Issuance Test", "-c", "user.email=issuance@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", "Isolated registered runner fixture")
    revision = git("rev-parse", "HEAD")
    source_ref = project.as_uri()
    test_ids = ["test_labels.py::LabelsTests.test_normalization"]
    register_profile(root=profile_root(ledger), name="labels", workspace=str(project),
        source_ref=source_ref, board_id="board", spec_id="spec", scenario_id="ts", test_ids=test_ids)
    register_profile(root=profile_root(ledger), name="foreign", workspace=str(project),
        source_ref=source_ref, board_id="another-board", spec_id="spec", scenario_id="ts", test_ids=test_ids)
    # If the endpoint runs the working tree instead of the selected commit,
    # the subprocess will fail rather than produce an authenticated pass.
    (project / "labels.py").write_text("raise RuntimeError('uncommitted code must not run')\n", encoding="utf-8")
    return {"runner_ref": "labels", "revision": revision}, {
        "runner_ref": "labels", "revision": revision, "source_ref": source_ref, "test_ids": test_ids,
    }, marker


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,reset_during_issuance", [
    ("execute", False), ("reports", False), ("reports", True), ("runner", False),
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
        method = "inspection" if operation == "reports" else "automated_test"
        scenario = dict(id="ts", title="Observe isolated runtime", status="ready",
            scenario_type="integration", verification_method=method,
            given="The isolated subject is available", when="Its health is observed",
            then="Version is 0.4.3", linked_criteria=["ac-1"])
        criteria = [dict(id="ac-1", text="Version is 0.4.3")]
        if operation == "runner":
            scenario.update(title="Registered immutable label normalizer",
                given="The registered project contains the label normalizer",
                when="Its exact committed test is executed",
                then="Empty and duplicate labels are removed in order")
            criteria = [dict(id="ac-1", text=scenario["then"])]
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
        if operation == "runner":
            selector, expected_basis, marker = _registered_git_runner(tmp_path, ledger)
            body = {"status": "passed", **selector}
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

        class ObservedExecutionIssuer(CommunityTestEvidenceExecutionIssuer):
            async def execute(self, request):
                if operation == "runner" and request.inline_replay == selector:
                    await observe_external_stage()
                return await super().execute(request)

        register_test_evidence_execution_issuer(ObservedExecutionIssuer(
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
            if operation == "runner":
                invalid_selectors = [
                    {}, {"runner_ref": "labels"}, {"revision": selector["revision"]},
                    {"manifest_ref": "health.json", **selector},
                    {"manifest_ref": "health.json", "runner_ref": "labels"},
                    {"manifest_ref": "health.json", "revision": selector["revision"]},
                    {"manifest_ref": " "}, {**selector, "runner_ref": "../labels"},
                    {**selector, "runner_ref": 123}, {**selector, "revision": "HEAD"},
                    {**selector, "revision": 123}, {**selector, "revision": "A" * 40},
                    {**selector, "revision": "a" * 41},
                    *[{**selector, field: value} for field, value in (
                        ("command", "arbitrary-code"), ("source_ref", "caller-source"),
                        ("test_ids", ["caller-test"]), ("actor_id", "caller-actor"),
                        ("execution_basis", expected_basis), ("replay", selector),
                    )],
                ]
                for invalid in invalid_selectors:
                    denied = await client.post("/api/v1/specs/spec/scenarios/ts/evidence/execute",
                        json={"status": "passed", **invalid})
                    assert denied.status_code == 422, (invalid, denied.text)
                async with sessions() as observer:
                    assert await observer.scalar(select(func.count()).select_from(VerificationReservationRow)) == 0
                assert not marker.exists() and external_observations == []
                assert not list(ledger.receipt_root.glob("*.json"))
            route_operation = "execute" if operation == "runner" else operation
            response = await client.post(f"/api/v1/specs/spec/scenarios/ts/evidence/{route_operation}", json=body)

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
            if operation == "runner":
                assert evidence["execution_attestation"]["execution_basis"] == expected_basis
                observed_run = evidence["execution_attestation"]["assertions"][0]["observed"]
                assert observed_run == {"tests_run": 1, "failures": 0, "errors": 0,
                    "skipped": 0, "expected_failures": 0, "unexpected_successes": 0, "exit_code": 0}
                assert marker.read_text(encoding="utf-8").splitlines() == ["executed"]

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
        if operation == "runner":
            assert marker.read_text(encoding="utf-8").splitlines() == ["executed"]
            # Structurally valid requests still pass through native profile
            # admission. They may reserve an ordinal, but cannot run or publish.
            phase = "profile_denial"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for name, reason in [("missing", "runner_not_registered_use_local_cli"), ("foreign", "profile_scope_mismatch")]:
                    denied = await client.post("/api/v1/specs/spec/scenarios/ts/evidence/execute",
                        json={"status": "passed", "runner_ref": name, "revision": selector["revision"]})
                    assert denied.status_code == 422, denied.text
                    assert reason in denied.text
            assert marker.read_text(encoding="utf-8").splitlines() == ["executed"]
            assert len(list(ledger.receipt_root.glob("*.json"))) == 1
            async with sessions() as observer:
                assert await observer.scalar(select(func.count()).select_from(VerificationReservationRow)) == 3
                assert await observer.scalar(select(func.count()).select_from(VerificationAcceptanceRow)) == 1
                saved = await observer.get(Spec, "spec")
                assert saved.test_scenarios[0]["evidence"] == evidence
            diagnostic.update(basis=expected_basis, rejected_selectors=len(invalid_selectors),
                rejected_profiles=2, actual_test_executions=1, final_reservations=3)
        diagnostic.update(reset_during_issuance=reset_during_issuance,
            publication={"status": published.status_code, "response": published.json()},
            final_acceptances=len(accepted), final_scenario_status=persisted.test_scenarios[0]["status"])
        (tmp_path / "issuance-observation.json").write_text(
            json.dumps(diagnostic, indent=2), encoding="utf-8")
    finally:
        await engine.dispose()
