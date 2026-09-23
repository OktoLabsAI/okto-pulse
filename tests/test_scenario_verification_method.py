"""Method writes through HTTP/MCP and authenticated method binding through the real adapter."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import update

from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.community.adapters.test_evidence import (
    CommunityTestEvidenceExecutionIssuer,
    CommunityTestEvidenceWriteVerifier,
    CommunityHttpManifestExecutor,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.permissions import ALL_FLAGS
from okto_pulse.core.ports.permission_policy import PermissionSet, set_permission_flag
from okto_pulse.core.ports.test_evidence import (
    TestEvidenceExecutionRequest as ExecutionRequest,
    register_test_evidence_write_verifier,
)
from okto_pulse.core.services.test_scenario_lifecycle import (
    compute_test_scenario_semantic_sha256,
    scenario_has_authenticated_required_evidence,
)
from okto_pulse.core.mcp import server

import test_architecture_candidates_integration as sources
import test_architecture_classification_use_case as writes
import test_architecture_classification_transports as transports
import test_evidence_v2_adapter as adapter_tests

adopted_context = sources.adopted_context
classified_context = writes.classified_context


def actor(*, deny=False, board_id="board"):
    flags = {}
    for flag in ALL_FLAGS:
        set_permission_flag(
            flags,
            flag,
            flag
            in {
                "spec.entity.read",
                "spec.tests.read",
                "spec.tests.execute",
                "spec.tests.update_status",
                "spec.tests.edit",
                "spec.tests.create",
                "spec.interact_in.draft",
                "test_scenario.interact_in.ready",
                "test_scenario.move.ready_to_passed",
            }
            and not (deny and flag == "spec.tests.edit"),
        )
    return ActorContext(
        "author",
        "rest",
        board_id=board_id,
        actor_kind="human",
        permissions=PermissionSet(flags),
    )


async def setup(db, monkeypatch, *, method=None):
    scenario = {
        "id": "ts",
        "title": "Observe version",
        "scenario_type": "manual",
        "status": "ready",
        "given": "Runtime available",
        "when": "Health queried",
        "then": "Version is 0.3.0",
        "linked_criteria": [],
        **({"verification_method": method} if method is not None else {}),
    }
    await db.execute(
        update(Spec)
        .where(Spec.id == "spec")
        .values(test_scenarios=[scenario], acceptance_criteria=[], version=1)
    )
    await db.commit()
    app, factory = transports.application(db)
    transports.mcp_factory(monkeypatch, factory)
    monkeypatch.setattr(
        server,
        "_get_agent_ctx",
        AsyncMock(
            return_value=SimpleNamespace(
                agent_id="author", agent_name="Author", permissions=actor().permissions
            )
        ),
    )
    monkeypatch.setattr(
        RESTAdapterContract, "actor", staticmethod(lambda *args, **kwargs: actor())
    )
    return app, scenario


@pytest.mark.asyncio
async def test_rest_mcp_same_body_writer_version_conflict_and_closed_method(
    classified_context, monkeypatch
):
    db = classified_context
    app, _ = await setup(db, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/api/v1/boards/board/specs/spec/scenarios/ts/verification-method"
        first = await client.patch(
            path, json={"verification_method": "inspection", "expected_spec_version": 1}
        )
        assert first.status_code == 200, first.text
        assert first.json()["scenario"]["scenario_type"] == "manual"
        assert first.json()["scenario"]["verification_method"] == "inspection"
        stale = await client.patch(
            path,
            json={"verification_method": "automated_test", "expected_spec_version": 1},
        )
        assert stale.status_code == 409
        invalid = await client.patch(
            path, json={"verification_method": "passing", "expected_spec_version": 2}
        )
        assert invalid.status_code == 422
        denied_extra = await client.patch(
            path,
            json={
                "verification_method": "automated_test",
                "expected_spec_version": 2,
                "trusted": True,
            },
        )
        assert denied_extra.status_code == 422
        native = json.loads(
            await server.okto_pulse_update_test_scenario.fn(
                board_id="board",
                spec_id="spec",
                scenario_id="ts",
                verification_method="automated_test",
                expected_spec_version=2,
            )
        )
        assert native.get("error") is None, native
        persisted = await db.get(Spec, "spec", populate_existing=True)
        assert persisted.test_scenarios[0]["verification_method"] == "automated_test"


@pytest.mark.asyncio
async def test_rest_edit_authority_and_board_scope_are_preserved(
    classified_context, monkeypatch
):
    app, _ = await setup(classified_context, monkeypatch)
    monkeypatch.setattr(
        RESTAdapterContract,
        "actor",
        staticmethod(lambda *args, **kwargs: actor(deny=True)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/api/v1/boards/board/specs/spec/scenarios/ts/verification-method",
            json={"verification_method": "automated_test", "expected_spec_version": 1},
        )
        assert response.status_code == 403
        monkeypatch.setattr(
            RESTAdapterContract,
            "actor",
            staticmethod(lambda *args, **kwargs: actor(board_id="other-board")),
        )
        foreign = await client.patch(
            "/api/v1/boards/other-board/specs/spec/scenarios/ts/verification-method",
            json={"verification_method": "automated_test", "expected_spec_version": 1},
        )
        assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_rest_method_edit_cannot_change_non_draft_content(
    classified_context, monkeypatch
):
    app, _ = await setup(classified_context, monkeypatch)
    await classified_context.execute(
        update(Spec).where(Spec.id == "spec").values(status="in_progress")
    )
    await classified_context.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            "/api/v1/boards/board/specs/spec/scenarios/ts/verification-method",
            json={"verification_method": "automated_test", "expected_spec_version": 1},
        )
        assert response.status_code == 409, response.text
    persisted = await classified_context.get(Spec, "spec", populate_existing=True)
    assert persisted.test_scenarios[0].get("verification_method") is None


@pytest.mark.asyncio
async def test_real_http_replay_receipt_binds_method_and_rejects_reinterpretation(
    classified_context, monkeypatch, tmp_path
):
    app, scenario = await setup(
        classified_context, monkeypatch, method="automated_test"
    )

    @app.get("/health")
    async def health():
        return {"version": "0.3.0"}

    ledger = adapter_tests._ledger(tmp_path)
    verifier = CommunityTestEvidenceWriteVerifier(ledger=ledger)
    register_test_evidence_write_verifier(verifier)
    digest = compute_test_scenario_semantic_sha256(
        board_id="board", spec_id="spec", scenario=scenario, acceptance_criteria=[]
    )
    manifest = adapter_tests._manifest(
        board_id="board", spec_id="spec", scenario_id="ts", scenario_sha256=digest
    )
    (ledger.manifest_root / "method.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    issuer = CommunityTestEvidenceExecutionIssuer(
        ledger=ledger,
        executor=CommunityHttpManifestExecutor(
            base_url="http://127.0.0.1", transport=httpx.ASGITransport(app=app)
        ),
        environment="method-test",
    )
    result = await issuer.execute(
        ExecutionRequest(
            board_id="board",
            spec_id="spec",
            scenario_id="ts",
            status="passed",
            manifest_ref="method.json",
            actor_id="author",
            scenario_sha256=digest,
        )
    )
    evidence = dict(result.evidence)
    assert verifier.verification_methods == frozenset({"automated_test", "static_analysis", "inspection", "demonstration"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        passed = await client.patch(
            "/api/v1/specs/spec/scenarios/ts/status",
            json={"status": "passed", "evidence": evidence},
        )
        assert passed.status_code == 200, passed.text
    observed = {**scenario, "status": "passed", "evidence": evidence}
    assert scenario_has_authenticated_required_evidence(
        board_id="board", spec_id="spec", scenario=observed, acceptance_criteria=[]
    )
    for method in (None, "static_analysis", "inspection", "demonstration"):
        assert not scenario_has_authenticated_required_evidence(
            board_id="board",
            spec_id="spec",
            scenario={**observed, "verification_method": method},
            acceptance_criteria=[],
        )
    tampered = {
        **evidence,
        "execution_receipt": evidence["execution_receipt"][:-1]
        + ("0" if evidence["execution_receipt"][-1] != "0" else "1"),
    }
    assert not scenario_has_authenticated_required_evidence(
        board_id="board",
        spec_id="spec",
        scenario={**observed, "evidence": tampered},
        acceptance_criteria=[],
    )
