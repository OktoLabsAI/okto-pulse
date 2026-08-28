from __future__ import annotations

import asyncio
import inspect
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryArtifactSnapshot,
    GlobalDiscoveryCutoverResult,
    GlobalDiscoveryRecovery,
)
from okto_pulse.core.kg.interfaces.global_discovery_runtime import (
    GlobalDiscoveryRuntime,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeObservationState,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecoveryFenceError,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    CommunityGrafxGlobalDiscoveryFenceError,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.routed_global_discovery import (
    CommunityGlobalDiscoveryRuntimeOperationSession,
    CommunityRoutedGlobalDiscoveryRecovery,
    CommunityRoutedGlobalDiscoveryRuntime,
)


def _snapshot(
    tmp_path: Path, *, backend: str = "ladybug"
) -> CommunityGraphRouteSnapshot:
    anchor = (
        tmp_path
        / "global"
        / ("discovery.lbug" if backend == "ladybug" else "discovery.grafx")
    )
    return CommunityGraphRouteSnapshot(
        scope="global",
        scope_id="global",
        backend=backend,  # type: ignore[arg-type]
        generation="generation-1",
        binding_path=tmp_path / "global" / "graph_backend_binding.json",
        anchor_path=anchor,
        active_path=anchor,
        page_size=4096 if backend == "grafx" else None,
        binding_sha256="a" * 64,
        route_sha256="b" * 64,
    )


class _Resolver:
    def __init__(
        self,
        current: CommunityGraphRouteSnapshot,
        *,
        missing: bool = False,
    ) -> None:
        self.current = current
        self.missing = missing
        self.acquire_calls = 0
        self.inspect_calls = 0
        self.revalidations: list[tuple[CommunityGraphRouteSnapshot, bool]] = []

    @staticmethod
    def _missing() -> GraphCapabilityUnavailable:
        return GraphCapabilityUnavailable(
            "missing",
            details={"reason": "binding_missing"},
        )

    def inspect_global_route(self) -> CommunityGraphRouteSnapshot:
        self.inspect_calls += 1
        if self.missing:
            raise self._missing()
        return self.current

    def acquire_global_route(self) -> CommunityGraphRouteSnapshot:
        self.acquire_calls += 1
        if self.missing:
            raise self._missing()
        return self.current

    def revalidate_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool = False,
    ) -> CommunityGraphRouteSnapshot:
        self.revalidations.append((snapshot, require_physical))
        if self.missing or self.current != snapshot:
            raise GraphCapabilityUnavailable(
                "drift",
                details={"reason": "graph_route_snapshot_mismatch"},
            )
        return self.current


class _TracingRLock:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.entries = 0
        self.exits = 0
        self.depth = 0
        self.max_depth = 0

    def __enter__(self) -> object:
        self.lock.acquire()
        self.entries += 1
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        return self

    def __exit__(self, *exc: object) -> None:
        self.exits += 1
        self.depth -= 1
        self.lock.release()


class _RuntimeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.execute_failure: BaseException | None = None

    def _call(
        self,
        name: str,
        *args: object,
        result: Any = None,
        **kwargs: object,
    ) -> Any:
        self.calls.append((name, args, kwargs))
        return result

    def state(self, *, generation: str | None = None) -> GraphRuntimeState:
        return self._call("state", generation=generation)

    def bootstrap(self):
        return self._call("bootstrap", result=object())

    def ensure_layer_schema(self) -> tuple[str, ...]:
        return self._call("ensure_layer_schema", result=("layer",))

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        self.calls.append(("execute", (statement, params), {}))
        if self.execute_failure is not None:
            raise self.execute_failure
        return GraphStatementResult.from_rows(((statement,),), columns=("statement",))

    def search_decision_digests(self, *args: object, **kwargs: object):
        return self._call("search_decision_digests", *args, result=[], **kwargs)

    def list_schema_objects(self) -> tuple[str, ...]:
        return self._call("list_schema_objects", result=("Board",))

    def __getattr__(self, name: str):
        def operation(*args: object, **kwargs: object):
            result: object = None
            if name == "upsert_decision_digest":
                result = "digest"
            elif name.startswith(("delete_", "normalize_")):
                result = 1
            return self._call(name, *args, result=result, **kwargs)

        return operation


class _SessionFactory:
    def __init__(
        self,
        provider: _RuntimeProvider,
        *,
        before_yield: Any = None,
    ) -> None:
        self.provider = provider
        self.before_yield = before_yield
        self.snapshots: list[CommunityGraphRouteSnapshot] = []
        self.entered = 0
        self.exited = 0
        self.verification_entered = 0
        self.verification_exited = 0
        self.flushes = 0
        self.closes = 0
        self.purges = 0
        self.privacy_erases = 0

    @contextmanager
    def _verification_scope(self):
        self.verification_entered += 1
        try:
            yield
        finally:
            self.verification_exited += 1

    def _flush(self) -> None:
        self.flushes += 1

    def _close(self) -> None:
        self.closes += 1

    def _purge(self, reason: str) -> GraphPurgeResult:
        self.purges += 1
        return GraphPurgeResult(
            board_id="_global",
            removed=True,
            not_found=False,
            status="purged",
            reason=reason,
        )

    def _privacy(
        self,
        board_id: str,
        reason: str,
        survivors: tuple[str, ...] | None,
    ) -> dict[str, object]:
        self.privacy_erases += 1
        return {"board_id": board_id, "reason": reason, "survivors": survivors}

    @contextmanager
    def __call__(self, snapshot: CommunityGraphRouteSnapshot):
        self.snapshots.append(snapshot)
        self.entered += 1
        if self.before_yield is not None:
            self.before_yield()
        try:
            yield CommunityGlobalDiscoveryRuntimeOperationSession(
                runtime=self.provider,  # type: ignore[arg-type]
                post_write_verification_scope_unguarded=self._verification_scope,
                flush_after_write_batch_unguarded=self._flush,
                close_unguarded=self._close,
                purge_unguarded=self._purge,
                erase_storage_for_privacy_unguarded=self._privacy,
            )
        finally:
            self.exited += 1


class _RuntimeHarness:
    def __init__(
        self,
        resolver: _Resolver,
        *,
        lock: _TracingRLock | None = None,
        ladybug_factory: _SessionFactory | None = None,
        grafx_factory: _SessionFactory | None = None,
    ) -> None:
        self.resolver = resolver
        self.lock = lock or _TracingRLock()
        self.ladybug_provider = _RuntimeProvider()
        self.grafx_provider = _RuntimeProvider()
        self.ladybug_factory = ladybug_factory or _SessionFactory(self.ladybug_provider)
        self.grafx_factory = grafx_factory or _SessionFactory(self.grafx_provider)
        self.fence_phases: list[str] = []
        self.state_calls: list[tuple[str, CommunityGraphRouteSnapshot]] = []
        self.path_calls: list[tuple[str, CommunityGraphRouteSnapshot]] = []
        self.standalone_closes: list[tuple[str, CommunityGraphRouteSnapshot]] = []

        def state(
            backend: str,
            snapshot: CommunityGraphRouteSnapshot,
            generation: str | None,
        ) -> GraphRuntimeState:
            self.state_calls.append((backend, snapshot))
            return GraphRuntimeState.from_observation(
                board_id="_global",
                storage_ref=StorageRef("global-discovery", "test"),
                state=GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
                generation=generation,
                reason_code=f"{backend}_present",
                observed_at=datetime.now(UTC),
                backend=backend,
            )

        def paths(
            backend: str,
            snapshot: CommunityGraphRouteSnapshot,
        ) -> tuple[Path, ...]:
            self.path_calls.append((backend, snapshot))
            return (snapshot.anchor_path,)

        def close(backend: str, snapshot: CommunityGraphRouteSnapshot) -> None:
            self.standalone_closes.append((backend, snapshot))

        def purge(
            snapshot: CommunityGraphRouteSnapshot,
            reason: str,
        ) -> GraphPurgeResult:
            return GraphPurgeResult(
                board_id="_global",
                removed=True,
                not_found=False,
                status="purged",
                reason=reason,
                backend=snapshot.backend,
            )

        def privacy(
            snapshot: CommunityGraphRouteSnapshot,
            board_id: str,
            reason: str,
            survivors: tuple[str, ...] | None,
        ) -> dict[str, object]:
            return {
                "backend": snapshot.backend,
                "board_id": board_id,
                "reason": reason,
                "survivors": survivors,
            }

        self.runtime = CommunityRoutedGlobalDiscoveryRuntime(
            resolver,  # type: ignore[arg-type]
            global_lock=self.lock,
            revalidate_write_fence=self.fence_phases.append,
            statement_is_write=lambda statement: statement.startswith("CREATE"),
            ladybug_session_factory=self.ladybug_factory,
            grafx_session_factory=self.grafx_factory,
            ladybug_state=lambda snapshot, generation: state(
                "ladybug", snapshot, generation
            ),
            grafx_state=lambda snapshot, generation: state(
                "grafx", snapshot, generation
            ),
            ladybug_materialization_paths=lambda snapshot: paths("ladybug", snapshot),
            grafx_materialization_paths=lambda snapshot: paths("grafx", snapshot),
            ladybug_close_unguarded=lambda snapshot: close("ladybug", snapshot),
            grafx_close_unguarded=lambda snapshot: close("grafx", snapshot),
            ladybug_purge_unguarded=purge,
            grafx_purge_unguarded=purge,
            ladybug_privacy_erase_unguarded=privacy,
            grafx_privacy_erase_unguarded=privacy,
        )


def test_state_is_inspect_only_non_opening_and_missing_is_unavailable(
    tmp_path: Path,
) -> None:
    resolver = _Resolver(_snapshot(tmp_path, backend="grafx"))
    harness = _RuntimeHarness(resolver)

    observed = harness.runtime.state(generation="health-7")

    assert (
        observed.normalized_state
        is GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )
    assert observed.generation == "health-7"
    assert harness.state_calls == [("grafx", resolver.current)]
    assert harness.grafx_factory.entered == 0
    assert harness.ladybug_factory.entered == 0
    assert resolver.acquire_calls == 0

    resolver.missing = True
    missing = harness.runtime.state(generation="health-8")
    assert missing.normalized_state is GraphRuntimeObservationState.PROVIDER_UNAVAILABLE
    assert missing.reason_code == "graph_route_binding_missing"
    assert harness.grafx_factory.entered == 0


def test_runtime_pins_one_selected_route_and_never_falls_back(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, backend="ladybug")
    resolver = _Resolver(snapshot)
    harness = _RuntimeHarness(resolver)

    result = harness.runtime.execute("MATCH (n) RETURN n")

    assert result.rows == (("MATCH (n) RETURN n",),)
    assert harness.ladybug_factory.snapshots == [snapshot]
    assert harness.grafx_factory.entered == 0
    assert resolver.acquire_calls == 1
    assert resolver.revalidations[-1] == (snapshot, True)


def test_runtime_refuses_route_drift_before_physical_dispatch(tmp_path: Path) -> None:
    initial = _snapshot(tmp_path, backend="grafx")
    resolver = _Resolver(initial)
    provider = _RuntimeProvider()
    drifted = replace(
        initial,
        active_path=initial.anchor_path.parent / "generations" / "g2" / "graph",
        route_sha256="c" * 64,
        active_generation="g2",
    )
    factory = _SessionFactory(
        provider,
        before_yield=lambda: setattr(resolver, "current", drifted),
    )
    harness = _RuntimeHarness(resolver, grafx_factory=factory)

    with pytest.raises(GraphCapabilityUnavailable) as refused:
        harness.runtime.list_schema_objects()

    assert refused.value.details["reason"] == "graph_route_snapshot_mismatch"
    assert provider.calls == []
    assert factory.exited == 1


def test_reads_skip_writer_revalidation_but_search_uses_writer_lane(
    tmp_path: Path,
) -> None:
    resolver = _Resolver(_snapshot(tmp_path))
    harness = _RuntimeHarness(resolver)

    harness.runtime.list_schema_objects()
    harness.runtime.execute("MATCH (n) RETURN n")
    assert harness.fence_phases == []

    harness.runtime.execute("CREATE (n:Board)")
    harness.runtime.search_decision_digests(
        [0.0],
        board_ids=("board-a",),
        graph_layer="project",
        top_k=1,
        min_similarity=0.0,
    )

    assert harness.fence_phases == [
        "global_statement_write",
        "global_digest_search",
    ]
    assert harness.lock.entries == harness.lock.exits
    assert harness.lock.depth == 0


def test_verification_scope_reuses_snapshot_session_and_cleans_cancellation(
    tmp_path: Path,
) -> None:
    resolver = _Resolver(_snapshot(tmp_path, backend="grafx"))
    harness = _RuntimeHarness(resolver)

    with (
        pytest.raises(asyncio.CancelledError),
        harness.runtime.post_write_verification_scope(),
    ):
        harness.runtime.list_schema_objects()
        harness.runtime.flush_after_write_batch()
        harness.runtime.close()
        raise asyncio.CancelledError

    assert resolver.acquire_calls == 1
    assert harness.grafx_factory.entered == 1
    assert harness.grafx_factory.exited == 1
    assert harness.grafx_factory.verification_entered == 1
    assert harness.grafx_factory.verification_exited == 1
    assert harness.grafx_factory.flushes == 1
    assert harness.grafx_factory.closes == 1
    assert harness.lock.depth == 0

    # No thread-local/context state leaked from cancellation: a new operation
    # obtains a new physical session normally.
    harness.runtime.list_schema_objects()
    assert harness.grafx_factory.entered == 2
    assert harness.grafx_factory.exited == 2


def test_materialization_observation_paths_follow_the_bound_backend(
    tmp_path: Path,
) -> None:
    resolver = _Resolver(_snapshot(tmp_path, backend="grafx"))
    harness = _RuntimeHarness(resolver)

    assert harness.runtime.materialization_observation_paths() == (
        resolver.current.anchor_path,
    )
    assert harness.path_calls == [("grafx", resolver.current)]
    assert harness.grafx_factory.entered == 0


class _RecoveryProvider:
    def __init__(self) -> None:
        self.before_second_fence: Any = None
        self.calls: list[str] = []
        self.extension_kwargs: list[tuple[str, dict[str, object]]] = []
        self.failure: BaseException | None = None
        self.retention_result = _RetentionResult()
        self.terminal_result: GlobalDiscoveryCutoverResult | None = None
        self.predecessor_result: GlobalDiscoveryCutoverResult | None = (
            GlobalDiscoveryCutoverResult(
                outcome="completed",
                candidate_sha256="c" * 64,
                quarantine_ref=None,
                schema_object_count=3,
            )
        )

    def inspect_live_artifact(self) -> GlobalDiscoveryArtifactSnapshot:
        self.calls.append("inspect")
        return GlobalDiscoveryArtifactSnapshot(
            exists=True,
            artifact_count=1,
            total_bytes=7,
            sha256="d" * 64,
        )

    def current_snapshot_fingerprint(self) -> str:
        self.calls.append("fingerprint")
        return "e" * 64

    def _operation(
        self,
        name: str,
        *,
        fence_check: Any,
        result: Any,
        **kwargs: object,
    ) -> Any:
        self.calls.append(name)
        self.extension_kwargs.append((name, kwargs))
        fence_check()
        if self.before_second_fence is not None:
            self.before_second_fence()
        fence_check()
        if self.failure is not None:
            raise self.failure
        return result

    def _recover(self, *, fence_check: Any, **kwargs: object):
        return self._operation(
            "recover",
            fence_check=fence_check,
            result=GlobalDiscoveryCutoverResult(
                outcome="completed",
                candidate_sha256="f" * 64,
                quarantine_ref=None,
                schema_object_count=3,
            ),
            **kwargs,
        )

    def reconcile_attempt_artifacts(self, *, fence_check: Any, **kwargs: object):
        return self._operation(
            "reconcile_attempt_artifacts",
            fence_check=fence_check,
            result=self.retention_result,
            **kwargs,
        )

    def reconcile_attempt_terminal_truth(
        self,
        *,
        fence_check: Any,
        **kwargs: object,
    ):
        return self._operation(
            "reconcile_attempt_terminal_truth",
            fence_check=fence_check,
            result=self.terminal_result,
            **kwargs,
        )

    def reconcile_predecessor_and_complete(
        self,
        *,
        fence_check: Any,
        **kwargs: object,
    ):
        return self._operation(
            "reconcile_predecessor_and_complete",
            fence_check=fence_check,
            result=self.predecessor_result,
            **kwargs,
        )

    rebuild_candidate_and_cutover = _recover
    recover_and_cutover = _recover


class _RetentionResult:
    quarantined_ids: tuple[str, ...] = ()
    retained_ids: tuple[str, ...] = ("retained",)
    deleted_ids: tuple[str, ...] = ()


def _recovery(
    resolver: _Resolver,
    provider: _RecoveryProvider,
    *,
    lock: _TracingRLock | None = None,
    validator: Any = None,
) -> CommunityRoutedGlobalDiscoveryRecovery:
    def wrong_backend(_snapshot: CommunityGraphRouteSnapshot):
        raise AssertionError("fallback provider called")

    factory = lambda _snapshot: provider
    return CommunityRoutedGlobalDiscoveryRecovery(
        resolver,  # type: ignore[arg-type]
        global_lock=lock or _TracingRLock(),
        ladybug_factory=(
            factory if resolver.current.backend == "ladybug" else wrong_backend
        ),
        grafx_factory=(
            factory if resolver.current.backend == "grafx" else wrong_backend
        ),
        validate_authenticated_transition=validator or (lambda **_kwargs: False),
    )


def test_recovery_accepts_only_authenticated_same_attempt_transition(
    tmp_path: Path,
) -> None:
    initial = _snapshot(tmp_path, backend="grafx")
    transitioned = replace(
        initial,
        active_path=initial.anchor_path.parent / "generations" / "attempt-2" / "graph",
        active_generation="attempt-2",
        active_manifest_sha256="1" * 64,
        route_sha256="2" * 64,
    )
    resolver = _Resolver(initial)
    provider = _RecoveryProvider()
    provider.before_second_fence = lambda: setattr(resolver, "current", transitioned)
    validation_calls: list[dict[str, object]] = []

    def validate(**kwargs: object) -> bool:
        validation_calls.append(kwargs)
        return True

    recovery = _recovery(resolver, provider, validator=validate)
    writer_checks = 0

    def fence() -> None:
        nonlocal writer_checks
        writer_checks += 1

    result = recovery.recover_and_cutover(
        run_id="run-a",
        epoch=2,
        attempt_id="attempt-a",
        expected_live_sha256="3" * 64,
        boards=(),
        fence_check=fence,
    )

    assert result.outcome == "completed"
    assert writer_checks == 3
    assert len(validation_calls) == 1
    assert validation_calls[0]["initial"] == initial
    assert validation_calls[0]["previous"] == initial
    assert validation_calls[0]["observed"] == transitioned
    assert validation_calls[0]["run_id"] == "run-a"
    assert validation_calls[0]["epoch"] == 2
    assert validation_calls[0]["attempt_id"] == "attempt-a"


def test_recovery_rejects_unauthenticated_transition_and_lost_writer(
    tmp_path: Path,
) -> None:
    initial = _snapshot(tmp_path)
    transitioned = replace(
        initial,
        active_path=initial.anchor_path.parent / "generations" / "foreign" / "graph",
        route_sha256="4" * 64,
        active_generation="foreign",
    )
    resolver = _Resolver(initial)
    provider = _RecoveryProvider()
    provider.before_second_fence = lambda: setattr(resolver, "current", transitioned)
    recovery = _recovery(resolver, provider, validator=lambda **_kwargs: False)

    with pytest.raises(GraphCapabilityUnavailable) as refused:
        recovery.rebuild_candidate_and_cutover(
            run_id="run-a",
            epoch=1,
            attempt_id="attempt-a",
            expected_live_sha256="5" * 64,
            boards=(),
            fence_check=lambda: None,
        )
    assert refused.value.details["reason"] == (
        "recovery_route_transition_unauthenticated"
    )

    resolver.current = initial
    provider.calls.clear()

    def lost() -> None:
        raise RuntimeError("writer lost")

    with pytest.raises(RuntimeError, match="writer lost"):
        recovery.recover_and_cutover(
            run_id="run-a",
            epoch=1,
            attempt_id="attempt-a",
            expected_live_sha256="5" * 64,
            boards=(),
            fence_check=lost,
        )
    assert provider.calls == []


def test_recovery_refuses_backend_or_binding_change_without_validator(
    tmp_path: Path,
) -> None:
    initial = _snapshot(tmp_path)
    changed = replace(
        initial,
        binding_sha256="6" * 64,
        route_sha256="7" * 64,
    )
    resolver = _Resolver(initial)
    provider = _RecoveryProvider()
    provider.before_second_fence = lambda: setattr(resolver, "current", changed)
    validator_calls = 0

    def validator(**_kwargs: object) -> bool:
        nonlocal validator_calls
        validator_calls += 1
        return True

    recovery = _recovery(resolver, provider, validator=validator)
    with pytest.raises(GraphCapabilityUnavailable) as refused:
        recovery.recover_and_cutover(
            run_id="run-a",
            epoch=1,
            attempt_id="attempt-a",
            expected_live_sha256="8" * 64,
            boards=(),
            fence_check=lambda: None,
        )
    assert refused.value.details["reason"] == "recovery_binding_changed"
    assert validator_calls == 0


def test_runtime_and_recovery_share_the_injected_lock_and_match_core_ports(
    tmp_path: Path,
) -> None:
    resolver = _Resolver(_snapshot(tmp_path))
    shared_lock = _TracingRLock()
    harness = _RuntimeHarness(resolver, lock=shared_lock)
    provider = _RecoveryProvider()
    recovery = _recovery(resolver, provider, lock=shared_lock)

    assert isinstance(harness.runtime, GlobalDiscoveryRuntime)
    assert isinstance(recovery, GlobalDiscoveryRecovery)
    harness.runtime.list_schema_objects()
    recovery.inspect_live_artifact()

    assert shared_lock.entries == 2
    assert shared_lock.exits == 2
    assert shared_lock.depth == 0


@pytest.mark.parametrize("backend", ["ladybug", "grafx"])
def test_recovery_worker_extensions_route_one_selected_leaf_with_exact_arguments(
    tmp_path: Path,
    backend: str,
) -> None:
    initial = _snapshot(tmp_path, backend=backend)
    resolver = _Resolver(initial)
    provider = _RecoveryProvider()
    selected: list[tuple[str, CommunityGraphRouteSnapshot]] = []
    wrong_backend_calls = 0

    def selected_factory(snapshot: CommunityGraphRouteSnapshot) -> _RecoveryProvider:
        selected.append((backend, snapshot))
        return provider

    def wrong_factory(_snapshot: CommunityGraphRouteSnapshot) -> _RecoveryProvider:
        nonlocal wrong_backend_calls
        wrong_backend_calls += 1
        raise AssertionError("fallback provider called")

    recovery = CommunityRoutedGlobalDiscoveryRecovery(
        resolver,  # type: ignore[arg-type]
        global_lock=_TracingRLock(),
        ladybug_factory=selected_factory if backend == "ladybug" else wrong_factory,
        grafx_factory=selected_factory if backend == "grafx" else wrong_factory,
        validate_authenticated_transition=lambda **_kwargs: False,
    )
    checks = 0

    def fence() -> None:
        nonlocal checks
        checks += 1

    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    retention = recovery.reconcile_attempt_artifacts(
        run_id="run-a",
        known_attempt_ids=("run-a/attempt-1",),
        now=now,
        fence_check=fence,
    )
    terminal = recovery.reconcile_attempt_terminal_truth(
        run_id="run-a",
        epoch=2,
        attempt_id="run-a/attempt-2",
        expected_live_sha256="1" * 64,
        boards=(),
        fence_check=fence,
    )
    predecessor = recovery.reconcile_predecessor_and_complete(
        run_id="run-a",
        epoch=2,
        attempt_id="run-a/attempt-2",
        ancestry=((1, "run-a/attempt-1"),),
        expected_live_sha256="1" * 64,
        boards=(),
        fence_check=fence,
    )

    assert retention is provider.retention_result
    assert terminal is None
    assert predecessor is provider.predecessor_result
    assert selected == [(backend, initial)] * 3
    assert wrong_backend_calls == 0
    assert checks == 9
    assert provider.calls == [
        "reconcile_attempt_artifacts",
        "reconcile_attempt_terminal_truth",
        "reconcile_predecessor_and_complete",
    ]
    assert provider.extension_kwargs[0][1] == {
        "run_id": "run-a",
        "known_attempt_ids": ("run-a/attempt-1",),
        "now": now,
    }
    assert provider.extension_kwargs[1][1] == {
        "run_id": "run-a",
        "epoch": 2,
        "attempt_id": "run-a/attempt-2",
        "expected_live_sha256": "1" * 64,
        "boards": (),
    }
    assert provider.extension_kwargs[2][1] == {
        **provider.extension_kwargs[1][1],
        "ancestry": ((1, "run-a/attempt-1"),),
    }


def test_recovery_worker_extension_requires_exact_route_and_core_fence(
    tmp_path: Path,
) -> None:
    initial = _snapshot(tmp_path, backend="grafx")
    transitioned = replace(
        initial,
        active_path=initial.anchor_path.parent / "generations" / "foreign" / "graph",
        active_generation="foreign",
        active_manifest_sha256="2" * 64,
        route_sha256="3" * 64,
    )
    resolver = _Resolver(initial)
    provider = _RecoveryProvider()
    provider.before_second_fence = lambda: setattr(resolver, "current", transitioned)
    validator_calls = 0

    def validator(**_kwargs: object) -> bool:
        nonlocal validator_calls
        validator_calls += 1
        return True

    recovery = _recovery(resolver, provider, validator=validator)
    with pytest.raises(GraphCapabilityUnavailable) as changed:
        recovery.reconcile_attempt_terminal_truth(
            run_id="run-a",
            epoch=1,
            attempt_id="run-a/attempt-1",
            expected_live_sha256="4" * 64,
            boards=(),
            fence_check=lambda: None,
        )
    assert changed.value.details["reason"] == ("recovery_route_transition_not_allowed")
    assert validator_calls == 0

    resolver.current = initial
    provider.before_second_fence = None
    provider.calls.clear()

    def lost() -> None:
        raise RuntimeError("writer lost before leaf")

    with pytest.raises(RuntimeError, match="writer lost before leaf"):
        recovery.reconcile_attempt_artifacts(
            run_id="run-a",
            known_attempt_ids=("run-a/attempt-1",),
            now=datetime.now(UTC),
            fence_check=lost,
        )
    assert provider.calls == []


def test_recovery_worker_extensions_translate_every_grafx_fence_envelope(
    tmp_path: Path,
) -> None:
    resolver = _Resolver(_snapshot(tmp_path, backend="grafx"))
    provider = _RecoveryProvider()
    original = RuntimeError("lost exact Grafx fence")
    provider.failure = CommunityGrafxGlobalDiscoveryFenceError(original)
    recovery = _recovery(resolver, provider)

    calls = (
        lambda: recovery.reconcile_attempt_artifacts(
            run_id="run-a",
            known_attempt_ids=("run-a/attempt-1",),
            now=datetime.now(UTC),
            fence_check=lambda: None,
        ),
        lambda: recovery.reconcile_attempt_terminal_truth(
            run_id="run-a",
            epoch=2,
            attempt_id="run-a/attempt-2",
            expected_live_sha256="5" * 64,
            boards=(),
            fence_check=lambda: None,
        ),
        lambda: recovery.reconcile_predecessor_and_complete(
            run_id="run-a",
            epoch=2,
            attempt_id="run-a/attempt-2",
            ancestry=((1, "run-a/attempt-1"),),
            expected_live_sha256="5" * 64,
            boards=(),
            fence_check=lambda: None,
        ),
    )
    for call in calls:
        with pytest.raises(CommunityGlobalDiscoveryRecoveryFenceError) as translated:
            call()
        assert translated.value.original is original

    assert provider.calls == [
        "reconcile_attempt_artifacts",
        "reconcile_attempt_terminal_truth",
        "reconcile_predecessor_and_complete",
    ]


def test_routed_global_module_has_no_settings_fallback_or_writer_acquisition() -> None:
    import okto_pulse.community.adapters.routed_global_discovery as routed

    source = inspect.getsource(routed)
    assert "get_settings" not in source
    assert "get_current_provider_registry" not in source
    assert "ContextVar" not in source.replace("No new ``ContextVar``", "")
    assert "global_discovery_writer_scope" not in source
    assert "community_global_discovery_writer_fence" not in source
    assert "ladybug_writer_scope" not in source
