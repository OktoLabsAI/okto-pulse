"""Real issuer/ledger checks in isolated storage; no live scenario is published."""

from copy import deepcopy
from dataclasses import replace
import json
import subprocess

from fastapi import FastAPI
import httpx
import pytest

from okto_pulse.community.adapters.external_test_runner import profile_root, register_profile
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger,
    CommunityHttpManifestExecutor,
    CommunityTestEvidenceError,
    CommunityTestEvidenceExecutionIssuer,
    CommunityTestEvidenceWriteVerifier,
    CommunityTestVerificationReportIssuer,
)
from okto_pulse.core.domain.verification_currentness import VerificationContext
from okto_pulse.core.models.schemas import TestScenarioEvidence as Evidence
from okto_pulse.core.ports.test_evidence import (
    TestEvidenceExecutionRequest as ExecutionRequest,
    TestVerificationReportRequest as ReportRequest,
)
from verification_report_fixtures import report


def context(method="automated_test", **changes):
    return VerificationContext.model_validate({
        "board_id": "board", "spec_id": "spec", "spec_edition": 2,
        "scenario_id": "scenario", "reservation_id": "reservation-1",
        "scenario_sha256": "a" * 64, "verification_method": method,
        "generation": 3, "ordinal": 4, **changes,
    })


def request(method="automated_test"):
    common = dict(board_id="board", spec_id="spec", scenario_id="scenario",
                  scenario_sha256="sha256:" + "a" * 64, actor_id="actor",
                  verification_context=context(method))
    if method != "automated_test":
        return ReportRequest(**common, report=report(method))
    return ExecutionRequest(**common, status="passed", manifest_ref=None,
        inline_replay={"steps": [{"name": "health", "path": "/health", "expected_status": 200,
            "assertions": [{"name": "version", "kind": "json_equals", "path": "version", "expected": "0.4.3"}]}]})


def verify(ledger, req, evidence, **changes):
    args = dict(board_id=req.board_id, spec_id=req.spec_id, scenario_id=req.scenario_id,
                scenario_sha256=req.scenario_sha256, actor_id=req.actor_id,
                status="passed", evidence=evidence)
    return CommunityTestEvidenceWriteVerifier(ledger=ledger).verify(**(args | changes))


@pytest.fixture
def runtime(tmp_path):
    app = FastAPI()
    calls = []

    @app.get("/health")
    async def health():
        calls.append("health")
        return {"version": "0.4.3"}

    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    executor = CommunityHttpManifestExecutor(base_url="http://127.0.0.1:8100",
        transport=httpx.ASGITransport(app=app))
    return ledger, CommunityTestEvidenceExecutionIssuer(ledger=ledger, executor=executor), calls


async def issue(runtime, method, req=None):
    ledger, issuer, _ = runtime
    req = req or request(method)
    result = await (issuer.execute(req) if method == "automated_test"
                    else CommunityTestVerificationReportIssuer(ledger=ledger).admit(req))
    return req, result.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["automated_test", "inspection", "static_analysis", "demonstration"])
async def test_real_ledger_authenticates_reserved_context_after_transport_roundtrip(runtime, method):
    ledger, _, calls = runtime
    req, evidence = await issue(runtime, method)
    transported = Evidence.model_validate(evidence).model_dump(mode="json", exclude_none=True)
    assert transported == evidence
    assert verify(ledger, req, transported).verified
    assert verify(ledger, req, transported).verified  # Read-only replay does not reexecute.
    assert transported["verification_context"] == req.verification_context.model_dump(mode="json")
    assert calls == (["health"] if method == "automated_test" else [])
    assert len(list(ledger.receipt_root.glob("*.json"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["automated_test", "inspection"])
async def test_context_tampering_and_scope_replay_fail_authentication(runtime, method):
    ledger, _, _ = runtime
    req, evidence = await issue(runtime, method)
    for field, value in {"generation": 4, "ordinal": 5, "reservation_id": "another",
                         "spec_edition": 3, "scenario_sha256": "b" * 64,
                         "verification_method": "demonstration", "board_id": "another",
                         "spec_id": "another", "scenario_id": "another"}.items():
        changed = deepcopy(evidence)
        changed["verification_context"][field] = value
        assert not verify(ledger, req, changed).verified, field
    changed = deepcopy(evidence)
    del changed["verification_context"]
    assert not verify(ledger, req, changed).verified
    for field in ("board_id", "spec_id", "scenario_id", "actor_id"):
        assert not verify(ledger, req, evidence, **{field: "another"}).verified


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["automated_test", "inspection"])
@pytest.mark.parametrize("change", [None, {"board_id": "another"}, {"spec_id": "another"},
                                    {"scenario_id": "another"}, {"scenario_sha256": "b" * 64},
                                    {"verification_method": "demonstration"}])
async def test_missing_or_inconsistent_reservation_never_executes_or_signs(runtime, method, change):
    ledger, _, calls = runtime
    req = replace(request(method), verification_context=None if change is None else context(method, **change))
    with pytest.raises(CommunityTestEvidenceError, match="verification_reservation_invalid"):
        await issue(runtime, method, req)
    assert calls == []
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_runtime_failure_is_not_reexecuted_or_signed(tmp_path):
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    calls = []

    async def fail_once(*args):
        calls.append("execution")
        raise RuntimeError("runtime failed")

    issuer = CommunityTestEvidenceExecutionIssuer(ledger=ledger, executor=fail_once)
    with pytest.raises((RuntimeError, CommunityTestEvidenceError)):
        await issuer.execute(request())
    assert calls == ["execution"]
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_registered_external_runner_preserves_server_context(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "test_example.py").write_text(
        "import unittest\nclass Cases(unittest.TestCase):\n    def test_pass(self): self.assertEqual(2 + 2, 4)\n",
        encoding="utf-8")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()

    git("init")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "isolated test fixture")
    revision = git("rev-parse", "HEAD")
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    register_profile(root=profile_root(ledger), name="example", workspace=str(root), source_ref="source-example",
        board_id="board", spec_id="spec", scenario_id="scenario", test_ids=["test_example.py::Cases.test_pass"],
        timeout_seconds=60)

    async def no_http(*args):
        pytest.fail("An external runner must not execute HTTP")

    req = replace(request(), inline_replay={"runner_ref": "example", "revision": revision})
    issuer = CommunityTestEvidenceExecutionIssuer(ledger=ledger, executor=no_http)
    result = await issuer.execute(req)
    transported = Evidence.model_validate(result.evidence).model_dump(mode="json", exclude_none=True)
    assert verify(ledger, req, transported).verified
    assert transported["verification_context"] == req.verification_context.model_dump(mode="json")
    assert transported["execution_attestation"]["execution_basis"]["revision"] == revision
    assert transported["execution_attestation"]["assertions"][0]["observed"]["tests_run"] == 1
    manifest = json.loads((ledger.manifest_root / transported["manifest_ref"]).read_text())
    assert "verification_context" not in manifest  # Server context is not client-controlled runner input.
