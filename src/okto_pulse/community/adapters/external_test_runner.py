"""Explicit host authorization for external unittest execution.

Remote callers select an existing scoped profile and immutable revision, never
commands, interpreters, paths or environment variables. Project tests are code:
local registration authorizes running that code as the Pulse OS user. A temporary
Git snapshot provides reproducibility, not an OS security sandbox.
"""

import asyncio
from datetime import datetime, timezone
import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import sys
import tempfile

from okto_pulse.core.models.schemas import TestExecutionBasis
from .test_evidence import (
    CommunityTestEvidenceError, ProductExecutionObservation,
    _assert_no_reparse_chain, _read_regular_file_secure, _decode_manifest_json,
    _persist_inline_manifest, _execute_validated_manifest_and_build_evidence_v2,
)

SCHEMA = "okto-pulse-external-unittest/v1"
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_TEST = re.compile(r"[a-zA-Z0-9_/.-]+\.py::[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*")


def _fail(reason):
    raise CommunityTestEvidenceError("external_test." + reason)


def profile_root(ledger):
    # Host execution authority is deliberately separate from portable evidence.
    # Restoring receipts must not silently authorize code on the receiving host.
    return ledger.evidence_root.parent / "test-runners"


def _profile_path(root, name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        _fail("runner_ref_invalid")
    path = root / (name + ".json")
    _assert_no_reparse_chain(path, allow_missing=True)
    return path


def register_profile(*, root, name, workspace, source_ref, board_id, spec_id,
                     scenario_id, test_ids, timeout_seconds=60):
    path = _profile_path(Path(root), name)
    workspace = Path(workspace)
    if not workspace.is_absolute() or not workspace.is_dir():
        _fail("absolute_workspace_required")
    _assert_no_reparse_chain(workspace, allow_missing=False)
    data = dict(schema_version=SCHEMA, workspace=str(workspace), source_ref=source_ref,
                board_id=board_id, spec_id=spec_id, scenario_id=scenario_id,
                test_ids=test_ids, timeout_seconds=timeout_seconds)
    _validate_profile(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A local operator must explicitly remove a profile to replace its authority.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    return path


def _validate_profile(data):
    keys = {"schema_version", "workspace", "source_ref", "board_id", "spec_id",
            "scenario_id", "test_ids", "timeout_seconds"}
    if not isinstance(data, dict) or set(data) != keys or data["schema_version"] != SCHEMA:
        _fail("profile_invalid")
    if any(not isinstance(data[k], str) or not data[k].strip()
           for k in ("workspace", "source_ref", "board_id", "spec_id", "scenario_id")):
        _fail("profile_invalid")
    if type(data["timeout_seconds"]) is not int or not 1 <= data["timeout_seconds"] <= 120:
        _fail("timeout_invalid")
    ids = data["test_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(v, str) for v in ids):
        _fail("test_ids_invalid")
    if len(set(ids)) != len(ids):
        _fail("test_ids_invalid")
    for selector in ids:
        if not isinstance(selector, str) or not _TEST.fullmatch(selector):
            _fail("test_id_invalid")
        path = PurePosixPath(selector.split("::")[0])
        if path.is_absolute() or ".." in path.parts:
            _fail("test_path_invalid")


def validate_external_manifest(data, *, board_id, spec_id, scenario_id, scenario_sha256):
    keys = {"schema_version", "purpose", "board_id", "spec_id", "scenario_id",
            "scenario_sha256", "execution_basis"}
    if not isinstance(data, dict) or set(data) != keys or data["schema_version"] != SCHEMA:
        _fail("manifest_invalid")
    if data["purpose"] != "test_scenario_evidence":
        _fail("manifest_invalid")
    for key, expected in (("board_id", board_id), ("spec_id", spec_id),
                          ("scenario_id", scenario_id), ("scenario_sha256", scenario_sha256)):
        if data[key] != expected:
            _fail("scope_mismatch")
    try:
        TestExecutionBasis.model_validate(data["execution_basis"])
    except ValueError as exc:
        raise CommunityTestEvidenceError("external_test.execution_basis_invalid") from exc
    return data


def _git(workspace, *args, input_data=None):
    try:
        result = subprocess.run(["git", "-C", str(workspace), *args],
                                capture_output=True, input=input_data, timeout=20, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommunityTestEvidenceError("external_test.git_snapshot_unavailable") from exc


def _snapshot(workspace, revision, destination):
    _assert_no_reparse_chain(workspace, allow_missing=False)
    actual = _git(workspace, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
    if actual != revision:
        _fail("revision_mismatch")
    entries = _git(workspace, "ls-tree", "-r", "-l", "-z", revision).split(b"\0")
    tracked = []
    total = 0
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, digest, size = metadata.decode().split()
        if mode not in {"100644", "100755"} or kind != "blob":
            _fail("snapshot_regular_files_required")
        name = raw_path.decode("utf-8")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            _fail("snapshot_path_invalid")
        size = int(size)
        total += size
        if len(tracked) >= 10000 or size > 8 * 1024 * 1024 or total > 64 * 1024 * 1024:
            _fail("snapshot_too_large")
        tracked.append((path, digest, size))
    if not tracked:
        _fail("snapshot_size_invalid")
    # Read blobs directly; archive applies export attributes and Windows line
    # ending conversion, which would change the declared immutable input.
    requests = "".join(digest + "\n" for _, digest, _ in tracked).encode()
    blobs = io.BytesIO(_git(workspace, "cat-file", "--batch", input_data=requests))
    for path, digest, size in tracked:
        if blobs.readline().decode().strip() != f"{digest} blob {size}":
            _fail("snapshot_not_exact_commit")
        content = blobs.read(size)
        actual = hashlib.new("sha256" if len(revision) == 64 else "sha1",
                             f"blob {len(content)}\0".encode() + content).hexdigest()
        if actual != digest or blobs.read(1) != b"\n":
            _fail("snapshot_not_exact_commit")
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _observe(profile, revision):
    with tempfile.TemporaryDirectory(prefix="pulse-test-") as temp:
        root = Path(temp) / "project"
        root.mkdir()
        _snapshot(Path(profile["workspace"]), revision, root)
        report = Path(temp) / "result.json"
        worker = Path(__file__).with_name("external_unittest_worker.py")
        # Do not forward API tokens, Pulse configuration or PYTHONPATH.
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG",
                                  "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "HOME"}}
        with open(os.devnull, "wb") as output:
            process = subprocess.Popen([sys.executable, "-E", "-P", "-B", str(worker), str(root),
                                        str(report), *profile["test_ids"]],
                                       cwd=root, env=env, stdout=output, stderr=output,
                                       start_new_session=os.name != "nt")
            try:
                code = process.wait(timeout=profile["timeout_seconds"])
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=10)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                process.kill()
                process.wait(timeout=10)
                _fail("timeout_no_receipt")
        raw = _read_regular_file_secure(report, max_bytes=4096,
                missing_code="external_test.result_missing", invalid_code="external_test.result_invalid")
        result = _decode_manifest_json(raw, source="unittest result")
        if set(result) == {"runner_error"} and isinstance(result["runner_error"], str):
            _fail("runner_setup_failed:" + result["runner_error"] +
                  "; check test selectors and dependencies in the Pulse Python environment")
        keys = {"tests_run", "failures", "errors", "skipped", "expected_failures", "unexpected_successes"}
        if set(result) != keys or any(type(v) is not int or v < 0 for v in result.values()):
            _fail("result_invalid")
        if result["tests_run"] != len(profile["test_ids"]) or result["tests_run"] == 0:
            _fail("test_count_mismatch")
        if result["skipped"] or result["expected_failures"]:
            _fail("incomplete_run_no_receipt")
        passed = code == 0 and not any(result[k] for k in keys - {"tests_run"})
        if code not in (0, 1):
            _fail("runner_aborted")
        assertion = dict(name="external unittest results", expected={**result, "failures": 0,
                         "errors": 0, "unexpected_successes": 0, "exit_code": 0},
                         observed={**result, "exit_code": code}, status="passed" if passed else "failed")
        return ProductExecutionObservation(secrets.token_hex(16), "passed" if passed else "failed",
                                           (assertion,), datetime.now(timezone.utc).isoformat())


async def execute_external_request(request, *, ledger, scope, status, actor_id, environment):
    if not isinstance(request, dict) or set(request) != {"runner_ref", "revision"}:
        _fail("request_requires_runner_ref_and_revision")
    revision = request["revision"]
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        _fail("immutable_revision_required")
    path = _profile_path(profile_root(ledger), request["runner_ref"])
    raw = _read_regular_file_secure(path, max_bytes=16384,
            missing_code="external_test.runner_not_registered_use_local_cli",
            invalid_code="external_test.profile_invalid")
    profile = _decode_manifest_json(raw, source="local runner profile")
    _validate_profile(profile)
    if any(profile[k] != scope[k] for k in ("board_id", "spec_id", "scenario_id")):
        _fail("profile_scope_mismatch")
    basis = dict(source_ref=profile["source_ref"], revision=revision,
                 runner_ref=request["runner_ref"], test_ids=profile["test_ids"])
    normalized = dict(schema_version=SCHEMA, purpose="test_scenario_evidence", **scope, execution_basis=basis)
    validate_external_manifest(normalized, **scope)
    manifest, canonical_ref = _persist_inline_manifest(normalized, ledger=ledger)

    async def executor(_manifest, _ref):
        return await asyncio.to_thread(_observe, profile, revision)

    return await _execute_validated_manifest_and_build_evidence_v2(
        manifest=manifest, canonical_ref=canonical_ref, normalized_manifest=normalized,
        **scope, status=status, actor_id=actor_id, executor=executor, ledger=ledger,
        environment=environment, execution_basis=basis)
