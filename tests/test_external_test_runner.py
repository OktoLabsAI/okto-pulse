"""Real external subprocess tests; receipts cannot be supplied by the caller."""

from copy import deepcopy
import json
import subprocess

import pytest

from okto_pulse.community.adapters.external_test_runner import register_profile, profile_root
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestEvidenceExecutionIssuer,
    CommunityTestEvidenceWriteVerifier, CommunityTestEvidenceError,
)
from okto_pulse.core.models.schemas import TestScenarioEvidence as Evidence
from okto_pulse.core.ports.test_evidence import TestEvidenceExecutionRequest as Request


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "test_example.py").write_text('''import unittest
from pydantic import BaseModel
import time
class Cases(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(2 + 2, 4)
        self.assertIsNotNone(BaseModel.model_config)
    def test_fail(self): self.assertEqual(2 + 2, 5)
    @unittest.skip("not implemented")
    def test_skip(self): pass
    def test_timeout(self): time.sleep(10)
''', encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "test fixture")
    return root, git("rev-parse", "HEAD")


def setup(tmp_path, project, test="test_pass", timeout=60):
    root, revision = project
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    register_profile(root=profile_root(ledger), name="example", workspace=str(root), source_ref="source-example",
        board_id="board", spec_id="spec", scenario_id="scenario", test_ids=["test_example.py::Cases." + test],
        timeout_seconds=timeout)
    def no_http(*args):
        pytest.fail("external tests must not execute HTTP against Pulse")
    issuer = CommunityTestEvidenceExecutionIssuer(ledger=ledger, executor=no_http)
    request = Request(board_id="board", spec_id="spec", scenario_id="scenario", status="passed",
        manifest_ref=None, actor_id="agent", scenario_sha256="sha256:" + "a" * 64,
        inline_replay={"runner_ref": "example", "revision": revision})
    return ledger, issuer, request


def verify(ledger, request, evidence):
    return CommunityTestEvidenceWriteVerifier(ledger=ledger).verify(board_id=request.board_id,
        spec_id=request.spec_id, scenario_id=request.scenario_id, status=request.status,
        scenario_sha256=request.scenario_sha256, actor_id=request.actor_id, evidence=evidence)


@pytest.mark.asyncio
async def test_real_external_run_receipt_and_typed_roundtrip(tmp_path, project):
    ledger, issuer, request = setup(tmp_path, project)
    # Uncommitted local failures are not mixed into an immutable revision.
    (project[0] / "test_example.py").write_text("raise RuntimeError('dirty workspace')")
    result = await issuer.execute(request)
    evidence = Evidence.model_validate(result.evidence).model_dump(exclude_none=True)
    assert verify(ledger, request, evidence).verified
    basis = evidence["execution_attestation"]["execution_basis"]
    assert basis["revision"] == project[1]
    assert basis["source_ref"] == "source-example"
    assert evidence["execution_attestation"]["assertions"][0]["observed"]["tests_run"] == 1
    tampered = deepcopy(evidence)
    tampered["execution_attestation"]["execution_basis"]["revision"] = "b" * 40
    assert not verify(ledger, request, tampered).verified
    manifest_path = ledger.manifest_root / evidence["manifest_ref"]
    manifest = json.loads(manifest_path.read_text())
    manifest["execution_basis"] = {"revision": "invalid"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert not verify(ledger, request, evidence).verified


@pytest.mark.asyncio
@pytest.mark.parametrize("test,reason", [("test_skip", "incomplete_run"), ("missing", "runner_setup_failed"),
                                      ("test_fail", "invalid_execution_observation"), ("test_timeout", "timeout_no_receipt")])
async def test_nonpassing_never_receives_passed_receipt(tmp_path, project, test, reason):
    ledger, issuer, request = setup(tmp_path, project, test, timeout=1)
    with pytest.raises(CommunityTestEvidenceError, match=reason):
        await issuer.execute(request)
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_failed_execution_can_be_recorded_as_failed(tmp_path, project):
    from dataclasses import replace
    ledger, issuer, request = setup(tmp_path, project, "test_fail")
    request = replace(request, status="failed")
    result = await issuer.execute(request)
    assert verify(ledger, request, result.evidence).verified
    assert result.evidence["execution_attestation"]["outcome"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"runner_ref": "../example"}, {"revision": "HEAD"},
                                   {"command": "echo PASS"}, {"runner_ref": "unknown"}])
async def test_caller_cannot_supply_code_or_paths(tmp_path, project, change):
    from dataclasses import replace
    ledger, issuer, request = setup(tmp_path, project)
    request = replace(request, inline_replay={**request.inline_replay, **change})
    with pytest.raises(CommunityTestEvidenceError):
        await issuer.execute(request)
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_profile_cannot_be_used_by_another_scope(tmp_path, project):
    from dataclasses import replace
    ledger, issuer, request = setup(tmp_path, project)
    with pytest.raises(CommunityTestEvidenceError, match="profile_scope_mismatch"):
        await issuer.execute(replace(request, board_id="other"))
    assert not ledger.receipt_root.exists()


def test_local_registration_does_not_overwrite_authority(tmp_path, project):
    setup(tmp_path, project)
    with pytest.raises(FileExistsError):
        setup(tmp_path, project)


@pytest.mark.asyncio
async def test_missing_method_cannot_be_authenticated_as_a_failing_test(tmp_path, project):
    from dataclasses import replace
    ledger, issuer, request = setup(tmp_path, project, "missing")
    with pytest.raises(CommunityTestEvidenceError, match="runner_setup_failed"):
        await issuer.execute(replace(request, status="failed"))
    assert not ledger.receipt_root.exists()
