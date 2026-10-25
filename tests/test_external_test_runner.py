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


@pytest.mark.parametrize("timeout", [121, 900, 3600])
def test_long_timeout_requires_explicit_local_operator_authority(tmp_path, project, timeout):
    from okto_pulse.community.adapters.external_test_runner import _validate_profile
    root, _ = project
    arguments = dict(root=tmp_path / "profiles", name="long", workspace=str(root),
                     source_ref="source", board_id="board", spec_id="spec", scenario_id="scenario",
                     test_ids=["test_example.py::Cases.test_pass"], timeout_seconds=timeout)
    with pytest.raises(CommunityTestEvidenceError, match="timeout_invalid"):
        register_profile(**arguments)
    path = register_profile(**arguments, allow_long_running=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["timeout_authorization"] == "local-long-running/v1"
    _validate_profile(data)
    for forged in (None, True, "remote", "local-long-running/v2"):
        with pytest.raises(CommunityTestEvidenceError, match="timeout_authorization_invalid"):
            _validate_profile({**data, "timeout_authorization": forged})
    del data["timeout_authorization"]
    with pytest.raises(CommunityTestEvidenceError, match="timeout_invalid"):
        _validate_profile(data)


@pytest.mark.parametrize("timeout", [0, True, 3601])
def test_long_timeout_remains_bounded(tmp_path, project, timeout):
    with pytest.raises(CommunityTestEvidenceError, match="timeout_invalid"):
        register_profile(root=tmp_path / "profiles", name="long", workspace=str(project[0]),
                         source_ref="source", board_id="board", spec_id="spec", scenario_id="scenario",
                         test_ids=["test_example.py::Cases.test_pass"], timeout_seconds=timeout,
                         allow_long_running=True)


@pytest.mark.asyncio
async def test_long_profile_runs_real_subprocess_and_remote_cannot_extend_it(tmp_path, project):
    from dataclasses import replace
    root, revision = project
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    register_profile(root=profile_root(ledger), name="long", workspace=str(root), source_ref="source",
                     board_id="board", spec_id="spec", scenario_id="scenario",
                     test_ids=["test_example.py::Cases.test_pass"], timeout_seconds=900,
                     allow_long_running=True)
    issuer = CommunityTestEvidenceExecutionIssuer(ledger=ledger, executor=lambda *_: None)
    request = Request(board_id="board", spec_id="spec", scenario_id="scenario", status="passed",
                      manifest_ref=None, actor_id="agent", scenario_sha256="sha256:" + "a" * 64,
                      inline_replay={"runner_ref": "long", "revision": revision})
    for forbidden in ({"timeout_seconds": 3600}, {"timeout_authorization": "local-long-running/v1"}):
        with pytest.raises(CommunityTestEvidenceError, match="request_requires"):
            await issuer.execute(replace(request, inline_replay={**request.inline_replay, **forbidden}))
    assert not ledger.receipt_root.exists()
    result = await issuer.execute(request)
    assert verify(ledger, request, result.evidence).verified


@pytest.mark.asyncio
async def test_missing_method_cannot_be_authenticated_as_a_failing_test(tmp_path, project):
    from dataclasses import replace
    ledger, issuer, request = setup(tmp_path, project, "missing")
    with pytest.raises(CommunityTestEvidenceError, match="runner_setup_failed"):
        await issuer.execute(replace(request, status="failed"))
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_paired_source_runner_snapshots_both_commits_and_attests_companion(tmp_path, project):
    root, _ = project
    (root / "test_paired.py").write_text('''import unittest
from pulse_companion_marker import VALUE
class PairedCases(unittest.TestCase):
    def test_companion(self): self.assertEqual(VALUE, 41)
''', encoding="utf-8")

    def git(workspace, *args):
        return subprocess.check_output(["git", "-C", str(workspace), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "paired test")
    primary_revision = git(root, "rev-parse", "HEAD")
    companion = tmp_path / "companion"
    (companion / "src").mkdir(parents=True)
    marker = companion / "src" / "pulse_companion_marker.py"
    marker.write_text("VALUE = 41\n", encoding="utf-8")
    git(companion, "init")
    git(companion, "add", ".")
    git(companion, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "companion fixture")
    companion_revision = git(companion, "rev-parse", "HEAD")
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "paired-evidence")
    register_profile(
        root=profile_root(ledger), name="paired", workspace=str(root),
        source_ref="source-core", board_id="board", spec_id="spec",
        scenario_id="scenario", test_ids=["test_paired.py::PairedCases.test_companion"],
        workspace_role="core", companions=[{
            "role": "community", "workspace": str(companion),
            "source_ref": "source-community", "revision": companion_revision,
        }],
    )
    marker.write_text("raise RuntimeError('dirty companion must be ignored')\n", encoding="utf-8")
    issuer = CommunityTestEvidenceExecutionIssuer(ledger=ledger, executor=lambda *_: None)
    request = Request(board_id="board", spec_id="spec", scenario_id="scenario", status="passed",
        manifest_ref=None, actor_id="agent", scenario_sha256="sha256:" + "a" * 64,
        inline_replay={"runner_ref": "paired", "revision": primary_revision})
    result = await issuer.execute(request)
    evidence = Evidence.model_validate(result.evidence).model_dump(exclude_none=True)
    assert verify(ledger, request, evidence).verified
    assert evidence["execution_attestation"]["execution_basis"]["companion_sources"] == [{
        "role": "community", "source_ref": "source-community", "revision": companion_revision,
    }]
    tampered = deepcopy(evidence)
    tampered["execution_attestation"]["execution_basis"]["companion_sources"][0]["revision"] = "b" * 40
    assert not verify(ledger, request, tampered).verified


def test_paired_profile_can_authorize_bounded_long_timeout(tmp_path, project):
    from okto_pulse.community.adapters.external_test_runner import _validate_profile

    companion = tmp_path / "companion"
    companion.mkdir()
    arguments = dict(
        root=tmp_path / "profiles", name="paired-long", workspace=str(project[0]),
        source_ref="source-core", board_id="board", spec_id="spec", scenario_id="scenario",
        test_ids=["test_example.py::Cases.test_pass"], timeout_seconds=900,
        workspace_role="core", companions=[{
            "role": "community", "workspace": str(companion),
            "source_ref": "source-community", "revision": "a" * 40,
        }],
    )
    with pytest.raises(CommunityTestEvidenceError, match="timeout_invalid"):
        register_profile(**arguments)
    path = register_profile(**arguments, allow_long_running=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "okto-pulse-external-unittest-profile/v2"
    assert data["timeout_authorization"] == "local-long-running/v1"
    _validate_profile(data)
