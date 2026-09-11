from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import okto_pulse.core.ports.global_discovery_recovery_control as recovery_contract
import okto_pulse.community.adapters.coordination as coordination_module
import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    CommunityGrafxGlobalDiscoveryRecovery,
    CommunityGrafxGlobalDiscoveryFenceError,
    CommunityGrafxGlobalDiscoveryRecoveryError,
)
from okto_grafx import connect
from test_grafx_global_discovery_providers import _DatabaseSlot, _board_seed, _runtime, _tree_bytes
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityGlobalDiscoveryRecoveryNativeOperation,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    read_active_generation,
)
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterContention,
    GlobalDiscoveryWriterFenceLost,
    GlobalDiscoveryWriterLease,
)
from okto_pulse.core.kg.global_discovery_reindex import (
    GlobalDiscoveryReindexStatusStore,
    GlobalDiscoveryReindexer,
    ReindexAttempt,
)
from okto_pulse.core.kg.rebuild_generation import generate_kg_generation_id
from okto_pulse.core.kg.single_writer_lock import KGSingleWriterLock


def _required(owner: object, name: str):
    value = getattr(owner, name, None)
    assert value is not None, f"R5 contract is missing {name}"
    return value


def _build_recovery(live: Path, revalidate=lambda phase: None):
    slot = _DatabaseSlot(live)
    _runtime(slot).bootstrap()
    slot.close()
    return CommunityGrafxGlobalDiscoveryRecovery(
        lambda: live,
        lambda path: connect(path, vector_exact_scan_threshold=4096),
        slot.close,
        revalidate,
    )


def _boards():
    return (_board_seed("board-1", "node-1"),)


def test_native_operation_acquires_shared_lease_inside_sql_fence_and_releases_reverse(
) -> None:
    source = inspect.getsource(CommunityGlobalDiscoveryRecoveryNativeOperation.__call__)

    assert "attempt_id" in inspect.signature(
        CommunityGlobalDiscoveryRecoveryNativeOperation.__call__
    ).parameters
    assert "GlobalDiscoveryWriterLease.acquire" in source
    assert ".guard()" in source
    assert ".release()" in source
    assert "ladybug_writer_scope" not in source

    # R2: target the EXACT production call expression, not any string (a comment
    # mentioning ``recover_and_cutover`` must NOT satisfy this proof).  There must
    # be exactly one such physical-operation call, and the OLD seed-only entry
    # must NOT be invoked on the recovery collaborator anywhere in __call__.
    physical_expr = "self._recovery.recover_and_cutover("
    assert source.count(physical_expr) == 1, source
    assert "self._recovery.rebuild_candidate_and_cutover(" not in source, source

    slot_fence = source.index("fence_check()")
    lease_acquire = source.index("GlobalDiscoveryWriterLease.acquire")
    lease_guard = source.index(".guard()")
    retention_call = source.index("reconcile_attempt_artifacts")
    physical_call = source.index(physical_expr)
    lease_release = source.rindex(".release()")
    assert (
        slot_fence
        < lease_acquire
        < lease_guard
        < retention_call
        < physical_call
        < lease_release
    )

    # AST proof (mutation-resistant): the production non-deadline path really
    # *calls* ``self._recovery.recover_and_cutover`` — exactly once — forwarding
    # the exact physical kwargs, and never calls the seed-only entry on the
    # recovery collaborator.  Removing or renaming the real call, or swapping it
    # back to ``rebuild_candidate_and_cutover``, fails this assertion.
    tree = ast.parse(textwrap.dedent(source))

    def _recovery_method_calls(name: str) -> list[ast.Call]:
        found: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == name
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "_recovery"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "self"
            ):
                found.append(node)
        return found

    unified_calls = _recovery_method_calls("recover_and_cutover")
    assert len(unified_calls) == 1, ast.dump(tree)
    assert _recovery_method_calls("rebuild_candidate_and_cutover") == []

    # R2: assert the EXACT keyword VALUE expression for every forwarded kwarg —
    # not merely the set of names.  A mutant that keeps the name but swaps the
    # value (``fence_check=lambda: None``, or run/epoch/attempt/boards swapped)
    # changes these unparsed expressions and fails here.
    forwarded_values = {
        kw.arg: ast.unparse(kw.value) for kw in unified_calls[0].keywords
    }
    assert forwarded_values == {
        "run_id": "str(run_id)",
        "epoch": "int(epoch)",
        "attempt_id": "str(attempt_id)",
        "expected_live_sha256": "inputs.expected_live_sha256",
        "boards": "inputs.boards",
        "fence_check": "physical_fence_check",
    }, forwarded_values

    # R2: the SAME fence object (``physical_fence_check``) that guards the physical
    # cutover also fences the pre-op reconcile — proving one fence identity spans
    # reconcile + physical operation at the source level.  The reconciler is
    # resolved via ``getattr(self._recovery, "reconcile_attempt_artifacts", ...)``
    # and invoked through the local ``reconcile_artifacts`` name.
    reconcile_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reconcile_artifacts"
    ]
    assert len(reconcile_calls) == 1, ast.dump(tree)
    reconcile_fence = {
        kw.arg: ast.unparse(kw.value)
        for kw in reconcile_calls[0].keywords
        if kw.arg == "fence_check"
    }
    assert reconcile_fence == {"fence_check": "physical_fence_check"}, reconcile_fence
    # And the physical operation forwards the identical fence name.
    assert forwarded_values["fence_check"] == "physical_fence_check"


def test_recovery_lease_contends_with_the_normal_global_writer_lane(
    tmp_path: Path,
) -> None:
    port = CommunityLocalWriteLockPort()
    normal_lock = KGSingleWriterLock(base_dir=tmp_path, write_lock_port=port)
    recovery_lock = KGSingleWriterLock(base_dir=tmp_path, write_lock_port=port)
    normal = GlobalDiscoveryWriterLease.acquire(
        operation="global_discovery_normal_write",
        owner_id="normal-writer",
        lock=normal_lock,
    )
    try:
        with pytest.raises(GlobalDiscoveryWriterContention):
            GlobalDiscoveryWriterLease.acquire(
                operation="global_discovery_recovery",
                owner_id="recovery-writer",
                admin_lane=True,
                lock=recovery_lock,
            )
    finally:
        assert normal.release() is True

    recovered = GlobalDiscoveryWriterLease.acquire(
        operation="global_discovery_recovery",
        owner_id="recovery-writer",
        admin_lane=True,
        lock=recovery_lock,
    )
    assert recovered.release() is True


def test_recovery_writer_lease_renews_and_expires_before_sql_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [2_000_000_000.0]
    monkeypatch.setattr(coordination_module.time, "time", lambda: clock[0])
    ttl_seconds = _required(worker_module, "_RECOVERY_WRITER_LEASE_SECONDS")
    assert ttl_seconds * 1_000 < _required(
        recovery_contract,
        "RECOVERY_WORKER_LEASE_MS",
    )
    port = CommunityLocalWriteLockPort()
    lock = KGSingleWriterLock(base_dir=tmp_path, write_lock_port=port)
    lease = GlobalDiscoveryWriterLease.acquire(
        operation="global_discovery_recovery",
        owner_id="recovery-attempt-one",
        ttl_seconds=ttl_seconds,
        admin_lane=True,
        lock=lock,
    )
    before = lock.inspect(board_id="_global")
    assert before is not None

    clock[0] += 5
    lease.renew()
    renewed = lock.inspect(board_id="_global")
    assert renewed is not None
    assert renewed.owner_token == before.owner_token
    assert renewed.expires_at_epoch == clock[0] + ttl_seconds
    assert renewed.expires_at_epoch > before.expires_at_epoch
    assert (
        lock.renew(
            board_id="_global",
            owner_token="stale-token",
            ttl_seconds=ttl_seconds,
        )
        is False
    )

    clock[0] = renewed.expires_at_epoch + 0.001
    replacement = GlobalDiscoveryWriterLease.acquire(
        operation="global_discovery_recovery",
        owner_id="recovery-attempt-one-restart",
        ttl_seconds=ttl_seconds,
        admin_lane=True,
        lock=lock,
    )
    assert replacement.owner_token != lease.owner_token
    assert lease.release() is False
    assert replacement.release() is True


def test_injected_reindex_adapter_cannot_mutate_without_durable_global_lease(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def mutating_adapter(
        board_id: str,
        generation_id: str,
        refs: tuple[str, ...],
    ) -> ReindexAttempt:
        calls.append((board_id, generation_id, refs))
        return ReindexAttempt(success=True, indexed_generation=generation_id)

    reindexer = GlobalDiscoveryReindexer(
        status_store=GlobalDiscoveryReindexStatusStore(
            artifact_store=CommunityFileSystemRebuildAuditArtifactStore(
                tmp_path
            )
        ),
        reindex_adapter=mutating_adapter,
    )
    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        reindexer.reindex_or_mark_pending(
            board_id="board-reindex-fence",
            kg_generation_id=generate_kg_generation_id(),
            reason="operator_requested",
        )
    assert calls == []


def test_writer_lease_loss_before_pointer_replace_preserves_live_generation(
    tmp_path: Path,
) -> None:
    live = tmp_path / "global" / "discovery.grafx"
    adapter = _build_recovery(live)
    original = _tree_bytes(live)
    before = adapter.inspect_live_artifact()
    run_id = "gdr_r5_lease_loss"
    attempt_id = _required(recovery_contract, "recovery_attempt_id")(run_id, 1)

    def lost_lease() -> None:
        raise GlobalDiscoveryWriterFenceLost()

    with pytest.raises(
        CommunityGrafxGlobalDiscoveryFenceError
    ) as refused:
        adapter.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=1,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
            fence_check=lost_lease,
        )

    assert isinstance(refused.value.__cause__, GlobalDiscoveryWriterFenceLost)
    assert read_active_generation(live) is None
    assert _tree_bytes(live) == original


def test_late_fence_loss_preserves_completed_journal_for_authorized_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = tmp_path / "global" / "discovery.grafx"
    adapter = _build_recovery(live)
    before = adapter.inspect_live_artifact()
    run_id = "gdr_r5_terminal_truth"
    attempt_id = _required(recovery_contract, "recovery_attempt_id")(run_id, 1)
    completed_written = False
    write_journal = adapter._write_attempt_journal

    def tracking_write(*args, **kwargs):
        nonlocal completed_written
        document = write_journal(*args, **kwargs)
        payload = args[1]
        if payload.get("phase") == "completed":
            completed_written = True
        return document

    monkeypatch.setattr(
        adapter,
        "_write_attempt_journal",
        tracking_write,
    )

    def cancel_after_terminal() -> None:
        if completed_written:
            raise GlobalDiscoveryWriterFenceLost()

    arguments = dict(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=cancel_after_terminal,
    )
    adapter.rebuild_candidate_and_cutover(**arguments)
    # The Grafx leaf must not bypass authority for final native readback. Its
    # durable success remains reconcilable by an authorized worker afterwards.
    with pytest.raises(CommunityGrafxGlobalDiscoveryFenceError):
        adapter.recover_and_cutover(**arguments)
    assert completed_written is True
    active = read_active_generation(live)
    assert active is not None
    result = adapter.reconcile_attempt_terminal_truth(**{**arguments, "fence_check": lambda: None})
    assert result is not None
    assert result.outcome == "completed"
    assert read_active_generation(live) == active


@pytest.mark.parametrize(
    ("loss_phase", "pointer_replaced"),
    [("recovery_cutover", False), ("recovery_readback", True)],
)
def test_exact_fence_loss_preserves_publication_and_reconciles_safely(
    tmp_path: Path,
    loss_phase: str,
    pointer_replaced: bool,
) -> None:
    live = tmp_path / "global" / "discovery.grafx"
    lost = False

    def lose_at_phase(phase):
        nonlocal lost
        lost = lost or phase == loss_phase
        if lost:
            raise GlobalDiscoveryWriterFenceLost()

    adapter = _build_recovery(live, lose_at_phase)
    original = _tree_bytes(live)
    before = adapter.inspect_live_artifact()
    run_id = f"gdr_r5_fence_{loss_phase}"
    attempt_id = _required(recovery_contract, "recovery_attempt_id")(run_id, 1)
    with pytest.raises(
        CommunityGrafxGlobalDiscoveryFenceError
    ) as refused:
        adapter.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=1,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
            fence_check=lambda: None,
        )

    assert isinstance(refused.value.__cause__, GlobalDiscoveryWriterFenceLost)
    assert _tree_bytes(live) == original
    active = read_active_generation(live)
    assert (active is not None) is pointer_replaced
    # Grafx publishes a certified manifest before pointer replacement; unlike
    # the removed backend, intermediate phases are not mutable journal rows.
    manifests = list(live.parent.glob("discovery.generations/*/generation_manifest.json"))
    assert manifests
    adapter._revalidate_fence = lambda phase: None

    deadline_resolution = adapter.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    if pointer_replaced:
        assert deadline_resolution is not None
        assert deadline_resolution.outcome == "completed"
    else:
        # A certified candidate has not crossed the pointer boundary. Deadline
        # reconciliation must never turn it into fresh post-budget work.
        assert deadline_resolution is None
        assert read_active_generation(live) is None
        # An unpublished native candidate is retained, never overwritten by a
        # same-attempt retry. A successor epoch gets an independent generation.
        with pytest.raises(CommunityGrafxGlobalDiscoveryRecoveryError) as existing:
            adapter.rebuild_candidate_and_cutover(
                run_id=run_id, epoch=1, attempt_id=attempt_id,
                expected_live_sha256=before.sha256, boards=_boards(),
                fence_check=lambda: None,
            )
        assert existing.value.code == "global_discovery_candidate_generation_already_exists"
        attempt_id = recovery_contract.recovery_attempt_id(run_id, 2)

    reconciled = adapter.rebuild_candidate_and_cutover(
        run_id=run_id,
        epoch=1 if pointer_replaced else 2,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    assert reconciled.outcome == "completed"
    assert read_active_generation(live) is not None
