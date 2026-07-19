"""INV-E2 durable incomplete-bootstrap marker regressions.

Covers the write-ahead intent-log contract (Nexus
msg_08ef262ec7b744c496e742bb6b42d45a / msg_20533dbbce3741248416fc0e53b7ea4e)
and the Codex-validator ordering/authority blockers:

* marker persisted BEFORE any bootstrap-side filesystem action (before close);
* durable completion ordering (close -> fsync -> readback -> fsync -> clear),
  never page-cache readability;
* authority-checked, TOCTOU-free clear;
* ``state()`` marker precedence with a metadata-only ``primary_confirmed_absent``
  fact and no Core-typed marker-bypass method;
* ordinary open/auto-bootstrap refuses over a live marker (recovery-only truth);
* R1 mid-DDL cross-context unreadable and R4 healthy/absent-retry.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import ladybug  # noqa: F401 - ensures the native backend is importable
import pytest

from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters import global_discovery_schema
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    BOOTSTRAP_INCOMPLETE_MARKER_FILENAME,
    BOOTSTRAP_INCOMPLETE_REASON,
    BootstrapMarkerAuthorityError,
    bootstrap_marker_path,
    bootstrap_marker_present,
    clear_bootstrap_marker,
    read_bootstrap_marker,
    write_bootstrap_marker,
)
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterFenceLost,
    GlobalDiscoveryWriterLease,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
)


class _AlwaysOwnedWriterLock:
    def is_owner(self, _board_id: str, _owner_token: str) -> bool:
        return True

    def release(self, *, board_id: str, owner_token: str) -> bool:
        del board_id, owner_token
        return True


@contextmanager
def under_global_safe_write(owner_token: str, operation: str):
    lease = GlobalDiscoveryWriterLease(
        lock=_AlwaysOwnedWriterLock(),  # type: ignore[arg-type]
        owner_token=owner_token,
        operation=operation,
    )
    try:
        with lease.guard():
            yield
    finally:
        lease.release()


def _runtime(graph_path: Path) -> CommunityGlobalDiscoveryRuntime:
    return CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: graph_path)


def _bootstrap(runtime: CommunityGlobalDiscoveryRuntime, owner: str) -> None:
    with under_global_safe_write(owner, "test_bootstrap"):
        runtime.bootstrap()


# ---------------------------------------------------------------------------
# Marker module unit contract
# ---------------------------------------------------------------------------


def test_marker_path_is_adjacent_and_not_residue_prefixed(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    marker = bootstrap_marker_path(legacy)
    assert marker.parent == legacy.parent
    assert marker.name == BOOTSTRAP_INCOMPLETE_MARKER_FILENAME
    # Must not share the residue scan prefix ``discovery.lbug.``.
    assert not marker.name.startswith(legacy.name + ".")


def test_write_read_clear_roundtrip_is_bounded_and_durable(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    assert bootstrap_marker_present(legacy) is False
    write = write_bootstrap_marker(legacy)
    assert isinstance(write.directory_fsync_supported, bool)
    assert bootstrap_marker_present(legacy) is True
    payload = read_bootstrap_marker(legacy)
    assert set(payload) == {"created_at", "kind", "nonce"}
    assert payload["kind"] == "init_bootstrap"
    assert payload["nonce"] == write.nonce
    clear_bootstrap_marker(legacy, fence_check=lambda: None)
    assert bootstrap_marker_present(legacy) is False
    # Idempotent clear.
    clear_bootstrap_marker(legacy, fence_check=lambda: None)
    assert read_bootstrap_marker(legacy) is None


def test_clear_requires_mandatory_authority_callback(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(legacy)
    with pytest.raises(BootstrapMarkerAuthorityError):
        clear_bootstrap_marker(legacy, fence_check=None)  # type: ignore[arg-type]
    # A null authority never performs a silent clear.
    assert bootstrap_marker_present(legacy) is True

    # The callback is invoked immediately before the physical unlink.
    calls: list[str] = []

    def _fence() -> None:
        calls.append("fence")

    clear_bootstrap_marker(legacy, fence_check=_fence)
    assert calls == ["fence"]
    assert bootstrap_marker_present(legacy) is False


# ---------------------------------------------------------------------------
# state() precedence + primary_confirmed_absent detail (blockers 4, 7)
# ---------------------------------------------------------------------------


def test_marker_present_absent_primary_reports_bootstrap_incomplete_absent_true(
    tmp_path,
):
    legacy = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(legacy)
    runtime = _runtime(legacy)
    st = runtime.state()
    assert st.state == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    assert st.reason_code == BOOTSTRAP_INCOMPLETE_REASON
    assert st.quarantined is True
    assert st.details.get("primary_confirmed_absent") is True


def test_marker_present_with_partial_primary_reports_absent_false(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"partial-primary")  # a physical primary exists
    write_bootstrap_marker(legacy)
    runtime = _runtime(legacy)
    st = runtime.state()
    assert st.state == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    assert st.reason_code == BOOTSTRAP_INCOMPLETE_REASON
    assert st.quarantined is True
    # A present/partial primary is NOT a safe retry target.
    assert st.details.get("primary_confirmed_absent") is False


def test_marker_precedence_overrides_readable_pointer(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"looks-present")
    runtime = _runtime(legacy)
    # Without a marker a present primary is a readable candidate.
    assert (
        runtime.state().state
        == GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )
    # Marker existence overrides that readable classification.
    write_bootstrap_marker(legacy)
    assert (
        runtime.state().state
        == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    )


def test_marker_is_in_materialization_observation_paths(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _runtime(legacy)
    assert bootstrap_marker_path(legacy) in set(
        runtime.materialization_observation_paths()
    )


def test_no_core_marker_bypass_method_exposed():
    # Blocker 7: the general marker-bypass classifier must not be public.
    assert not hasattr(
        CommunityGlobalDiscoveryRuntime, "classify_primary_without_marker"
    )


# ---------------------------------------------------------------------------
# Blocker 1 - marker precedes close / all bootstrap-side FS actions
# ---------------------------------------------------------------------------


def test_marker_is_written_before_close_and_open(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"

    events: list[str] = []

    class _OrderingRuntime(CommunityGlobalDiscoveryRuntime):
        def _close_with_writer_lease(self) -> None:
            events.append(
                "close:marker=" + str(bootstrap_marker_present(legacy))
            )
            super()._close_with_writer_lease()

    runtime = _OrderingRuntime(graph_path_provider=lambda: legacy)
    _bootstrap(runtime, "ordering")
    # The first close inside bootstrap must observe the marker already present.
    assert events[0] == "close:marker=True"


def test_marker_survives_open_failure(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _runtime(legacy)

    def _boom(*_a, **_kw):
        raise RuntimeError("synthetic open failure")

    monkeypatch.setattr(runtime._runtime(), "open_kuzu_db", _boom)
    with under_global_safe_write("open-fail", "test"):
        with pytest.raises(Exception):
            runtime.bootstrap()
    assert bootstrap_marker_present(legacy) is True


# ---------------------------------------------------------------------------
# Blocker 2 - durable completion ordering, not page-cache readability
# ---------------------------------------------------------------------------


def test_durable_completion_ordering_close_fsync_readback_fsync_clear(
    tmp_path, monkeypatch
):
    legacy = tmp_path / "global" / "discovery.lbug"

    events: list[str] = []

    import okto_pulse.community.adapters.global_discovery_bootstrap_marker as marker_mod

    real_clear = marker_mod.clear_bootstrap_marker

    def _recording_clear(legacy_path, *, fence_check):
        events.append("clear")
        return real_clear(legacy_path, fence_check=fence_check)

    monkeypatch.setattr(marker_mod, "clear_bootstrap_marker", _recording_clear)

    class _RecordingRuntime(CommunityGlobalDiscoveryRuntime):
        def _fsync_global_artifacts_and_dir(self, path: Path) -> bool:
            events.append("fsync")
            return super()._fsync_global_artifacts_and_dir(path)

        def _readback_global_discovery_schema(self, path: Path) -> None:
            events.append("readback")
            super()._readback_global_discovery_schema(path)

    runtime = _RecordingRuntime(graph_path_provider=lambda: legacy)
    _bootstrap(runtime, "ordering-complete")
    # close (unrecorded) -> fsync -> readback -> fsync -> clear
    assert events == ["fsync", "readback", "fsync", "clear"]
    assert bootstrap_marker_present(legacy) is False


def test_marker_not_cleared_when_readback_fails(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"

    class _FailingReadbackRuntime(CommunityGlobalDiscoveryRuntime):
        def _readback_global_discovery_schema(self, path: Path) -> None:
            raise RuntimeError("synthetic readback failure")

    runtime = _FailingReadbackRuntime(graph_path_provider=lambda: legacy)
    with under_global_safe_write("readback-fail", "test"):
        with pytest.raises(RuntimeError, match="synthetic readback failure"):
            runtime.bootstrap()
    # Durable completion never reached: marker remains, state stays unreadable.
    assert bootstrap_marker_present(legacy) is True
    fresh = _runtime(legacy)
    assert (
        fresh.state().state
        == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    )


# ---------------------------------------------------------------------------
# Blocker 3 - authority-checked, TOCTOU-free clear
# ---------------------------------------------------------------------------


def test_cutover_revalidates_fence_before_unlink(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(legacy)
    runtime = _runtime(legacy)

    def _lost_fence() -> None:
        raise GlobalDiscoveryWriterFenceLost()

    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        runtime.note_successful_generation_cutover(
            active_path=legacy, fence_check=_lost_fence
        )
    # Fence lost at the boundary -> marker must NOT be cleared.
    assert bootstrap_marker_present(legacy) is True

    # A live fence clears it.
    runtime.note_successful_generation_cutover(
        active_path=legacy, fence_check=lambda: None
    )
    assert bootstrap_marker_present(legacy) is False


# ---------------------------------------------------------------------------
# Blocker 8 - ordinary open/auto-bootstrap refuses over a live marker
# ---------------------------------------------------------------------------


def test_ordinary_read_refuses_over_marker_and_does_not_clear_it(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(legacy)
    runtime = _runtime(legacy)
    with pytest.raises(RuntimeError, match="global_discovery_bootstrap_incomplete"):
        runtime.execute("MATCH (n) RETURN n LIMIT 1")
    assert bootstrap_marker_present(legacy) is True


def test_ordinary_write_refuses_over_marker_and_does_not_mutate(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(legacy)
    runtime = _runtime(legacy)
    with under_global_safe_write("ordinary-write", "test"):
        with pytest.raises(
            RuntimeError, match="global_discovery_bootstrap_incomplete"
        ):
            runtime.execute("CREATE (b:Board {board_id: 'x'})")
    assert bootstrap_marker_present(legacy) is True
    # No primary graph was auto-bootstrapped over the marker.
    assert not legacy.exists()


def test_direct_bootstrap_refuses_over_marker_plus_partial_primary(tmp_path):
    # Blocker 14: the recovery-only boundary is enforced at the bootstrap choke
    # point itself, not only via the CLI state() check.  A direct
    # runtime.bootstrap() under a writer token cannot overwrite/open/mutate/clear
    # a marker that sits over a present/partial primary.
    legacy = tmp_path / "global" / "discovery.lbug"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"partial-primary")
    write_bootstrap_marker(legacy)
    runtime = _runtime(legacy)
    with under_global_safe_write("direct-bootstrap", "test"):
        with pytest.raises(
            RuntimeError,
            match="global_discovery_bootstrap_refused_marker_present",
        ):
            runtime.bootstrap()
    # Zero mutation: partial primary bytes + marker both preserved.
    assert legacy.read_bytes() == b"partial-primary"
    assert bootstrap_marker_present(legacy) is True


# ---------------------------------------------------------------------------
# R1 - mid-DDL failure, fresh context observes unreadable (never readable)
# ---------------------------------------------------------------------------


def test_r1_mid_ddl_failure_is_unreadable_from_fresh_context(tmp_path, monkeypatch):
    legacy = tmp_path / "global" / "discovery.lbug"

    def _boom(_conn):
        raise RuntimeError("synthetic mid-DDL failure")

    monkeypatch.setattr(
        global_discovery_schema, "ensure_decision_digest_layer_column", _boom
    )
    runtime1 = _runtime(legacy)
    with under_global_safe_write("mid-ddl", "test"):
        with pytest.raises(RuntimeError, match="synthetic mid-DDL failure"):
            runtime1.bootstrap()
    # Marker present + a partial primary artifact exists.
    assert bootstrap_marker_present(legacy) is True
    assert legacy.exists()
    runtime1.close()
    del runtime1

    # Fresh runtime/context: durable evidence (not the process latch) drives it.
    runtime2 = _runtime(legacy)
    st = runtime2.state()
    assert st.state == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    assert st.reason_code == BOOTSTRAP_INCOMPLETE_REASON
    assert st.quarantined is True
    assert st.state != GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    # Partial primary -> not a safe init retry.
    assert st.details.get("primary_confirmed_absent") is False


# ---------------------------------------------------------------------------
# R4 - healthy no-marker has no false positive; marker+absent retried safely
# ---------------------------------------------------------------------------


def test_r4_healthy_bootstrap_has_no_marker_and_is_readable(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _runtime(legacy)
    _bootstrap(runtime, "healthy")
    runtime.close()
    assert bootstrap_marker_present(legacy) is False
    fresh = _runtime(legacy)
    assert (
        fresh.state().state
        == GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )


def test_r4_marker_plus_absent_primary_is_retried_by_bootstrap(tmp_path):
    legacy = tmp_path / "global" / "discovery.lbug"
    # Marker with no primary (previous process died before any artifact).
    write_bootstrap_marker(legacy)
    assert not legacy.exists()
    runtime = _runtime(legacy)
    # The explicit bootstrap (init retry) rewrites a fresh marker then clears it.
    _bootstrap(runtime, "absent-retry")
    runtime.close()
    assert bootstrap_marker_present(legacy) is False
    fresh = _runtime(legacy)
    assert (
        fresh.state().state
        == GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )


# ---------------------------------------------------------------------------
# Blocker 23 - warm-handle marker bypass: a marker published after warm-up
# makes every ordinary borrow refuse, zero open/mutation/clear.
# ---------------------------------------------------------------------------


def _graph_fingerprint(legacy: Path) -> tuple:
    # Stat-based (size + mtime): the warm handle holds an exclusive lock on the
    # open .lbug on Windows, so reading its bytes would fail; size+mtime still
    # proves zero mutation across the refused borrows.
    parent = legacy.parent
    entries = []
    for child in sorted(parent.glob(legacy.name + "*")):
        if child.is_file():
            info = child.stat()
            entries.append((child.name, info.st_size, info.st_mtime_ns))
    return tuple(entries)


@pytest.mark.parametrize("primary_present", [True, False])
def test_warm_handle_refuses_after_marker_published(
    tmp_path, monkeypatch, primary_present
):
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _runtime(legacy)
    _bootstrap(runtime, "warm")
    with under_global_safe_write("warm-read", "test"):
        runtime.execute("MATCH (n) RETURN n LIMIT 1")
    assert runtime._database_is_open()  # handle is warm
    graph_before = _graph_fingerprint(legacy)

    # Another process publishes a marker after this runtime warmed its handle;
    # the absent variant also removes the primary artifact (quarantined away).
    write_bootstrap_marker(legacy)
    if not primary_present:
        for child in sorted(legacy.parent.glob(legacy.name + "*")):
            if child.is_file():
                child.unlink()

    # Spy: no physical connection borrow may occur after the marker appears.
    borrows = {"n": 0}
    graph_runtime = runtime._runtime()
    real_new_conn = graph_runtime.new_connection

    def _counting_new_conn(db):
        borrows["n"] += 1
        return real_new_conn(db)

    monkeypatch.setattr(graph_runtime, "new_connection", _counting_new_conn)

    with pytest.raises(RuntimeError, match="global_discovery_bootstrap_incomplete"):
        runtime.execute("MATCH (n) RETURN n LIMIT 1")
    with under_global_safe_write("warm-write", "test"):
        with pytest.raises(
            RuntimeError, match="global_discovery_bootstrap_incomplete"
        ):
            runtime.execute("CREATE (b:Board {board_id: 'y'})")

    # Zero physical open/borrow, zero clear, and (when present) zero mutation.
    assert borrows["n"] == 0
    assert bootstrap_marker_present(legacy) is True
    if primary_present:
        assert _graph_fingerprint(legacy) == graph_before
    runtime.close()


# ---------------------------------------------------------------------------
# Blocker 24 - a non-already-exists vector-index DDL failure must propagate and
# preserve the marker (never silently cleared).
# ---------------------------------------------------------------------------


def test_vector_index_real_failure_preserves_marker(tmp_path, monkeypatch):
    # Inject a non-already-exists failure at the physical execute boundary so the
    # narrow catch must propagate it (patching a symbol the SUT does not bind
    # would be a false test).
    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _runtime(legacy)
    graph_runtime = runtime._runtime()
    real_new_conn = graph_runtime.new_connection

    class _Conn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, *args, **kwargs):
            if "CREATE_VECTOR_INDEX" in statement:
                raise RuntimeError("synthetic_vector_index_failure")
            return self._inner.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(
        graph_runtime, "new_connection", lambda db: _Conn(real_new_conn(db))
    )

    with under_global_safe_write("vec-fail", "test"):
        with pytest.raises(RuntimeError, match="synthetic_vector_index_failure"):
            runtime.bootstrap()
    # The exact injected error propagated (narrow catch did not swallow it) and
    # the marker is retained -> recovery-only state from a fresh context.
    assert bootstrap_marker_present(legacy) is True
    fresh = _runtime(legacy)
    st = fresh.state()
    assert st.state == GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    assert st.reason_code == BOOTSTRAP_INCOMPLETE_REASON


# ---------------------------------------------------------------------------
# Blocker 18/30 - directory-fsync durability evidence is threaded, not erased.
# Force false at each boundary and assert the observable aggregate is False.
# ---------------------------------------------------------------------------


def test_bootstrap_directory_fsync_evidence_all_true(tmp_path, monkeypatch):
    import okto_pulse.community.adapters.global_discovery_layout as layout_mod
    import okto_pulse.community.adapters.global_discovery_bootstrap_marker as mk

    monkeypatch.setattr(layout_mod, "fsync_directory", lambda _p: True)
    monkeypatch.setattr(mk, "fsync_directory", lambda _p: True)

    legacy = tmp_path / "global" / "discovery.lbug"
    runtime = _runtime(legacy)
    _bootstrap(runtime, "durable-all-true")
    assert runtime._bootstrap_directory_fsync_supported is True


@pytest.mark.parametrize(
    "boundary", ["marker_write", "fsync_first", "fsync_second", "clear"]
)
def test_bootstrap_directory_fsync_false_at_each_boundary(
    tmp_path, monkeypatch, boundary
):
    import okto_pulse.community.adapters.global_discovery_layout as layout_mod
    import okto_pulse.community.adapters.global_discovery_bootstrap_marker as mk

    # Baseline: every boundary would otherwise report support (Windows dir fsync
    # is naturally False, so pin the baseline True to isolate the injected one).
    monkeypatch.setattr(layout_mod, "fsync_directory", lambda _p: True)
    monkeypatch.setattr(mk, "fsync_directory", lambda _p: True)

    legacy = tmp_path / "global" / "discovery.lbug"

    if boundary == "marker_write":
        real = mk.write_bootstrap_marker

        def _false_write(legacy_path, *, nonce=None):
            w = real(legacy_path, nonce=nonce)
            return mk.BootstrapMarkerWrite(
                nonce=w.nonce, directory_fsync_supported=False
            )

        monkeypatch.setattr(mk, "write_bootstrap_marker", _false_write)
    elif boundary == "clear":
        real_clear = mk.clear_bootstrap_marker

        def _false_clear(legacy_path, *, fence_check):
            real_clear(legacy_path, fence_check=fence_check)
            return False

        monkeypatch.setattr(mk, "clear_bootstrap_marker", _false_clear)

    # Distinguish the FIRST (pre-readback) from the SECOND (post-readback)
    # artifact+dir fsync by call order.
    fsync_calls = {"n": 0}

    class _Runtime(CommunityGlobalDiscoveryRuntime):
        def _fsync_global_artifacts_and_dir(self, path):
            fsync_calls["n"] += 1
            result = super()._fsync_global_artifacts_and_dir(path)
            if boundary == "fsync_first" and fsync_calls["n"] == 1:
                return False
            if boundary == "fsync_second" and fsync_calls["n"] == 2:
                return False
            return result

    runtime = _Runtime(graph_path_provider=lambda: legacy)
    _bootstrap(runtime, f"durable-false-{boundary}")
    # Exactly two artifact+dir fsync boundaries run in _complete_bootstrap_durably.
    assert fsync_calls["n"] == 2
    # The injected false is retained in the observable aggregate evidence.
    assert runtime._bootstrap_directory_fsync_supported is False
    # A false durability flag must NOT block completion (the marker is still
    # cleared); it is retained as observable evidence only.
    assert bootstrap_marker_present(legacy) is False
