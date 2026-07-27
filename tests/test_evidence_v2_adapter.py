"""Adversarial tests for the Community Evidence V2 trust boundary."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from okto_pulse.community.adapters.test_evidence import (
    COMMUNITY_MANIFEST_SCHEMA,
    COMMUNITY_MANIFEST_PURPOSE,
    CommunityEvidenceLedger,
    CommunityHttpManifestExecutor,
    CommunityTestEvidenceError,
    CommunityTestEvidenceExecutionIssuer,
    CommunityTestEvidenceWriteVerifier,
    ProductExecutionObservation,
    migrate_test_scenario_evidence,
    normalize_test_scenario_evidence,
    run_inline_replay_and_build_evidence_v2,
    run_manifest_and_build_evidence_v2,
    verify_community_evidence_v2,
)
from okto_pulse.community.adapters import test_evidence as evidence_adapter
from okto_pulse.community.api.specs import (
    ScenarioStatusUpdate,
    _prepare_spec_update_evidence,
)
from okto_pulse.core.models.schemas import SpecUpdate
from okto_pulse.core.ports.test_evidence import (
    TestEvidenceExecutionRequest as EvidenceExecutionRequest,
)
from okto_pulse.core.services.test_scenario_lifecycle import (
    compute_execution_attestation_sha256,
    compute_test_scenario_semantic_sha256,
    verify_mcp_replay_evidence_v2,
)


BOARD_ID = "board-evidence"
SPEC_ID = "spec-evidence"
SCENARIO_ID = "ts-about"
ACTOR_ID = "agent-evidence"
ACCEPTANCE_CRITERIA = [{"id": "ac-about", "text": "About reports v0.3.0"}]
SCENARIO = {
    "id": SCENARIO_ID,
    "scenario_type": "e2e",
    "given": "the Community runtime is running",
    "when": "the health endpoint is read",
    "then": "the installed version is v0.3.0",
    "linked_criteria": ["ac-about"],
}
SCENARIO_SHA256 = compute_test_scenario_semantic_sha256(
    board_id=BOARD_ID,
    spec_id=SPEC_ID,
    scenario=SCENARIO,
    acceptance_criteria=ACCEPTANCE_CRITERIA,
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _manifest(
    *,
    expected: str = "0.3.0",
    board_id: str = BOARD_ID,
    spec_id: str = SPEC_ID,
    scenario_id: str = SCENARIO_ID,
    scenario_sha256: str = SCENARIO_SHA256,
) -> dict:
    return {
        "schema_version": COMMUNITY_MANIFEST_SCHEMA,
        "description": "Exercise the live About/health runtime",
        "purpose": COMMUNITY_MANIFEST_PURPOSE,
        "board_id": board_id,
        "spec_id": spec_id,
        "scenario_id": scenario_id,
        "scenario_sha256": scenario_sha256,
        "steps": [
            {
                "name": "health",
                "path": "/health",
                "expected_status": 200,
                "assertions": [
                    {
                        "name": "version",
                        "kind": "json_equals",
                        "path": "version",
                        "expected": expected,
                    }
                ],
            }
        ],
    }


def _ledger(tmp_path: Path) -> CommunityEvidenceLedger:
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    ledger.manifest_root.mkdir(parents=True)
    return ledger


async def _produce(
    tmp_path: Path,
    *,
    observed: str = "0.3.0",
    ledger: CommunityEvidenceLedger | None = None,
):
    ledger = ledger or _ledger(tmp_path)
    path = ledger.manifest_root / "about-replay.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    calls: list[str] = []

    async def execute(manifest, manifest_ref):
        calls.append(manifest_ref)
        assert manifest["schema_version"] == COMMUNITY_MANIFEST_SCHEMA
        return ProductExecutionObservation(
            run_id="community-run-1",
            outcome="passed",
            executed_at="2026-07-14T15:00:00Z",
            assertions=(
                {
                    "name": "about-version",
                    "expected": "0.3.0",
                    "observed": observed,
                    "status": "passed",
                },
            ),
        )

    evidence = await run_manifest_and_build_evidence_v2(
        manifest_ref="about-replay.json",
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        status="passed",
        actor_id=ACTOR_ID,
        executor=execute,
        ledger=ledger,
        environment="pytest",
    )
    return ledger, calls, evidence


def _inline_replay(*, path: str = "/health") -> str:
    return json.dumps(
        {
            "description": "MCP-only live health replay",
            "steps": [
                {
                    "name": "health",
                    "path": path,
                    "expected_status": 200,
                    "assertions": [
                        {
                            "name": "version",
                            "kind": "json_equals",
                            "path": "version",
                            "expected": "0.3.0",
                        }
                    ],
                }
            ],
        }
    )


async def _produce_inline(
    tmp_path: Path,
    *,
    replay: object | None = None,
    ledger: CommunityEvidenceLedger | None = None,
):
    ledger = ledger or CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    calls: list[tuple[dict, str]] = []

    async def execute(manifest, manifest_ref):
        calls.append((dict(manifest), manifest_ref))
        return ProductExecutionObservation(
            run_id=f"inline-run-{len(calls)}",
            outcome="passed",
            executed_at="2026-07-14T15:00:00Z",
            assertions=(
                {
                    "name": "health.version",
                    "expected": "0.3.0",
                    "observed": "0.3.0",
                    "status": "passed",
                },
            ),
        )

    evidence = await run_inline_replay_and_build_evidence_v2(
        inline_replay=replay or _inline_replay(),
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        status="passed",
        actor_id=ACTOR_ID,
        executor=execute,
        ledger=ledger,
        environment="pytest-inline",
    )
    return ledger, calls, evidence


@pytest.mark.asyncio
async def test_inline_replay_is_bound_persisted_canonically_and_idempotently(
    tmp_path,
):
    ledger, calls, first = await _produce_inline(tmp_path)
    _ledger_value, second_calls, second = await _produce_inline(
        tmp_path,
        replay=json.loads(_inline_replay()),
        ledger=ledger,
    )

    assert first["manifest_ref"] == second["manifest_ref"]
    assert first["manifest_ref"].startswith("inline-")
    assert first["manifest_ref"].endswith(".json")
    manifests = list(ledger.manifest_root.glob("*.json"))
    assert [path.name for path in manifests] == [first["manifest_ref"]]
    raw = manifests[0].read_bytes()
    decoded = json.loads(raw)
    assert raw == _canonical_json(decoded).encode("utf-8")
    assert decoded["board_id"] == BOARD_ID
    assert decoded["spec_id"] == SPEC_ID
    assert decoded["scenario_id"] == SCENARIO_ID
    assert decoded["scenario_sha256"] == SCENARIO_SHA256
    assert calls[0][0] == decoded
    assert second_calls[0][0] == decoded
    assert len(list(ledger.receipt_root.glob("*.json"))) == 2
    for evidence in (first, second):
        assert verify_community_evidence_v2(
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            status="passed",
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            actor_id=ACTOR_ID,
            evidence=evidence,
            ledger=ledger,
        ).verified


@pytest.mark.asyncio
async def test_inline_manifest_conflict_and_hardlink_never_reexecute(tmp_path):
    ledger, calls, evidence = await _produce_inline(tmp_path)
    target = ledger.manifest_root / evidence["manifest_ref"]
    target.write_bytes(b"tampered")

    with pytest.raises(
        CommunityTestEvidenceError,
        match="inline_manifest_content_conflict",
    ):
        await _produce_inline(tmp_path, ledger=ledger)
    assert len(calls) == 1
    assert len(list(ledger.receipt_root.glob("*.json"))) == 1

    target.unlink()
    _ledger_value, _calls, restored = await _produce_inline(tmp_path, ledger=ledger)
    target = ledger.manifest_root / restored["manifest_ref"]
    shadow = tmp_path / "manifest-hardlink.json"
    try:
        shadow.hardlink_to(target)
    except OSError as exc:  # pragma: no cover - filesystem may forbid hard links
        pytest.skip(f"host cannot create hard links: {exc}")
    with pytest.raises(
        CommunityTestEvidenceError,
        match="inline_manifest_hardlink_forbidden",
    ):
        await _produce_inline(tmp_path, ledger=ledger)


@pytest.mark.asyncio
async def test_content_addressed_manifest_remains_verifiable_after_write_roundtrip(
    tmp_path,
):
    ledger, _calls, evidence = await _produce_inline(tmp_path)
    write_verifier = CommunityTestEvidenceWriteVerifier(ledger=ledger)

    before_write = write_verifier.verify(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        actor_id=ACTOR_ID,
        evidence=evidence,
    )
    assert before_write.verified is True

    request = ScenarioStatusUpdate(status="passed", evidence=evidence)
    persisted_evidence = json.loads(
        _canonical_json(request.evidence.model_dump(mode="json", exclude_none=True))
    )
    after_write = write_verifier.verify(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        actor_id=ACTOR_ID,
        evidence=persisted_evidence,
    )

    assert after_write.verified is True
    assert persisted_evidence["manifest_ref"] == evidence["manifest_ref"]
    assert (ledger.manifest_root / evidence["manifest_ref"]).is_file()


@pytest.mark.asyncio
async def test_community_port_issuer_accepts_transport_neutral_inline_mapping(tmp_path):
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")

    async def executor(_manifest, _manifest_ref):
        return ProductExecutionObservation(
            run_id="port-inline-run",
            outcome="passed",
            executed_at="2026-07-14T15:00:00Z",
            assertions=(
                {
                    "name": "health.version",
                    "expected": "0.3.0",
                    "observed": "0.3.0",
                    "status": "passed",
                },
            ),
        )

    result = await CommunityTestEvidenceExecutionIssuer(
        ledger=ledger,
        executor=executor,
        environment="pytest-port",
    ).execute(
        EvidenceExecutionRequest(
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id=SCENARIO_ID,
            status="passed",
            manifest_ref=None,
            actor_id=ACTOR_ID,
            scenario_sha256=SCENARIO_SHA256,
            inline_replay=json.loads(_inline_replay()),
        )
    )

    assert result.evidence["manifest_ref"].startswith("inline-")
    assert result.evidence["execution_receipt"].startswith("ev2r.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replay", "reason"),
    [
        ('{"description":"a","description":"b","steps":[]}', "duplicate_key"),
        ('{"description":NaN,"steps":[]}', "manifest_invalid_json"),
        (
            json.dumps(
                {
                    "board_id": "caller-controlled",
                    "steps": [],
                }
            ),
            "inline_replay_unexpected_fields",
        ),
        (_inline_replay(path="/../outside"), "manifest_step_path_invalid"),
    ],
)
async def test_inline_replay_rejects_ambiguous_bindings_and_traversal(
    tmp_path,
    replay,
    reason,
):
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    called = False

    async def executor(_manifest, _ref):
        nonlocal called
        called = True
        raise AssertionError("invalid inline replay must never execute")

    with pytest.raises(CommunityTestEvidenceError, match=reason):
        await run_inline_replay_and_build_evidence_v2(
            inline_replay=replay,
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            status="passed",
            actor_id=ACTOR_ID,
            executor=executor,
            ledger=ledger,
        )
    assert called is False
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_inline_replay_is_size_bounded_before_json_decode(tmp_path):
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "evidence")
    replay = '{"description":"' + ("x" * (1024 * 1024)) + '","steps":[]}'

    with pytest.raises(CommunityTestEvidenceError, match="manifest_too_large"):
        await run_inline_replay_and_build_evidence_v2(
            inline_replay=replay,
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            status="passed",
            actor_id=ACTOR_ID,
            executor=lambda *_args: None,
            ledger=ledger,
        )
    assert not ledger.manifest_root.exists()


@pytest.mark.asyncio
async def test_inline_manifest_reparse_root_fails_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence_root = tmp_path / "linked-evidence"
    evidence_root.mkdir()
    try:
        (evidence_root / "manifests").symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - host policy may prohibit symlinks
        pytest.skip(f"host cannot create symlinks: {exc}")
    linked_ledger = CommunityEvidenceLedger(evidence_root=evidence_root)
    with pytest.raises(CommunityTestEvidenceError, match="reparse_point_forbidden"):
        await _produce_inline(tmp_path, ledger=linked_ledger)
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_inline_manifest_publish_swap_fails_before_execution(
    tmp_path,
    monkeypatch,
):
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / "race-evidence")
    original_link = evidence_adapter.os.link

    def racing_link(source, target, **kwargs):
        result = original_link(source, target, **kwargs)
        Path(target).unlink()
        Path(target).write_bytes(b"attacker bytes")
        return result

    monkeypatch.setattr(evidence_adapter.os, "link", racing_link)
    with pytest.raises(
        CommunityTestEvidenceError,
        match="inline_manifest_persist_failed",
    ):
        await _produce_inline(tmp_path, ledger=ledger)
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_real_http_runtime_executes_before_signed_receipt(tmp_path):
    app = FastAPI()
    calls = 0

    @app.get("/health")
    async def health():
        nonlocal calls
        calls += 1
        return {"status": "healthy", "version": "0.3.0"}

    ledger = _ledger(tmp_path)
    (ledger.manifest_root / "about-replay.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )
    executor = CommunityHttpManifestExecutor(
        base_url="http://127.0.0.1:8100",
        transport=httpx.ASGITransport(app=app),
    )
    evidence = await run_manifest_and_build_evidence_v2(
        manifest_ref="about-replay.json",
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        status="passed",
        actor_id=ACTOR_ID,
        executor=executor,
        ledger=ledger,
        environment="pytest-asgi",
    )

    assert calls == 1
    assert evidence["manifest_ref"] == "about-replay.json"
    assert evidence["execution_receipt"].startswith("ev2r.")
    assert verify_mcp_replay_evidence_v2(
        "passed", evidence, scenario_id=SCENARIO_ID
    ).verified
    assert verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=evidence,
        ledger=ledger,
    ).verified

    request = ScenarioStatusUpdate(status="passed", evidence=evidence)
    assert request.evidence is not None
    assert request.evidence.execution_receipt == evidence["execution_receipt"]
    whole_spec = _prepare_spec_update_evidence(
        SpecUpdate(
            test_scenarios=[
                {
                    "id": SCENARIO_ID,
                    "title": "About version",
                    "status": "passed",
                    "evidence": evidence,
                }
            ]
        )
    )
    assert (
        whole_spec.test_scenarios[0].evidence.execution_receipt
        == evidence["execution_receipt"]
    )


@pytest.mark.asyncio
async def test_text_or_invalid_manifest_never_invokes_runtime(tmp_path):
    ledger = _ledger(tmp_path)
    (ledger.manifest_root / "not-a-manifest.json").write_text(
        "this is only caller-authored text", encoding="utf-8"
    )
    called = False

    async def executor(_manifest, _ref):
        nonlocal called
        called = True
        raise AssertionError("must not execute")

    with pytest.raises(CommunityTestEvidenceError, match="manifest_invalid_json"):
        await run_manifest_and_build_evidence_v2(
            manifest_ref="not-a-manifest.json",
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            status="passed",
            actor_id=ACTOR_ID,
            executor=executor,
            ledger=ledger,
        )
    assert called is False
    assert not ledger.receipt_root.exists()


@pytest.mark.asyncio
async def test_absolute_traversal_and_outside_root_are_rejected(tmp_path):
    ledger = _ledger(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_manifest()), encoding="utf-8")

    async def executor(_manifest, _ref):
        raise AssertionError("must not execute outside allowlist")

    for ref in (str(outside.resolve()), "../outside.json"):
        with pytest.raises(
            CommunityTestEvidenceError,
            match="manifest_ref_(must_be_relative|path_traversal)",
        ):
            await run_manifest_and_build_evidence_v2(
                manifest_ref=ref,
                board_id=BOARD_ID,
                spec_id=SPEC_ID,
                scenario_id=SCENARIO_ID,
                scenario_sha256=SCENARIO_SHA256,
                status="passed",
                actor_id=ACTOR_ID,
                executor=executor,
                ledger=ledger,
            )


@pytest.mark.asyncio
async def test_auto_signed_or_unregistered_receipt_is_rejected(tmp_path):
    ledger, _calls, evidence = await _produce(tmp_path)
    forged = deepcopy(evidence)
    forged["execution_receipt"] = "ev2r." + "a" * 32 + "." + "b" * 64
    verdict = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=forged,
        ledger=ledger,
    )
    assert verdict.verified is False
    assert "evidence_v2.receipt_not_registered" in verdict.reason_codes


@pytest.mark.asyncio
async def test_tampering_and_cross_scope_replay_fail_closed(tmp_path):
    ledger, _calls, evidence = await _produce(tmp_path)
    for overrides, expected_reason in (
        ({"board_id": "other-board"}, "receipt_board_id_binding_mismatch"),
        ({"spec_id": "other-spec"}, "receipt_spec_id_binding_mismatch"),
        ({"scenario_id": "other-scenario"}, "scenario_binding_mismatch"),
        ({"status": "automated"}, "receipt_status_binding_mismatch"),
    ):
        verdict = verify_community_evidence_v2(
            board_id=overrides.get("board_id", BOARD_ID),
            spec_id=overrides.get("spec_id", SPEC_ID),
            status=overrides.get("status", "passed"),
            scenario_id=overrides.get("scenario_id", SCENARIO_ID),
            scenario_sha256=SCENARIO_SHA256,
            evidence=evidence,
            ledger=ledger,
        )
        assert verdict.verified is False
        assert any(expected_reason in reason for reason in verdict.reason_codes)

    tampered = deepcopy(evidence)
    tampered["execution_attestation"]["run_id"] = "client-rewritten"
    tampered["execution_attestation"]["attestation_sha256"] = (
        compute_execution_attestation_sha256(
            tampered["execution_attestation"],
            manifest_ref=tampered["manifest_ref"],
        )
    )
    verdict = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=tampered,
        ledger=ledger,
    )
    assert verdict.verified is False
    assert "evidence_v2.receipt_evidence_tampered" in verdict.reason_codes


@pytest.mark.asyncio
async def test_actor_and_current_semantic_digest_are_write_bindings(tmp_path):
    ledger, _calls, evidence = await _produce(tmp_path)

    wrong_actor = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        actor_id="different-actor",
        evidence=evidence,
        ledger=ledger,
    )
    assert wrong_actor.verified is False
    assert "evidence_v2.receipt_actor_id_binding_mismatch" in wrong_actor.reason_codes

    changed = {**SCENARIO, "given": "the semantics changed after execution"}
    changed_digest = compute_test_scenario_semantic_sha256(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        scenario=changed,
        acceptance_criteria=ACCEPTANCE_CRITERIA,
    )
    stale = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=changed_digest,
        actor_id=ACTOR_ID,
        evidence=evidence,
        ledger=ledger,
    )
    assert stale.verified is False
    assert "evidence_v2.scenario_semantic_binding_mismatch" in stale.reason_codes


@pytest.mark.asyncio
async def test_manifest_is_bound_and_generic_or_cross_scenario_replay_is_rejected(
    tmp_path,
):
    ledger = _ledger(tmp_path)
    path = ledger.manifest_root / "bound.json"
    generic = _manifest()
    for field in ("purpose", "board_id", "spec_id", "scenario_id", "scenario_sha256"):
        generic.pop(field)
    path.write_text(json.dumps(generic), encoding="utf-8")

    async def executor(_manifest, _ref):
        raise AssertionError("an unbound manifest must never execute")

    with pytest.raises(CommunityTestEvidenceError, match="manifest_purpose_invalid"):
        await run_manifest_and_build_evidence_v2(
            manifest_ref="bound.json",
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            status="passed",
            actor_id=ACTOR_ID,
            executor=executor,
            ledger=ledger,
        )

    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    with pytest.raises(
        CommunityTestEvidenceError, match="scenario_id_binding_mismatch"
    ):
        await run_manifest_and_build_evidence_v2(
            manifest_ref="bound.json",
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id="another-scenario",
            scenario_sha256=SCENARIO_SHA256,
            status="passed",
            actor_id=ACTOR_ID,
            executor=executor,
            ledger=ledger,
        )


@pytest.mark.asyncio
async def test_manifest_receipt_and_secret_reparse_points_are_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "about-replay.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )

    manifest_evidence = tmp_path / "manifest-link-evidence"
    manifest_evidence.mkdir()
    try:
        (manifest_evidence / "manifests").symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - host policy may prohibit symlinks
        pytest.skip(f"host cannot create symlinks: {exc}")
    manifest_ledger = CommunityEvidenceLedger(evidence_root=manifest_evidence)

    async def executor(_manifest, _ref):
        raise AssertionError("reparse-backed manifest must never execute")

    with pytest.raises(CommunityTestEvidenceError, match="reparse_point_forbidden"):
        await run_manifest_and_build_evidence_v2(
            manifest_ref="about-replay.json",
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            status="passed",
            actor_id=ACTOR_ID,
            executor=executor,
            ledger=manifest_ledger,
        )

    receipt_ledger = _ledger(tmp_path / "receipt-case")
    (receipt_ledger.manifest_root / "about-replay.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )
    receipt_outside = tmp_path / "outside-receipts"
    receipt_outside.mkdir()
    receipt_ledger.receipt_root.symlink_to(receipt_outside, target_is_directory=True)
    with pytest.raises(CommunityTestEvidenceError, match="reparse_point_forbidden"):
        await _produce(tmp_path / "receipt-case", ledger=receipt_ledger)
    assert list(receipt_outside.iterdir()) == []

    secret_ledger = _ledger(tmp_path / "secret-case")
    known_secret = tmp_path / "known-secret"
    known_secret.write_bytes(b"x" * 32)
    secret_ledger.secret_path.symlink_to(known_secret)
    with pytest.raises(CommunityTestEvidenceError, match="reparse_point_forbidden"):
        await _produce(tmp_path / "secret-case", ledger=secret_ledger)


def test_windows_reparse_attribute_is_detected_even_without_symlink_mode():
    fake = SimpleNamespace(st_mode=stat_mode_regular(), st_file_attributes=0x400)
    assert evidence_adapter._is_reparse_stat(fake) is True


def stat_mode_regular() -> int:
    # S_IFREG keeps the test portable; Windows junction detection comes from
    # FILE_ATTRIBUTE_REPARSE_POINT rather than POSIX symlink mode.
    import stat

    return stat.S_IFREG


@pytest.mark.asyncio
async def test_manifest_growth_during_handle_read_fails_closed(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    manifest_path = ledger.manifest_root / "about-replay.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    original_read = evidence_adapter.os.read
    mutated = False

    def racing_read(fd, size):
        nonlocal mutated
        if not mutated:
            mutated = True
            with manifest_path.open("ab") as stream:
                stream.write(b" ")
        return original_read(fd, size)

    monkeypatch.setattr(evidence_adapter.os, "read", racing_read)

    async def executor(_manifest, _ref):
        raise AssertionError("a changing manifest must never execute")

    with pytest.raises(
        CommunityTestEvidenceError,
        match="secure_file_changed_(during_read|path_changed)",
    ):
        await run_manifest_and_build_evidence_v2(
            manifest_ref="about-replay.json",
            board_id=BOARD_ID,
            spec_id=SPEC_ID,
            scenario_id=SCENARIO_ID,
            scenario_sha256=SCENARIO_SHA256,
            status="passed",
            actor_id=ACTOR_ID,
            executor=executor,
            ledger=ledger,
        )


@pytest.mark.asyncio
async def test_manifest_change_and_secret_loss_or_rotation_fail_closed(tmp_path):
    ledger, _calls, evidence = await _produce(tmp_path)
    (ledger.manifest_root / "about-replay.json").write_text(
        json.dumps(_manifest(expected="9.9.9")), encoding="utf-8"
    )
    changed = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=evidence,
        ledger=ledger,
    )
    assert "evidence_v2.manifest_content_hash_mismatch" in changed.reason_codes

    ledger.secret_path.unlink()
    missing = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=evidence,
        ledger=ledger,
    )
    assert "evidence_v2.receipt_secret_missing" in missing.reason_codes
    with pytest.raises(CommunityTestEvidenceError, match="receipt_secret_missing"):
        await _produce(tmp_path, ledger=ledger)
    assert not ledger.secret_path.exists()

    ledger.secret_path.write_bytes(b"x" * 32)
    ledger.secret_path.chmod(0o600)
    rotated = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=evidence,
        ledger=ledger,
    )
    assert "evidence_v2.receipt_secret_rotated" in rotated.reason_codes
    with pytest.raises(CommunityTestEvidenceError, match="receipt_secret_rotated"):
        await _produce(tmp_path, ledger=ledger)


@pytest.mark.asyncio
async def test_rotated_key_cannot_be_hidden_by_injected_first_receipt(tmp_path):
    ledger, _calls, _evidence = await _produce(tmp_path)
    rotated_key = b"r" * 32
    ledger.secret_path.write_bytes(rotated_key)
    ledger.secret_path.chmod(0o600)
    rotated_key_id = hashlib.sha256(rotated_key).hexdigest()[:16]
    injected = ledger.receipt_root / ("0" * 32 + ".json")
    injected.write_text(
        json.dumps({"secret_key_id": rotated_key_id}),
        encoding="utf-8",
    )
    before = {path.name for path in ledger.receipt_root.iterdir()}

    with pytest.raises(CommunityTestEvidenceError, match="receipt_secret_rotated"):
        await _produce(tmp_path, ledger=ledger)

    assert {path.name for path in ledger.receipt_root.iterdir()} == before


@pytest.mark.asyncio
async def test_append_authenticates_every_historical_receipt(tmp_path):
    ledger, _calls, _evidence = await _produce(tmp_path)
    await _produce(tmp_path, ledger=ledger)
    receipts = sorted(ledger.receipt_root.glob("*.json"))
    assert len(receipts) == 2
    later_record = json.loads(receipts[-1].read_text(encoding="utf-8"))
    later_record["signature"] = "0" * 64
    receipts[-1].write_text(_canonical_json(later_record), encoding="utf-8")

    with pytest.raises(
        CommunityTestEvidenceError,
        match="receipt_ledger_signature_invalid",
    ):
        await _produce(tmp_path, ledger=ledger)


@pytest.mark.asyncio
async def test_signed_legacy_receipt_is_continuity_only_and_never_authority(
    tmp_path,
):
    ledger, _calls, old_evidence = await _produce(tmp_path)
    receipt_path = next(ledger.receipt_root.glob("*.json"))
    legacy_record = json.loads(receipt_path.read_text(encoding="utf-8"))
    legacy_record.pop("scenario_sha256")
    legacy_record["signature"] = ledger._sign(  # noqa: SLF001
        legacy_record,
        ledger.secret_path.read_bytes(),
    )
    receipt_path.write_text(_canonical_json(legacy_record), encoding="utf-8")

    old_verdict = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=old_evidence,
        ledger=ledger,
    )
    assert old_verdict.verified is False
    assert "evidence_v2.receipt_ledger_schema_invalid" in old_verdict.reason_codes

    _ledger, _calls, new_evidence = await _produce(tmp_path, ledger=ledger)
    new_verdict = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=new_evidence,
        ledger=ledger,
    )
    assert new_verdict.verified is True


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("whitespace", "receipt_ledger_noncanonical_record"),
        ("reordered", "receipt_ledger_noncanonical_record"),
        ("duplicate_key", "receipt_ledger_duplicate_key"),
    ],
)
@pytest.mark.asyncio
async def test_receipt_bytes_are_canonical_for_verify_and_append_scan(
    tmp_path,
    mutation,
    expected_reason,
):
    ledger, _calls, evidence = await _produce(tmp_path)
    receipt_path = next(ledger.receipt_root.glob("*.json"))
    canonical = receipt_path.read_text(encoding="utf-8")
    record = json.loads(canonical)
    if mutation == "whitespace":
        rewritten = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    elif mutation == "reordered":
        rewritten = json.dumps(
            dict(reversed(tuple(record.items()))),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        duplicate_actor = json.dumps(record["actor_id"], ensure_ascii=False)
        rewritten = f'{{"actor_id":{duplicate_actor},' + canonical[1:]
    receipt_path.write_text(rewritten, encoding="utf-8")

    verdict = verify_community_evidence_v2(
        board_id=BOARD_ID,
        spec_id=SPEC_ID,
        status="passed",
        scenario_id=SCENARIO_ID,
        scenario_sha256=SCENARIO_SHA256,
        evidence=evidence,
        ledger=ledger,
    )
    assert verdict.verified is False
    assert f"evidence_v2.{expected_reason}" in verdict.reason_codes

    with pytest.raises(CommunityTestEvidenceError, match=expected_reason):
        await _produce(tmp_path, ledger=ledger)


@pytest.mark.asyncio
async def test_runner_refuses_contradictory_observation_without_receipt(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(CommunityTestEvidenceError, match="observed_expected_mismatch"):
        await _produce(tmp_path, observed="0.2.5", ledger=ledger)
    assert not ledger.receipt_root.exists()


def test_legacy_normalization_never_fabricates_attestation_or_receipt():
    legacy_string = {
        "evidence_class": "mcp_replay_manifest",
        "mcp_replay_manifest": "manifests/legacy.json",
    }
    legacy_object = {
        "evidence_class": "mcp_replay_manifest",
        "mcp_replay_manifest": {
            "product_runtime_exercised": True,
            "expected_output_snapshot": "0.3.0",
            "observed_output": "0.3.0",
        },
    }
    pre_receipt_v2 = {
        "evidence_class": "mcp_replay_manifest",
        "manifest_ref": "manifests/pre-receipt.json",
        "execution_attestation": {"schema_version": 2},
    }
    assert normalize_test_scenario_evidence(legacy_string) == legacy_string
    assert normalize_test_scenario_evidence(legacy_object) == legacy_object
    report = migrate_test_scenario_evidence(
        [
            {"id": "ts-string", "evidence": legacy_string},
            {"id": "ts-object", "evidence": legacy_object},
            {"id": "ts-pre-receipt", "evidence": pre_receipt_v2},
        ]
    )
    assert report.legacy_unverified == 3
    assert report.promoted_v2 == 0


@pytest.mark.asyncio
async def test_complete_embedded_signed_v2_is_promoted_losslessly(tmp_path):
    _ledger_value, _calls, canonical = await _produce(tmp_path)
    embedded = {
        "evidence_class": "mcp_replay_manifest",
        "mcp_replay_manifest": {
            "manifest_ref": canonical["manifest_ref"],
            "execution_attestation": canonical["execution_attestation"],
            "execution_receipt": canonical["execution_receipt"],
        },
    }
    normalized = normalize_test_scenario_evidence(embedded)
    assert normalized == canonical
    report = migrate_test_scenario_evidence(
        [{"id": SCENARIO_ID, "status": "passed", "evidence": embedded}]
    )
    assert report.promoted_v2 == 1
    assert report.scenarios[0]["evidence"] == canonical
