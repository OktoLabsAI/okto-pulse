"""INV-E2 recovery-ceremony marker reconciliation regressions.

Exercises the COMPLETED-FIRST terminal order and the resume-with-marker floor
(Nexus msg_20533dbbce3741248416fc0e53b7ea4e / msg_08f6fa2df8ab4e728144e12e30ca7c67):

* R2: the recovery ceremony reconciles, publishes, clears the marker -> readable;
* F-a: a fault BEFORE the terminal `completed` journal rolls back, marker kept;
* F-b: a fault BETWEEN completed and clear leaves completed + active generation +
  marker; resume clears idempotently; a second resume is the same no-op;
* F-c: a fault DURING the physical clear behaves identically;
* F-mut: mutated generation bytes make every resume refuse (SHA mismatch),
  marker preserved, zero clear;
* F-happy: intact bytes clear exactly once, second resume idempotent no-op;
* both terminal entry points (rebuild_candidate_and_cutover AND
  reconcile_attempt_terminal_truth) run the same reconcile+clear helper.

Global invariant asserted throughout: there is never a partial legacy primary
without the marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_pulse.community.adapters.global_discovery_bootstrap_marker as marker_mod
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    bootstrap_marker_present,
    write_bootstrap_marker,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    read_active_generation,
    resolve_active_graph_path,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    recovery_attempt_id,
)

# Reuse the vetted candidate-runtime fake + board seeds from the adapter suite.
from test_global_discovery_recovery_adapter import (  # noqa: E402
    _CandidateRuntime,
    _boards,
)


def _raw_graph_inventory(graph: Path) -> dict[str, bytes]:
    """Complete raw inventory of a graph artifact set: the primary plus every
    ``graph-name.*`` sidecar — names/suffixes and exact bytes."""

    rows: dict[str, bytes] = {}
    if graph.is_file():
        rows[graph.name] = graph.read_bytes()
    for sibling in sorted(graph.parent.glob(graph.name + ".*")):
        if sibling.is_file():
            rows[sibling.name] = sibling.read_bytes()
    return rows


def _build_real_runtime_adapter(live: Path):
    """Adapter whose live runtime is a REAL runtime (real marker clear path).

    A5-freeze ruling: production's NON-MUTATING resume validation (approved B7)
    opens a BYTE-IDENTICAL scratch COPY of the active artifacts under
    ``quarantine/global-discovery/<id>/resume-validate-scratch-<uuid>/``.  The
    fixture keys semantic state by Path, so the factory is SCRATCH-AWARE for
    exactly that resolved location: it independently re-proves the copy is
    byte-identical to the active inventory and only then deep-copies the ACTIVE
    path's semantic state for the scratch path.  It never seeds from expected
    values, never shares the dict, never bootstraps and never falls back by
    filename or global state.
    """

    import copy as _copy

    global_runtime = CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: live)
    states: dict[Path, dict] = {}
    created: list[Path] = []

    def factory(path: Path):
        created.append(Path(path))
        resolved = Path(path).resolve()
        parent = resolved.parent
        ancestor_names = {ancestor.name for ancestor in resolved.parents}
        is_scratch = (
            parent.name.startswith("resume-validate-scratch-")
            and "global-discovery" in ancestor_names
            and "quarantine" in ancestor_names
            and resolved.is_relative_to(Path(live).resolve().parent)
        )
        if is_scratch:
            active = read_active_generation(live)
            assert active is not None, "scratch open without an active generation"
            active_graph = active.graph_path.resolve()
            assert resolved != active_graph, "scratch must never be the active"
            # Independent raw comparison: the scratch copy must be byte-exact
            # against the ACTIVE inventory before any semantic clone happens.
            assert _raw_graph_inventory(resolved) == _raw_graph_inventory(
                active_graph
            ), "scratch copy is not byte-identical to the active inventory"
            source_state = None
            for known_path, known_state in states.items():
                if Path(known_path).resolve() == active_graph:
                    source_state = known_state
                    break
            assert source_state is not None, (
                "no semantic state recorded for the active graph path"
            )
            cloned = _copy.deepcopy(source_state)
            states[resolved] = cloned
            return _CandidateRuntime(resolved, cloned)
        return _CandidateRuntime(path, states.setdefault(path, {}))

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=global_runtime,
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    # Test-only observability for the freeze-ruling oracles.
    adapter.test_created_paths = created  # type: ignore[attr-defined]
    return adapter, global_runtime


def _seed_live_partial_primary_with_marker(live: Path) -> str:
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"partial-primary")
    live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
    write_bootstrap_marker(live)
    return "seeded"


def _assert_never_partial_legacy_without_marker(live: Path) -> None:
    """Global INV-E2 invariant: a surviving legacy primary implies the marker."""

    if live.exists():
        assert bootstrap_marker_present(live) is True


# ---------------------------------------------------------------------------
# R2 - ceremony reconciles + clears marker -> readable
# ---------------------------------------------------------------------------


def test_r2_ceremony_clears_marker_and_becomes_readable(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = adapter.inspect_live_artifact()

    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_r2ceremony",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )

    assert result.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    # Pointer now resolves to a published generation and the marker is gone.
    assert resolve_active_graph_path(live) != live
    assert (
        global_runtime.state().state
        == GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )


# ---------------------------------------------------------------------------
# F-a - fault before completed -> rollback, marker preserved
# ---------------------------------------------------------------------------


def test_fa_fault_at_terminal_completed_write_rolls_back_and_keeps_marker(
    tmp_path, monkeypatch
):
    """F-a: fault exactly at the terminal ``completed`` journal write.

    Blocker 15: the fault must land at the mandatory terminal transition (after
    ``readback_validated``), not at an earlier candidate validation.  Because
    ``journal = completed`` is assigned only after that durable write succeeds,
    the failure is a *pre-completed* failure and MUST follow the normal rollback
    path: previous pointer/bytes restored, marker preserved, never a partial
    legacy without the marker.
    """

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    live_bytes_before = live.read_bytes()
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = adapter.inspect_live_artifact()

    real_write = rec_mod._write_journal_with_directory_fsync

    def fault_completed_write(path, payload, *, fence_check=None):
        if payload.get("phase") == "completed":
            raise RuntimeError("F-a fault at terminal completed write")
        return real_write(path, payload, fence_check=fence_check)

    monkeypatch.setattr(
        rec_mod, "_write_journal_with_directory_fsync", fault_completed_write
    )

    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_faaa",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )

    # Pre-completed failure => normal rollback path (not a raise, not completed).
    assert result.outcome == "rolled_back"
    # Previous pointer/bytes restored: no active generation existed before, so
    # the pointer rolls back to the legacy primary, whose bytes are intact.
    assert resolve_active_graph_path(live) == live
    assert live.read_bytes() == live_bytes_before
    # Marker preserved; never a partial legacy without a marker.
    assert bootstrap_marker_present(live) is True
    _assert_never_partial_legacy_without_marker(live)


# ---------------------------------------------------------------------------
# F-b / F-c - fault between completed and clear (and during clear)
# ---------------------------------------------------------------------------


def _run_completed_but_block_clear(adapter, live, run_id, *, block):
    """Drive a run to durable `completed` but fail the marker clear once."""

    before = adapter.inspect_live_artifact()
    with pytest.raises(Exception):
        with block():
            adapter.rebuild_candidate_and_cutover(
                run_id=run_id,
                expected_live_sha256=before.sha256,
                boards=_boards(),
            )


def test_fb_fault_between_completed_and_clear_resumes_idempotently(
    tmp_path, monkeypatch
):
    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = adapter.inspect_live_artifact()

    calls = {"n": 0}
    real_note = global_runtime.note_successful_generation_cutover

    def flaky_note(*, active_path, fence_check):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("F-b crash between completed and clear")
        return real_note(active_path=active_path, fence_check=fence_check)

    monkeypatch.setattr(global_runtime, "note_successful_generation_cutover", flaky_note)

    # Run 1: completed journal is durable, but the clear crashes.
    with pytest.raises(RuntimeError, match="F-b crash"):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_fbbb",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    # Terminal completed + validated active generation + marker preserved.
    assert bootstrap_marker_present(live) is True
    active = read_active_generation(live)
    assert active is not None

    # Resume: reconcile validates intact bytes and clears exactly once.
    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_fbbb",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert bootstrap_marker_present(live) is False

    # Second resume: idempotent no-op, still readable, marker still absent.
    result2 = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_fbbb",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result2.outcome == "completed"
    assert bootstrap_marker_present(live) is False


def test_fc_fault_at_physical_clear_after_fence_resumes_idempotently(
    tmp_path, monkeypatch
):
    """F-c (blocker 32): fault the physical clear AFTER the mandatory fence
    callback, inside the authority-checked helper — distinct from F-b, which
    faults ``note_successful`` before the clear is reached."""

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = adapter.inspect_live_artifact()

    import json
    import pathlib

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
        BOOTSTRAP_INCOMPLETE_MARKER_FILENAME,
    )

    counters = {"snapshot": 0, "physical_unlink": 0, "fence_before_unlink": 0}
    real_snapshot = rec_mod._snapshot

    def counting_snapshot(path, *, fence_check=None):
        counters["snapshot"] += 1
        return real_snapshot(path, fence_check=fence_check)

    monkeypatch.setattr(rec_mod, "_snapshot", counting_snapshot)

    # The mandatory fence callback in clear_bootstrap_marker runs BEFORE the
    # physical unlink; record that it fired.
    real_clear = marker_mod.clear_bootstrap_marker

    def fence_recording_clear(legacy_path, *, fence_check):
        def recording_fence():
            counters["fence_before_unlink"] += 1
            return fence_check()

        return real_clear(legacy_path, fence_check=recording_fence)

    monkeypatch.setattr(marker_mod, "clear_bootstrap_marker", fence_recording_clear)

    # Fault the EXACT physical unlink seam: the first attempt on the marker file
    # fails (after the fence), the retry (resume) performs exactly one real
    # unlink.
    real_unlink = pathlib.Path.unlink
    fail = {"on": True}

    def patched_unlink(self, *args, **kwargs):
        if self.name == BOOTSTRAP_INCOMPLETE_MARKER_FILENAME:
            counters["physical_unlink"] += 1
            if fail["on"]:
                fail["on"] = False
                raise PermissionError("F-c physical unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", patched_unlink)

    with pytest.raises(PermissionError, match="F-c physical unlink failure"):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_fccc",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    # Fence fired before the unlink; the physical unlink attempt failed ->
    # marker preserved, no rollback, terminal completed durable.
    assert counters["fence_before_unlink"] >= 1
    assert counters["physical_unlink"] == 1
    assert bootstrap_marker_present(live) is True
    active = read_active_generation(live)
    assert active is not None
    journal_path = (
        live.parent / "quarantine" / "global-discovery" / "gdr_fccc"
        / "recovery_journal.json"
    )
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert persisted["phase"] == "completed"

    # First resume: full re-conquest (validation runs) + exactly ONE real
    # physical unlink.
    counters["snapshot"] = 0
    counters["physical_unlink"] = 0
    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_fccc",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert result.rollback_performed is False
    assert bootstrap_marker_present(live) is False
    assert counters["snapshot"] >= 1
    assert counters["physical_unlink"] == 1

    # Second resume: zero validation/runtime construction and zero physical
    # unlink.
    counters["snapshot"] = 0
    counters["physical_unlink"] = 0
    result2 = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_fccc",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result2.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert counters["snapshot"] == 0
    assert counters["physical_unlink"] == 0


# ---------------------------------------------------------------------------
# F-mut / F-happy - resume-with-marker re-conquers cutover evidence (blocker 9)
# ---------------------------------------------------------------------------


def _drive_to_completed_with_marker(adapter, global_runtime, live, run_id, monkeypatch):
    """Reach durable `completed` while deliberately skipping the marker clear."""

    before = adapter.inspect_live_artifact()

    def refuse_clear(*, active_path, fence_check):
        raise RuntimeError("hold-clear")

    monkeypatch.setattr(
        global_runtime, "note_successful_generation_cutover", refuse_clear
    )
    with pytest.raises(RuntimeError, match="hold-clear"):
        adapter.rebuild_candidate_and_cutover(
            run_id=run_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    monkeypatch.undo()
    assert bootstrap_marker_present(live) is True
    return before


def test_fmut_mutated_generation_refuses_every_resume_and_keeps_marker(
    tmp_path, monkeypatch
):
    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = _drive_to_completed_with_marker(
        adapter, global_runtime, live, "gdr_fmut", monkeypatch
    )

    # F-mut: corrupt the published active generation bytes.
    active = read_active_generation(live)
    assert active is not None
    active.graph_path.write_bytes(b"tampered-generation-bytes")

    # First resume refuses on the exact-SHA re-conquest; zero clear.
    with pytest.raises(Exception, match="candidate_sha_mismatch"):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_fmut",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True

    # Second resume refuses identically; marker still preserved.
    with pytest.raises(Exception, match="candidate_sha_mismatch"):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_fmut",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True


def test_fhappy_intact_generation_clears_once_then_noop(tmp_path, monkeypatch):
    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = _drive_to_completed_with_marker(
        adapter, global_runtime, live, "gdr_fhappy", monkeypatch
    )

    # F-happy: intact bytes -> resume validates and clears exactly once.
    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_fhappy",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert bootstrap_marker_present(live) is False

    # Second resume: idempotent no-op result, marker still absent.
    result2 = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_fhappy",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result2.outcome == "completed"
    assert bootstrap_marker_present(live) is False


# ---------------------------------------------------------------------------
# Blocker 11 - both terminal entry points converge on the same reconcile+clear
# ---------------------------------------------------------------------------


def test_reconcile_attempt_terminal_truth_also_clears_marker(tmp_path, monkeypatch):
    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)

    run_id = "gdr_bothentry"
    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)

    # Reach durable `completed` (attempt-scoped journal) while holding the clear.
    before = adapter.inspect_live_artifact()

    def refuse_clear(*, active_path, fence_check):
        raise RuntimeError("hold-clear")

    monkeypatch.setattr(
        global_runtime, "note_successful_generation_cutover", refuse_clear
    )
    with pytest.raises(RuntimeError, match="hold-clear"):
        adapter.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    monkeypatch.undo()
    assert bootstrap_marker_present(live) is True

    # The R5-worker entry point must run the SAME reconcile+clear helper.
    result = adapter.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    assert result is not None
    assert result.outcome == "completed"
    assert bootstrap_marker_present(live) is False

    # Double resume through the same entry point is an idempotent no-op.
    result2 = adapter.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    assert result2 is not None
    assert result2.outcome == "completed"
    assert bootstrap_marker_present(live) is False


# ---------------------------------------------------------------------------
# Blocker 13 - marker-gated reconcile: first resume clears once, second is a
# true no-op (zero runtime construction/validation/fsync/clear) for BOTH
# terminal entry points.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["rebuild", "reconcile"])
def test_blocker13_second_resume_does_zero_validation_and_clear(
    tmp_path, monkeypatch, entry
):
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)

    run_id = "gdr_b13" + ("rebuild" if entry == "rebuild" else "reconc")
    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    before = adapter.inspect_live_artifact()

    # Reach durable `completed` while holding the clear so the marker persists.
    def refuse_clear(*, active_path, fence_check):
        raise RuntimeError("hold-clear")

    monkeypatch.setattr(
        global_runtime, "note_successful_generation_cutover", refuse_clear
    )
    with pytest.raises(RuntimeError, match="hold-clear"):
        adapter.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    monkeypatch.undo()
    assert bootstrap_marker_present(live) is True

    # Instrument the heavy re-conquest primitives + the physical clear.
    counters = {"snapshot": 0, "clear": 0}
    real_snapshot = rec_mod._snapshot
    real_clear = marker_mod.clear_bootstrap_marker

    def counting_snapshot(path, *, fence_check=None):
        counters["snapshot"] += 1
        return real_snapshot(path, fence_check=fence_check)

    def counting_clear(legacy_path, *, fence_check):
        counters["clear"] += 1
        return real_clear(legacy_path, fence_check=fence_check)

    monkeypatch.setattr(rec_mod, "_snapshot", counting_snapshot)
    monkeypatch.setattr(marker_mod, "clear_bootstrap_marker", counting_clear)

    def _resume():
        if entry == "rebuild":
            return adapter.rebuild_candidate_and_cutover(
                run_id=run_id,
                epoch=epoch,
                attempt_id=attempt_id,
                expected_live_sha256=before.sha256,
                boards=_boards(),
            )
        return adapter.reconcile_attempt_terminal_truth(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
            fence_check=lambda: None,
        )

    # First resume (marker present): full re-conquest + exactly one clear.
    r1 = _resume()
    assert r1 is not None and r1.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert counters["snapshot"] >= 1
    assert counters["clear"] == 1

    # Second resume (marker absent): zero validation, zero clear.
    counters["snapshot"] = 0
    counters["clear"] = 0
    r2 = _resume()
    assert r2 is not None and r2.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert counters["snapshot"] == 0
    assert counters["clear"] == 0


# ---------------------------------------------------------------------------
# Blocker 18/30 (recovery side) - directory-fsync evidence is threaded through
# the completed journal write and both completed-resume clear callers into the
# typed result, never silently erased.
# ---------------------------------------------------------------------------


def _patch_all_fsync_true(monkeypatch):
    """Pin every directory-fsync boundary True so injected falses are isolated
    (Windows dir fsync is naturally False)."""

    import okto_pulse.community.adapters.global_discovery_layout as layout_mod
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    monkeypatch.setattr(layout_mod, "fsync_directory", lambda _p: True)
    monkeypatch.setattr(rec_mod, "fsync_directory", lambda _p: True)
    monkeypatch.setattr(marker_mod, "fsync_directory", lambda _p: True)


@pytest.mark.parametrize("completed_write_supported", [True, False])
def test_completed_journal_write_fsync_threaded_to_result(
    tmp_path, monkeypatch, completed_write_supported
):
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = adapter.inspect_live_artifact()
    _patch_all_fsync_true(monkeypatch)

    real_write = rec_mod._write_journal_with_directory_fsync

    def _write(path, payload, *, fence_check=None):
        supported = real_write(path, payload, fence_check=fence_check)
        if payload.get("phase") == "completed":
            return completed_write_supported
        return supported

    monkeypatch.setattr(rec_mod, "_write_journal_with_directory_fsync", _write)

    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_fsjrnl",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    # The terminal-journal writer's durability result is retained in the result.
    assert result.directory_fsync_supported is completed_write_supported


@pytest.mark.parametrize("entry", ["rebuild", "reconcile"])
@pytest.mark.parametrize("clear_supported", [True, False])
def test_resume_clear_reports_sticky_false_never_rebounds_true(
    tmp_path, monkeypatch, entry, clear_supported
):
    """R4 sticky-false: after a held clear leaves a durable PENDING completed
    journal (whose on-disk ``directory_fsync_supported`` is a conservative
    ``false``), a RESUME re-conquers and clears but reports ``false`` — it must
    NEVER rebound to ``true`` even when the resumed clear itself returns ``true``
    and every current fsync is patched true.  A persisted false is sticky."""

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    _patch_all_fsync_true(monkeypatch)

    run_id = "gdr_rcf" + ("rebuild" if entry == "rebuild" else "reconc")
    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    before = adapter.inspect_live_artifact()

    real_note = global_runtime.note_successful_generation_cutover
    hold = {"on": True}

    def note_wrapper(*, active_path, fence_check):
        if hold["on"]:
            raise RuntimeError("hold-clear")
        real_note(active_path=active_path, fence_check=fence_check)
        return clear_supported

    monkeypatch.setattr(
        global_runtime, "note_successful_generation_cutover", note_wrapper
    )

    # Drive to a durable PENDING completed journal + marker, holding the clear.
    with pytest.raises(RuntimeError, match="hold-clear"):
        adapter.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True
    # The pending completed journal persisted a conservative false, NOT an
    # optimistic true (even though every fsync boundary was patched true).
    import json as _json

    journal_path = (
        live.parent / "quarantine" / "global-discovery" / attempt_id
        / "recovery_journal.json"
    )
    pending = _json.loads(journal_path.read_text(encoding="utf-8"))
    assert pending["phase"] == "completed"
    assert pending["directory_fsync_supported"] is False
    assert pending.get("clear_settled") is False

    # Resume: the clear now returns the parametrized durability; the result is
    # STICKY FALSE regardless — the persisted false never rebounds to true.
    hold["on"] = False
    if entry == "rebuild":
        result = adapter.rebuild_candidate_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    else:
        result = adapter.reconcile_attempt_terminal_truth(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
            fence_check=lambda: None,
        )
    assert result is not None and result.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert result.directory_fsync_supported is False


def test_r4_mutant_no_optimistic_true_persisted_at_any_replace_boundary(
    tmp_path, monkeypatch
):
    """R4 MANDATED mutant test: intercept the atomic journal writer so EVERY
    replace performs the real write, then returns False (its own directory fsync
    unsupported), simulating immediate process death right after the replace and
    BEFORE any correction.  Inspecting the file a fresh reader would see after
    each replace, a PENDING completed, a SETTLE completed, and a POINTER_SWITCHED
    journal must NEVER expose a trusted ``directory_fsync_supported=True`` — even
    though every other fsync boundary is patched true so the in-memory aggregate
    WANTS true.  A mutant that persisted the optimistic aggregate would fail
    here; a two-write correction is never relied upon."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    import json as _json

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    # Every OTHER fsync boundary reports true, so the in-memory aggregate is true
    # and a mutant WOULD have a true to (wrongly) persist.
    _patch_all_fsync_true(monkeypatch)

    real_write = rec_mod._write_journal_with_directory_fsync
    # (phase, clear_settled, directory_fsync_supported) a fresh reader sees AFTER
    # each atomic replace (i.e. if the process died immediately, returning False).
    fresh_reader_snapshots: list[tuple] = []

    def intercept(path, payload, *, fence_check=None):
        real_write(path, payload, fence_check=fence_check)
        on_disk = _json.loads(Path(path).read_text(encoding="utf-8"))
        fresh_reader_snapshots.append(
            (
                on_disk.get("phase"),
                on_disk.get("clear_settled"),
                on_disk.get("directory_fsync_supported"),
            )
        )
        return False  # this write's own directory fsync is unsupported

    monkeypatch.setattr(
        rec_mod, "_write_journal_with_directory_fsync", intercept
    )

    before = adapter.inspect_live_artifact()
    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_r4mutant",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"

    boundary = [
        snap
        for snap in fresh_reader_snapshots
        if snap[0] in ("completed", "pointer_switched", "prepared")
    ]
    # The boundaries were actually exercised (mutation-resistant: not vacuous).
    phases = {snap[0] for snap in boundary}
    assert "completed" in phases  # pending + settle
    assert "pointer_switched" in phases
    # No replace ever left a trusted optimistic true on disk.
    for phase, clear_settled, dfs in boundary:
        assert dfs is False, (phase, clear_settled, dfs)
    # A settle recording clear_settled=True still persisted directory fsync false.
    settle = [s for s in boundary if s[0] == "completed" and s[1] is True]
    assert settle, boundary
    for _phase, _cs, dfs in settle:
        assert dfs is False
    # Every writer returned false, so the in-process result is also false.
    assert result.directory_fsync_supported is False


# ---------------------------------------------------------------------------
# R4 - crash-conservative clear: every crash point defaults false
# ---------------------------------------------------------------------------


def test_r4_crash_after_physical_clear_before_settle_reports_false(
    tmp_path, monkeypatch
):
    """R4: a crash AFTER the physical marker clear but BEFORE the final settle
    leaves ``clear_settled=False`` on disk.  A second resume (marker absent) MUST
    report false EVEN THOUGH every CURRENT fsync succeeds — proving the false came
    from the durable pending journal, not the live monkeypatches (R4 point 6)."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = adapter.inspect_live_artifact()
    _patch_all_fsync_true(monkeypatch)

    real_write = rec_mod._write_journal_with_directory_fsync

    def fault_settle_write(path, payload, *, fence_check=None):
        # The pending write (clear_settled False) and the physical clear run
        # first; fault ONLY the final settle write (clear_settled True), i.e. a
        # crash after the marker is physically gone but before durable settle.
        if payload.get("clear_settled") is True:
            raise RuntimeError("R4 crash after clear before settle")
        return real_write(path, payload, fence_check=fence_check)

    monkeypatch.setattr(
        rec_mod, "_write_journal_with_directory_fsync", fault_settle_write
    )
    with pytest.raises(RuntimeError, match="R4 crash after clear before settle"):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_r4settle",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    # The physical clear DID happen (marker gone) but the settle never persisted.
    assert bootstrap_marker_present(live) is False

    # Restore writes; EVERY current fsync boundary now succeeds.
    monkeypatch.setattr(rec_mod, "_write_journal_with_directory_fsync", real_write)
    _patch_all_fsync_true(monkeypatch)

    # Second resume: marker absent + clear_settled False -> conservatively false.
    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_r4settle",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert result.directory_fsync_supported is False


def test_r4_fresh_clear_false_then_second_noop_stays_false(tmp_path, monkeypatch):
    """R4: a fresh completion whose clear directory-fsync is unsupported records
    false; a SECOND resume/no-op must ALSO report false — never upgrade to true
    even when the clear would now succeed (proves the durable false persisted)."""

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    before = adapter.inspect_live_artifact()
    _patch_all_fsync_true(monkeypatch)

    real_note = global_runtime.note_successful_generation_cutover

    def note_clear_false(*, active_path, fence_check):
        real_note(active_path=active_path, fence_check=fence_check)
        return False  # clear's own directory fsync unsupported

    monkeypatch.setattr(
        global_runtime, "note_successful_generation_cutover", note_clear_false
    )
    result1 = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_r4freshfalse",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result1.outcome == "completed"
    assert result1.directory_fsync_supported is False
    assert bootstrap_marker_present(live) is False

    # Restore note to return True; the second no-op must NOT be fooled into true.
    monkeypatch.setattr(
        global_runtime, "note_successful_generation_cutover", real_note
    )
    result2 = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_r4freshfalse",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result2.outcome == "completed"
    assert result2.directory_fsync_supported is False


# ---------------------------------------------------------------------------
# A5-freeze ruling — scratch-clone oracles (explicit)
# ---------------------------------------------------------------------------


def test_freeze_ruling_scratch_clone_oracles(tmp_path):
    """A5-freeze ruling oracles for the scratch-aware fixture clone:
    (a) a completed+marker RESUME constructs its validation runtime over the
    SCRATCH path and never over the active graph path; (b) the copied bytes are
    exact (asserted inside the clone) and the scratch is removed after the
    resume; (c) the active raw state stays unchanged except the expected marker
    clear/settle; (d) a tampered candidate SHA is rejected BEFORE any
    clone/factory call, with marker and raw bytes preserved; (e) a second
    marker-absent resume performs zero factory constructions, zero validation
    and zero clear."""

    import json as _json

    from okto_pulse.community.adapters.global_discovery_layout import (
        canonical_sha256 as _canon,
    )
    from okto_pulse.community.adapters.global_discovery_recovery import (
        CommunityGlobalDiscoveryRecoveryError,
        _journal_binding,
    )

    live = tmp_path / "global" / "discovery.lbug"
    _seed_live_partial_primary_with_marker(live)
    adapter, global_runtime = _build_real_runtime_adapter(live)
    run_id = "gdr_frzoracle"
    attempt_id = recovery_attempt_id(run_id, 1)
    before = adapter.inspect_live_artifact()

    built = adapter.rebuild_candidate_and_cutover(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert built.outcome == "completed"
    assert bootstrap_marker_present(live) is False

    active = read_active_generation(live)
    assert active is not None
    active_graph = active.graph_path.resolve()
    journal_dir = (
        live.parent / "quarantine" / "global-discovery" / attempt_id
    )
    journal_path = journal_dir / "recovery_journal.json"

    def _active_raw():
        rows = {}
        pointer = live.parent / "active_generation.json"
        rows["pointer"] = pointer.read_bytes()
        for name, data in sorted(
            _raw_graph_inventory(active.graph_path).items()
        ):
            rows[name] = data
        return rows

    # (a)+(b)+(c): restore the marker and RESUME — the validation runtime is
    # constructed over the SCRATCH path only, the scratch is removed after, and
    # the active raw state is unchanged (the marker clear is the only change).
    write_bootstrap_marker(live)
    raw_before = _active_raw()
    created_before = len(adapter.test_created_paths)
    resumed = adapter.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    assert resumed is not None and resumed.outcome == "completed"
    assert bootstrap_marker_present(live) is False  # expected clear happened
    resume_created = [
        Path(p).resolve()
        for p in adapter.test_created_paths[created_before:]
    ]
    assert resume_created, "the resume must construct a validation runtime"
    for constructed in resume_created:
        assert constructed != active_graph
        assert constructed.parent.name.startswith("resume-validate-scratch-")
    assert not list(journal_dir.glob("resume-validate-scratch*"))
    assert _active_raw() == raw_before

    # (d): tampered candidate SHA is rejected BEFORE any clone/factory call —
    # marker preserved, raw preserved, journal bytes preserved.
    journal = _json.loads(journal_path.read_text(encoding="utf-8"))
    original_journal_bytes = journal_path.read_bytes()
    forged = {**journal, "candidate_sha256": "f" * 64}
    binding = _journal_binding(forged)
    forged = {**binding, "journal_sha256": _canon(binding)}
    journal_path.write_text(
        _json.dumps(forged, sort_keys=True, indent=2), encoding="utf-8"
    )
    write_bootstrap_marker(live)
    tampered_raw = _active_raw()
    tampered_journal_bytes = journal_path.read_bytes()
    created_before_tamper = len(adapter.test_created_paths)
    try:
        adapter.reconcile_attempt_terminal_truth(
            run_id=run_id,
            epoch=1,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
            fence_check=lambda: None,
        )
        raise AssertionError("tampered candidate SHA must be rejected")
    except CommunityGlobalDiscoveryRecoveryError as exc:
        assert "candidate_sha" in str(exc)
    assert len(adapter.test_created_paths) == created_before_tamper
    assert bootstrap_marker_present(live) is True
    assert _active_raw() == tampered_raw
    assert journal_path.read_bytes() == tampered_journal_bytes
    assert not list(journal_dir.glob("resume-validate-scratch*"))

    # Restore the authentic journal and converge again (clears the marker).
    journal_path.write_bytes(original_journal_bytes)
    healed = adapter.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    assert healed is not None and healed.outcome == "completed"
    assert bootstrap_marker_present(live) is False

    # (e): a SECOND marker-absent resume is a pure no-op — zero factory
    # constructions, zero validation, zero clear (journal bytes stable).
    journal_after = journal_path.read_bytes()
    created_before_noop = len(adapter.test_created_paths)
    noop = adapter.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=1,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    assert noop is not None and noop.outcome == "completed"
    assert len(adapter.test_created_paths) == created_before_noop
    assert bootstrap_marker_present(live) is False
    assert journal_path.read_bytes() == journal_after
