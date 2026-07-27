"""Worker-level proof (canonical point 1): the PRODUCTION recovery worker path
drives ``recover_and_cutover`` and correctly branches — a marked complete
primary is ADOPTED, a marked partial/incoherent primary falls back to
authoritative-seed rebuild.  A direct adapter-only R3 does not prove this.
"""

from __future__ import annotations

import json
from pathlib import Path

import ladybug  # noqa: F401
import pytest

import okto_pulse.community.adapters.global_discovery_recovery_worker as worker_module
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
)
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityGlobalDiscoveryRecoveryNativeOperation,
    RecoveryNativeInputs,
)
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    write_bootstrap_marker,
)
from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterLease
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryDigestSeed,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    RecoveryProgressCounts,
    RecoveryTerminalOutcome,
    recovery_attempt_id,
)

# Reuse the vetted fakes from the adapter suite.
from test_global_discovery_recovery_adapter import (  # noqa: E402
    _CandidateRuntime,
    _UnreadableLiveRuntime,
    _coherent_adopt_state,
)


class _AlwaysOwnedWorkerLock:
    def is_owner(self, _board_id, _owner_token):
        return True

    def renew(self, *, board_id, owner_token, ttl_seconds):
        del board_id, owner_token, ttl_seconds
        return True

    def release(self, *, board_id, owner_token):
        del board_id, owner_token
        return True


def _seed() -> GlobalDiscoveryBoardSeed:
    return GlobalDiscoveryBoardSeed(
        board_id="board-from-seed",
        board_name="Seed board",
        summary="summary",
        summary_embedding=(0.1, 0.2),
        digests=(
            GlobalDiscoveryDigestSeed(
                original_node_id="decision-seed",
                title="Decision",
                summary="digest",
                node_type="Decision",
                graph_layer="canonical",
                source_artifact_ref="artifact-seed",
                embedding=(0.3, 0.4),
            ),
        ),
        source_inventory_hash="sha256:inventory",
    )


def _drive_worker(monkeypatch, live: Path, factory, run_id: str, *, epoch: int = 2):
    """Drive the production native-operation dispatch (non-deadline) with a
    nontrivial epoch, exact attempt identity, a real counting fence, exact
    acquire kwargs, and single-lease continuity.  Return (result, calls, journal,
    proofs)."""

    global_runtime = _UnreadableLiveRuntime(live)
    real = CommunityGlobalDiscoveryRecovery(
        global_runtime=global_runtime,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    attempt_id = recovery_attempt_id(run_id, epoch)
    calls: list[str] = []
    forwarded: dict = {}

    class _SpyRecovery:
        def inspect_live_artifact(self):
            return real.inspect_live_artifact()

        def current_snapshot_fingerprint(self):
            return "sha256:worker-adoption-test"

        def reconcile_attempt_artifacts(self, **kwargs):
            kwargs["fence_check"]()
            calls.append("reconcile_artifacts")

        def rebuild_candidate_and_cutover(self, **kwargs):
            calls.append("rebuild_candidate_and_cutover")
            return real.rebuild_candidate_and_cutover(**kwargs)

        def recover_and_cutover(self, **kwargs):
            calls.append("recover_and_cutover")
            # Prove every forwarded kwarg reaches the physical operation.
            forwarded.update(
                {k: kwargs[k] for k in kwargs if k != "fence_check"}
            )
            forwarded["fence_check_callable"] = callable(kwargs.get("fence_check"))
            return real.recover_and_cutover(**kwargs)

    live_sha = real.inspect_live_artifact().sha256

    def _input_provider(*, run_id, epoch):
        return RecoveryNativeInputs(
            expected_live_sha256=live_sha,
            boards=(_seed(),),
            terminal_counts=RecoveryProgressCounts(
                sources_total=1,
                sources_processed=1,
                nodes_written=2,
                edges_written=1,
            ),
        )

    # Single-lease continuity + exact acquire kwargs capture.
    acquired = {"kwargs": None, "lease": None}

    def _acquire(**kwargs):
        assert acquired["lease"] is None, "acquire must be called exactly once"
        acquired["kwargs"] = dict(kwargs)
        acquired["lease"] = GlobalDiscoveryWriterLease(
            lock=_AlwaysOwnedWorkerLock(),  # type: ignore[arg-type]
            owner_token="worker-token",
            operation="global_discovery_recovery",
        )
        return acquired["lease"]

    monkeypatch.setattr(worker_module.GlobalDiscoveryWriterLease, "acquire", _acquire)

    # A real counting authority/deadline fence (not a no-op).
    fence_calls = {"n": 0}

    def counting_fence():
        fence_calls["n"] += 1

    operation = CommunityGlobalDiscoveryRecoveryNativeOperation(
        recovery=_SpyRecovery(),  # type: ignore[arg-type]
        input_provider=_input_provider,
    )
    result = operation(
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
        fence_check=counting_fence,
    )
    journal_path = (
        live.parent / "quarantine" / "global-discovery" / attempt_id
        / "recovery_journal.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    proofs = {
        "acquire_kwargs": acquired["kwargs"],
        "lease": acquired["lease"],
        "fence_calls": fence_calls["n"],
        "forwarded": forwarded,
        "attempt_id": attempt_id,
        "epoch": epoch,
        "expected_live_sha256": live_sha,
    }
    return result, calls, journal, proofs


def test_worker_marked_complete_primary_takes_adoption(tmp_path, monkeypatch):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared_state = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared_state)

    result, calls, journal, proofs = _drive_worker(
        monkeypatch, live, factory, "gdr_workeradopt", epoch=3
    )

    assert result.outcome is RecoveryTerminalOutcome.SUCCESS
    # The production worker invoked the unified recover_and_cutover, NOT the
    # seed-only rebuild.
    assert calls == ["reconcile_artifacts", "recover_and_cutover"]
    # A complete primary was ADOPTED (its exact content published).
    assert journal["kind"] == "adopt_complete_primary"
    assert journal["phase"] == "completed"

    # Exact acquire kwargs + admin lane + nontrivial epoch/attempt identity.
    assert proofs["acquire_kwargs"]["operation"] == "global_discovery_recovery"
    assert proofs["acquire_kwargs"]["admin_lane"] is True
    assert proofs["acquire_kwargs"]["owner_id"] == f"gdr_workeradopt:{proofs['attempt_id']}"
    assert proofs["epoch"] == 3
    assert proofs["attempt_id"] == recovery_attempt_id("gdr_workeradopt", 3)
    # The live fence was actually called; single lease continuity.
    assert proofs["fence_calls"] > 0
    assert proofs["lease"] is not None
    # Every forwarded kwarg reached the physical operation.
    assert proofs["forwarded"]["run_id"] == "gdr_workeradopt"
    assert proofs["forwarded"]["epoch"] == 3
    assert proofs["forwarded"]["attempt_id"] == proofs["attempt_id"]
    assert proofs["forwarded"]["expected_live_sha256"] == proofs["expected_live_sha256"]
    assert proofs["forwarded"]["fence_check_callable"] is True


def test_worker_marked_incoherent_primary_takes_seed_rebuild(tmp_path, monkeypatch):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"partial-primary")
    live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
    write_bootstrap_marker(live)

    factory_calls = {"n": 0}
    states: dict[Path, dict] = {}

    class _IncoherentAdoptRuntime(_CandidateRuntime):
        def __init__(self, path: Path) -> None:
            super().__init__(
                path,
                {
                    "boards": {
                        "board-x": {
                            "board_id": "board-x",
                            "name": "X",
                            "summary": "s",
                            "decision_count": 1,
                            "summary_embedding": [0.0, 0.0],
                        }
                    },
                    "digests": {},
                    "links": set(),
                },
            )

    def factory(path: Path):
        factory_calls["n"] += 1
        if factory_calls["n"] == 1:
            # Adoption copy open: schema-complete but structurally incoherent.
            return _IncoherentAdoptRuntime(path)
        return _CandidateRuntime(path, states.setdefault(path, {}))

    result, calls, journal, proofs = _drive_worker(
        monkeypatch, live, factory, "gdr_workerrebuild", epoch=2
    )

    assert result.outcome is RecoveryTerminalOutcome.SUCCESS
    assert calls == ["reconcile_artifacts", "recover_and_cutover"]
    # Incoherent primary -> authoritative-seed rebuild (NOT an adoption journal).
    assert journal.get("kind") != "adopt_complete_primary"
    assert journal["phase"] == "completed"
    # Exact acquire kwargs + nontrivial epoch + forwarded identity still hold.
    assert proofs["acquire_kwargs"]["admin_lane"] is True
    assert proofs["epoch"] == 2
    assert proofs["forwarded"]["epoch"] == 2
    assert proofs["fence_calls"] > 0
