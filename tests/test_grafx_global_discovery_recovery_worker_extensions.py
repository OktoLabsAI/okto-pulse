"""Effective production-worker extensions of the Grafx recovery leaf."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path

import okto_grafx
import pytest
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryCutoverResult,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    RecoveryProgressCounts,
    RecoveryTerminalOutcome,
    recovery_attempt_id,
)
from test_grafx_global_discovery_providers import (
    _board_seed,
    _DatabaseSlot,
    _runtime,
)

import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityGlobalDiscoveryRecoveryNativeOperation,
    RecoveryNativeInputs,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    CommunityGrafxGlobalDiscoveryRecovery,
    CommunityGrafxGlobalDiscoveryRecoveryError,
)
from okto_pulse.community.adapters.grafx_global_operational import (
    read_safe_active_generation,
)


class _Lease:
    @contextlib.contextmanager
    def guard(self):
        yield

    def renew(self) -> None:
        return None

    def assert_fenced(self) -> None:
        return None

    def release(self) -> None:
        return None


def _recovery(slot: _DatabaseSlot) -> CommunityGrafxGlobalDiscoveryRecovery:
    return CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        lambda path: okto_grafx.connect(path, vector_exact_scan_threshold=4096),
        slot.close,
        lambda _phase: None,
    )


def test_snapshot_fingerprint_late_binding_is_identity_idempotent(
    tmp_path: Path,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    recovery = _recovery(slot)
    provider = lambda: "sha256:relational"

    recovery.bind_snapshot_fingerprint_provider(provider)
    recovery.bind_snapshot_fingerprint_provider(provider)

    assert recovery.current_snapshot_fingerprint() == "sha256:relational"
    with pytest.raises(CommunityGrafxGlobalDiscoveryRecoveryError) as conflict:
        recovery.bind_snapshot_fingerprint_provider(lambda: "sha256:different")
    assert conflict.value.code == "global_discovery_snapshot_fingerprint_already_bound"


def test_reconcile_only_without_published_candidate_is_non_mutating(
    tmp_path: Path,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    opened: list[Path] = []
    recovery = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        lambda path: opened.append(path),  # type: ignore[arg-type,return-value]
        slot.close,
        lambda _phase: None,
    )
    run_id = "gdr_reconcileonly"
    attempt_id = recovery_attempt_id(run_id, 1)

    result = recovery.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        expected_live_sha256="0" * 64,
        boards=(_board_seed("board-a", "source-a"),),
        fence_check=lambda: None,
    )

    assert result is None
    assert opened == []
    assert not (tmp_path / "discovery.generations").exists()
    assert not (tmp_path / "quarantine").exists()


def test_normal_worker_calls_grafx_reconcile_then_unified_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    recovery = _recovery(slot)
    calls: list[str] = []

    def reconcile_attempt_artifacts(**kwargs):
        kwargs["fence_check"]()
        calls.append("reconcile_attempt_artifacts")

    def recover_and_cutover(**kwargs):
        kwargs["fence_check"]()
        calls.append("recover_and_cutover")
        return GlobalDiscoveryCutoverResult(
            outcome="completed",
            candidate_sha256="a" * 64,
            quarantine_ref=None,
            schema_object_count=3,
            recovery_journal_ref="grafx:test-worker",
        )

    monkeypatch.setattr(
        recovery, "reconcile_attempt_artifacts", reconcile_attempt_artifacts
    )
    monkeypatch.setattr(recovery, "recover_and_cutover", recover_and_cutover)
    monkeypatch.setattr(
        worker_module.GlobalDiscoveryWriterLease,
        "acquire",
        lambda **_kwargs: _Lease(),
    )
    run_id = "gdr_workerextensions"
    attempt_id = recovery_attempt_id(run_id, 1)
    operation = CommunityGlobalDiscoveryRecoveryNativeOperation(
        recovery=recovery,
        input_provider=lambda **_kwargs: RecoveryNativeInputs(
            expected_live_sha256="0" * 64,
            boards=(_board_seed("board-a", "source-a"),),
            terminal_counts=RecoveryProgressCounts(),
        ),
    )

    result = operation(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        fence_check=lambda: None,
    )

    assert result.outcome is RecoveryTerminalOutcome.SUCCESS
    assert calls == ["reconcile_attempt_artifacts", "recover_and_cutover"]


def test_canonical_recovery_reconciles_and_binds_successor_evidence(
    tmp_path: Path,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    runtime = _runtime(slot)
    runtime.bootstrap()
    slot.close()
    recovery = _recovery(slot)
    run_id = "gdr_grafxpredecessor"
    attempt_one = recovery_attempt_id(run_id, 1)
    boards = (_board_seed("board-a", "source-a"),)
    before = recovery.inspect_live_artifact()

    completed = recovery.recover_and_cutover(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_one,
        expected_live_sha256=before.sha256,
        boards=boards,
        fence_check=lambda: None,
    )
    active_before = read_safe_active_generation(slot.legacy)
    assert active_before is not None
    journal_one_path = (
        tmp_path
        / "quarantine"
        / "global-discovery"
        / run_id
        / "attempt-1"
        / "recovery_journal.json"
    )
    journal_one_bytes = journal_one_path.read_bytes()
    journal_one = json.loads(journal_one_bytes)

    reconciled = recovery.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_one,
        expected_live_sha256=before.sha256,
        boards=boards,
        fence_check=lambda: None,
    )
    with pytest.raises(CommunityGrafxGlobalDiscoveryRecoveryError) as wrong_sha:
        recovery.reconcile_attempt_terminal_truth(
            run_id=run_id,
            epoch=1,
            attempt_id=attempt_one,
            expected_live_sha256="f" * 64,
            boards=boards,
            fence_check=lambda: None,
        )
    assert wrong_sha.value.code.endswith(":expected_live_sha256")
    assert journal_one_path.read_bytes() == journal_one_bytes
    attempt_two = recovery_attempt_id(run_id, 2)
    successor = recovery.reconcile_predecessor_and_complete(
        run_id=run_id,
        epoch=2,
        attempt_id=attempt_two,
        ancestry=((1, attempt_one),),
        expected_live_sha256=before.sha256,
        boards=boards,
        fence_check=lambda: None,
    )
    repeated = recovery.reconcile_predecessor_and_complete(
        run_id=run_id,
        epoch=2,
        attempt_id=attempt_two,
        ancestry=((1, attempt_one),),
        expected_live_sha256=before.sha256,
        boards=boards,
        fence_check=lambda: None,
    )

    assert reconciled is not None and reconciled.to_dict() == completed.to_dict()
    assert successor is not None and successor.outcome == "completed"
    assert repeated is not None and repeated.to_dict() == successor.to_dict()
    assert successor.recovery_journal_ref == (
        f"grafx-global-discovery-recovery:{attempt_two}"
    )
    assert journal_one_path.read_bytes() == journal_one_bytes
    assert json.loads(journal_one_path.read_text(encoding="utf-8")) == journal_one
    journal_two = json.loads(
        (
            tmp_path
            / "quarantine"
            / "global-discovery"
            / run_id
            / "attempt-2"
            / "recovery_journal.json"
        ).read_text(encoding="utf-8")
    )
    assert journal_two["kind"] == "grafx_global_discovery_reconcile_predecessor"
    assert journal_two["ancestry"] == [[1, attempt_one]]
    assert journal_two["predecessor_attempt_id"] == attempt_one
    assert read_safe_active_generation(slot.legacy) == active_before

    # Retention may delete old terminal evidence, but the attempt owning the
    # currently active generation remains immutable even when it is not newest.
    for epoch in range(3, 7):
        directory = recovery._attempt_directory(
            slot.legacy,
            run_id=run_id,
            epoch=epoch,
        )
        directory.mkdir(parents=True)
    retention = recovery.reconcile_attempt_artifacts(
        run_id=run_id,
        known_attempt_ids=tuple(
            recovery_attempt_id(run_id, epoch) for epoch in range(1, 7)
        ),
        now=datetime.now(UTC),
        fence_check=lambda: None,
    )
    assert attempt_one in retention.retained_ids
    assert read_safe_active_generation(slot.legacy) == active_before
