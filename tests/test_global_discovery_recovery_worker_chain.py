"""R6: exercise the PRODUCTION worker/dispatch-claim/deadline chain end to end,
using REAL production components — no replaced acquisition, handoff, or reconciler.

* The writer lease is the untouched production ``GlobalDiscoveryWriterLease`` — its
  classmethod is NOT monkeypatched.  Observation is done by registering a REAL
  ``CommunityLocalWriteLockPort`` wrapped in a delegating recorder at the
  coordination-provider boundary (``register_coordination_providers``); the
  production ``KGSingleWriterLock`` constructs/uses it.
* The native inputs come from the REAL ``CommunityDurableRecoveryInputProvider``
  backed by a REAL ``CommunityFileSystemRebuildAuditArtifactStore``; epoch inputs
  are seeded through ``GlobalDiscoveryRecoveryWorkerInputStore.put`` and loaded
  create-only by the provider.
* The resume handoff is the REAL ``input_provider.handoff_resume_inputs`` wired
  into the store at construction (as ``build_community_recovery_runtime`` does) —
  ``store._resume_input_handoff`` is never written by the test.
* The recorder DELEGATES ``reconcile_attempt_artifacts`` to the real adapter (a
  no-op production reconciler would be visible), and records the exact forwarded
  fence OBJECT identity + full ``RecoveryNativeInputs`` values.
* Epoch-2 resume closes the epoch-1 runtime and opens a FRESH runtime over the
  same SQL database + artifact store — proving restart-safe durable handoff and
  avoiding the epoch-1 lease-release race (no wall-clock sleeps).
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ladybug  # noqa: F401
import pytest
from sqlalchemy import create_engine, select, update

from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    bootstrap_marker_present,
    write_bootstrap_marker,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityDurableRecoveryInputProvider,
    CommunityGlobalDiscoveryRecoveryNativeOperation,
    CommunityRecoveryWorker,
    RecoveryDispatchStage,
    RecoveryDispatchState,
    RecoveryNativeInputs,
    RecoveryPendingAncestryError,
    RecoveryPredecessorReconcilePlan,
    RecoveryWorkerFenceError,
    SQLAlchemyRecoveryRunStore,
    _RECOVERY_WRITER_LEASE_SECONDS,
)
from okto_pulse.community.adapters.rebuild_audit_storage import (
    CommunityFileSystemRebuildAuditArtifactStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    GlobalDiscoveryRecoveryAttempt,
    GlobalDiscoveryRecoveryDispatch,
    GlobalDiscoveryRecoverySlot,
    GlobalDiscoveryRecoveryTransition,
)
from okto_pulse.core.kg.global_discovery_recovery import (
    GlobalDiscoveryRecoveryWorkerInputStore,
    GlobalDiscoveryRecoveryWorkerInputs,
)
from okto_pulse.core.kg.global_discovery_recovery_control import (
    RecoveryControlPlane,
    RecoveryPreparationCommand,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    RecoveryRunBinding,
    RecoveryRunPhase,
    RecoveryRunState,
    RecoveryStartCommand,
    RecoveryTerminalOutcome,
    RecoveryWorkerResult,
)
from okto_pulse.core.ports.coordination import (
    CoordinationProviderMissing,
    get_write_lock_port,
    register_coordination_providers,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    recovery_attempt_id,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    read_active_generation,
)
from okto_pulse.core.kg.global_discovery_writer import (
    GLOBAL_DISCOVERY_WRITER_SCOPE,
    global_discovery_writer_scope,
)
from okto_pulse.core.kg.single_writer_lock import (
    GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
)

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecoveryError,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult

from test_global_discovery_recovery_adapter import (  # noqa: E402
    _SCHEMA,
    _CandidateRuntime,
    _UnreadableLiveRuntime,
    _coherent_adopt_state,
)
from test_global_discovery_recovery_worker_adoption import _seed  # noqa: E402
from repo_layout import resolve_core_repo


def _two_seeds():
    """Two NON-identical board seeds so a board-ID-only rebuild mutant (that
    materializes ids but drops digests/links) dies on exact counts equality."""

    from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
        GlobalDiscoveryBoardSeed,
        GlobalDiscoveryDigestSeed,
    )

    second = GlobalDiscoveryBoardSeed(
        board_id="board-second-seed",
        board_name="Second seed board",
        summary="second summary",
        summary_embedding=(0.5, 0.6),
        digests=(
            GlobalDiscoveryDigestSeed(
                original_node_id="decision-two",
                title="Decision Two",
                summary="digest two",
                node_type="Decision",
                graph_layer="canonical",
                source_artifact_ref="artifact-two",
                embedding=(0.7, 0.8),
            ),
        ),
        source_inventory_hash="sha256:inventory-two",
    )
    return (_seed(), second)


def _two_real_seeds():
    """R8-B7.1: two NON-identical board seeds whose embeddings have the REAL
    Ladybug schema width (384-dim DOUBLE array), so the PRODUCTION runtime can
    materialize them into actual graph/WAL bytes."""

    from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
        GlobalDiscoveryBoardSeed,
        GlobalDiscoveryDigestSeed,
    )

    def _emb(block: int) -> tuple[float, ...]:
        # Exactly-representable doubles (n/256) — bit-stable through persistence.
        return tuple((block * 384 + i) / 256.0 for i in range(384))

    first = GlobalDiscoveryBoardSeed(
        board_id="board-real-one",
        board_name="Real seed board one",
        summary="first real summary",
        summary_embedding=_emb(0),
        digests=(
            GlobalDiscoveryDigestSeed(
                original_node_id="decision-real-one",
                title="Real Decision One",
                summary="real digest one",
                node_type="Decision",
                graph_layer="canonical",
                source_artifact_ref="artifact-real-one",
                embedding=_emb(1),
            ),
        ),
        source_inventory_hash="sha256:inventory-real-one",
    )
    second = GlobalDiscoveryBoardSeed(
        board_id="board-real-two",
        board_name="Real seed board two",
        summary="second real summary",
        summary_embedding=_emb(2),
        digests=(
            GlobalDiscoveryDigestSeed(
                original_node_id="decision-real-two",
                title="Real Decision Two",
                summary="real digest two",
                node_type="Decision",
                graph_layer="canonical",
                source_artifact_ref="artifact-real-two",
                embedding=_emb(3),
            ),
        ),
        source_inventory_hash="sha256:inventory-real-two",
    )
    return (first, second)


class _PersistentGlobalDiscoveryRuntime:
    """B7.1: a genuinely file-backed Global Discovery runtime.  Its materialized
    semantics (boards/digests/links) are serialized INTO the graph bytes at
    ``path`` on every write and RELOADED from those EXACT bytes on reopen.  There
    is no detached in-memory truth: a fresh runtime derives its projection solely
    from the bytes being hashed, so arbitrary/constant active bytes cannot pass a
    reopened validation."""

    _MARKER = "__okto_gd_persistent_runtime__"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state = self._load()

    def _wal_path(self) -> Path:
        return self.path.with_name(self.path.name + ".wal")

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get(self._MARKER) is not True:
            return None
        return {
            "boards": {k: dict(v) for k, v in raw.get("boards", {}).items()},
            "digests": {k: dict(v) for k, v in raw.get("digests", {}).items()},
            "links": {tuple(pair) for pair in raw.get("links", [])},
        }

    def _persist(self) -> None:
        payload = {
            self._MARKER: True,
            "boards": self.state["boards"],
            "digests": self.state["digests"],
            "links": sorted([list(pair) for pair in self.state["links"]]),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        self._wal_path().write_bytes(b"")

    def bootstrap(self) -> None:
        self.state = {"boards": {}, "digests": {}, "links": set()}
        self._persist()

    def list_schema_objects(self):
        # A graph whose bytes are not our persisted format has NO schema, so a
        # reopened validation fails closed (constant/arbitrary bytes cannot pass).
        return _SCHEMA if self.state is not None else ()

    def close(self) -> None:
        return None

    def flush_after_write_batch(self) -> None:
        if self.state is not None:
            self._persist()

    def upsert_board_summary(self, **values) -> None:
        self.state["boards"][values["board_id"]] = dict(values)
        self._persist()

    def upsert_decision_digest(self, **values) -> str:
        self.state["digests"][values["digest_id"]] = dict(values)
        self._persist()
        return "inserted"

    def link_board_digest(self, *, board_id: str, digest_id: str) -> None:
        self.state["links"].add((board_id, digest_id))
        self._persist()

    def execute(self, statement: str, params=None) -> GraphStatementResult:
        if self.state is None:
            raise CommunityGlobalDiscoveryRecoveryError(
                "global_discovery_persistent_runtime_unopened"
            )
        params = params or {}
        board_id = params.get("board_id")
        if statement.startswith("MATCH (b:Board) RETURN b.board_id, b.name, b.summary"):
            return GraphStatementResult.from_rows(
                tuple(
                    (
                        row["board_id"],
                        row["name"],
                        row["summary"],
                        row["decision_count"],
                        row["summary_embedding"],
                    )
                    for row in self.state["boards"].values()
                )
            )
        if statement.startswith("MATCH (d:DecisionDigest) RETURN d.id, d.board_id"):
            return GraphStatementResult.from_rows(
                tuple(
                    (
                        row["digest_id"],
                        row["board_id"],
                        row["original_node_id"],
                        row["title"],
                        row["summary"],
                        row["node_type"],
                        row.get("graph_layer") or "legacy_unknown",
                        row["embedding"],
                    )
                    for row in self.state["digests"].values()
                )
            )
        if statement.startswith(
            "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) RETURN"
        ):
            return GraphStatementResult.from_rows(
                tuple(
                    (
                        linked_board_id,
                        digest_id,
                        self.state["digests"][digest_id]["board_id"],
                        self.state["digests"][digest_id]["original_node_id"],
                    )
                    for linked_board_id, digest_id in self.state["links"]
                )
            )
        if "MATCH (b:Board) WHERE" in statement:
            count = int(board_id in self.state["boards"])
        elif "MATCH (d:DecisionDigest) WHERE" in statement:
            count = sum(
                row["board_id"] == board_id for row in self.state["digests"].values()
            )
        elif "CONTAINS_DECISION" in statement:
            count = sum(link[0] == board_id for link in self.state["links"])
        elif "MATCH (b:Board) RETURN" in statement:
            count = len(self.state["boards"])
        elif "MATCH (d:DecisionDigest) RETURN" in statement:
            count = len(self.state["digests"])
        else:
            raise AssertionError(statement)
        return GraphStatementResult.from_rows(((count,),))


def _sorted_boards(boards):
    return sorted(boards, key=lambda seed: seed.board_id)


def _wait_until(control, *, run_id, predicate, timeout_seconds=5.0):
    import time

    deadline = time.monotonic() + timeout_seconds
    last = control.status(run_id)
    while not predicate(last):
        if time.monotonic() >= deadline:
            raise AssertionError(f"status predicate timed out; last={last!r}")
        time.sleep(0.01)
        last = control.status(run_id)
    return last


def _wait_for_release(recording_port, owner_token, *, timeout_seconds=6.0):
    """Wait until the production writer lease was RELEASED for ``owner_token`` —
    the observable signal that a stale/aborted native op has drained and stepped
    aside (there is no terminal state for a stale-claim step-aside)."""

    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        if any(
            op == "release" and kwargs.get("owner_token") == owner_token
            for op, kwargs, _result in recording_port.events
        ):
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"writer lease was never released for {owner_token!r}: "
                f"{recording_port.events!r}"
            )
        time.sleep(0.02)


class _RecordingWriteLockPort:
    """Delegates to a REAL ``CommunityLocalWriteLockPort`` and records every
    single-writer fence op with its COMPLETE kwargs + result — proving the
    genuine production lock lifecycle/contract without replacing the lease
    acquisition method."""

    def __init__(self, real: CommunityLocalWriteLockPort) -> None:
        self._real = real
        # Each event: (op, kwargs_dict, result)
        self.events: list[tuple] = []

    def acquire_single_writer_sync(self, *args, **kwargs):
        result = self._real.acquire_single_writer_sync(*args, **kwargs)
        self.events.append(("acquire", dict(kwargs), result))
        return result

    def release_single_writer_sync(self, *args, **kwargs):
        result = self._real.release_single_writer_sync(*args, **kwargs)
        self.events.append(("release", dict(kwargs), result))
        return result

    def renew_single_writer_sync(self, *args, **kwargs):
        result = self._real.renew_single_writer_sync(*args, **kwargs)
        self.events.append(("renew", dict(kwargs), result))
        return result

    def inspect_single_writer_sync(self, *args, **kwargs):
        result = self._real.inspect_single_writer_sync(*args, **kwargs)
        self.events.append(("inspect", dict(kwargs), result))
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


def _lease_from_fence(physical_fence_check):
    """Recover the exact ``GlobalDiscoveryWriterLease`` the physical fence closure
    guards, WITHOUT monkeypatching acquisition — by inspecting the real closure."""

    from okto_pulse.core.kg.global_discovery_writer import GlobalDiscoveryWriterLease

    seen: list[object] = []
    stack = [physical_fence_check]
    while stack:
        fn = stack.pop()
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, GlobalDiscoveryWriterLease):
                return val
            if callable(val) and getattr(val, "__closure__", None) and val not in seen:
                seen.append(val)
                stack.append(val)
    return None


def _deadline_from_fence(physical_fence_check):
    """Recover the exact monotonic deadline the fence enforces (the larger of the
    two floats closed over: started_monotonic < deadline_at_monotonic)."""

    floats: list[float] = []
    seen: list[object] = []
    stack = [physical_fence_check]
    while stack:
        fn = stack.pop()
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, float):
                floats.append(val)
            if callable(val) and getattr(val, "__closure__", None) and val not in seen:
                seen.append(val)
                stack.append(val)
    return max(floats) if floats else None


def _active_guard_lease():
    from okto_pulse.core.kg.global_discovery_writer import _active_lease

    return _active_lease.get()


def _acquisition_for_owner(recording_port, owner_id):
    matches = [
        (kwargs, result)
        for op, kwargs, result in recording_port.events
        if op == "acquire" and kwargs.get("owner_id") == owner_id
    ]
    assert len(matches) == 1, recording_port.events
    return matches[0]


def _assert_exact_acquire_contract(recording_port, *, owner_id):
    """R6.1: the production lock was acquired EXACTLY once for this owner with the
    complete exact kwargs map."""

    from okto_pulse.core.kg.global_discovery_writer import (
        GLOBAL_DISCOVERY_WRITER_SCOPE,
    )
    from okto_pulse.core.kg.single_writer_lock import (
        GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
    )

    kwargs, result = _acquisition_for_owner(recording_port, owner_id)
    assert kwargs["board_id"] == GLOBAL_DISCOVERY_WRITER_SCOPE
    assert kwargs["artifact_id"] == GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID
    assert kwargs["operation"] == "global_discovery_recovery"
    assert isinstance(kwargs["operation"], str)
    assert kwargs["owner_id"] == owner_id
    assert kwargs["ttl_seconds"] == _RECOVERY_WRITER_LEASE_SECONDS
    assert isinstance(kwargs["ttl_seconds"], int)
    assert not isinstance(kwargs["ttl_seconds"], bool)
    assert kwargs["admin_lane"] is True
    assert result.acquired is True
    owner_token = result.owner_token
    assert isinstance(owner_token, str) and owner_token
    return owner_token


def _assert_exactly_one_release(recording_port, *, owner_token, lease=None):
    """R6.2: exactly ONE release for this exact token; the token is no longer
    owned; the captured lease is marked released."""

    releases = [
        kwargs
        for op, kwargs, _result in recording_port.events
        if op == "release" and kwargs.get("owner_token") == owner_token
    ]
    assert len(releases) == 1, recording_port.events
    # No release for any OTHER token, and no second release of this token.
    all_releases = [k for op, k, _ in recording_port.events if op == "release"]
    assert len(all_releases) == 1, recording_port.events
    # Every renew that occurred used this exact token (owner continuity).
    for op, kwargs, _result in recording_port.events:
        if op == "renew":
            assert kwargs.get("owner_token") == owner_token, (op, kwargs)
    if lease is not None:
        assert lease.released is True


@contextmanager
def _registered_recording_port(recording_port):
    try:
        previous = get_write_lock_port()
    except CoordinationProviderMissing:
        previous = None
    register_coordination_providers(write_lock_port=recording_port)
    try:
        yield
    finally:
        register_coordination_providers(
            write_lock_port=previous or CommunityLocalWriteLockPort()
        )


@contextmanager
def _kg_base_dir_configured(base_dir: Path):
    """Configure the KG registry with a real ``kg_base_dir`` so the untouched
    production ``CommunityLocalWriteLockPort`` resolves its local lock directory
    exactly as it does in production (``default_community_rebuild_base_dir``)."""

    from types import SimpleNamespace

    from okto_pulse.core.kg.interfaces.registry import (
        KGProviderRegistry,
        capture_registry_state_for_tests,
        configure_kg_registry,
        reset_registry_for_tests,
    )

    required = (
        "event_bus",
        "graph_store",
        "cypher_executor",
        "graph_transaction",
        "graph_schema_manager",
        "graph_lifecycle",
        "graph_runtime_store",
        "global_discovery_runtime",
        "board_source_reader",
    )

    class _AuditRepo:
        async def stage_consolidation_records(
            self,
            transaction_context,
            audit,
            node_refs,
            outbox_event,
        ) -> None:
            del transaction_context, audit, node_refs, outbox_event

    base = KGProviderRegistry(
        config=SimpleNamespace(kg_base_dir=str(base_dir)),
        audit_repo=_AuditRepo(),
        **{slot: object() for slot in required},
    )
    previous = capture_registry_state_for_tests()
    configure_kg_registry(base_registry=base)
    try:
        yield
    finally:
        # R6.2: honest teardown.  ``bundle.close()`` returns only after every
        # native/renew/executor future is done, so no lock op can run after this
        # point; restore the EXACT pre-test registry (or reset when absent).
        if previous is not None:
            configure_kg_registry(base_registry=previous)
        else:
            reset_registry_for_tests()


@contextmanager
def _r6_env(recording_port, kg_base_dir: Path):
    with _kg_base_dir_configured(kg_base_dir):
        with _registered_recording_port(recording_port):
            yield


class _SpyRecovery:
    """Implements the GlobalDiscoveryRecovery Protocol; DELEGATES the real
    reconciliation + physical recovery to a real adapter, recording the call
    sequence, the exact forwarded fence OBJECT, the ACTIVE guard lease + the
    fence-closure lease (for same-object proof), the full forwarded inputs, and
    the live dispatch/status row observed at the reconcile gate."""

    def __init__(self, real: CommunityGlobalDiscoveryRecovery) -> None:
        self._real = real
        self.store = None
        self.run_id: str | None = None
        self.calls: list[str] = []
        self.fences: dict[str, object] = {}
        self.forwarded: dict = {}
        self.active_lease = None
        self.fence_lease = None
        self.observed_status = None
        self.reconcile_gate: threading.Event | None = None
        self.reconcile_entered = threading.Event()
        # Optional hook (B6): wrap the physical fence forwarded to
        # recover_and_cutover so a test can inject a REAL external writer-fence
        # loss at a precise point (e.g. right after the pointer crosses).
        self.fence_wrap = None

    def inspect_live_artifact(self):
        return self._real.inspect_live_artifact()

    def current_snapshot_fingerprint(self):
        return self._real.current_snapshot_fingerprint()

    def reconcile_attempt_artifacts(self, **kwargs):
        self.calls.append("reconcile_artifacts")
        fence = kwargs["fence_check"]
        self.fences["reconcile"] = fence
        # R6.3: inside the guard on the native thread — the active lease and the
        # fence-closure lease must be the SAME object.
        self.active_lease = _active_guard_lease()
        self.fence_lease = _lease_from_fence(fence)
        # R6.7: causally observe the live control status at the reconcile gate.
        if self.store is not None and self.run_id is not None:
            self.observed_status = self.store.get_status(run_id=self.run_id)
        self.reconcile_entered.set()
        if self.reconcile_gate is not None:
            self.reconcile_gate.wait(timeout=6)
        # Delegate to the REAL adapter reconciliation (a no-op reconciler shows).
        return self._real.reconcile_attempt_artifacts(**kwargs)

    def rebuild_candidate_and_cutover(self, **kwargs):
        self.calls.append("rebuild_candidate_and_cutover")
        return self._real.rebuild_candidate_and_cutover(**kwargs)

    def reconcile_attempt_terminal_truth(self, **kwargs):
        # Allow-deadline terminalization re-enters the native op with
        # reconcile_only=True; delegate to the real adapter (records the call).
        self.calls.append("reconcile_attempt_terminal_truth")
        self.fences["terminal_truth"] = kwargs.get("fence_check")
        return self._real.reconcile_attempt_terminal_truth(**kwargs)

    def recover_and_cutover(self, **kwargs):
        self.calls.append("recover_and_cutover")
        self.fences["recover"] = kwargs["fence_check"]
        self.forwarded = {k: kwargs[k] for k in kwargs if k != "fence_check"}
        if self.fence_wrap is not None:
            kwargs = {**kwargs, "fence_check": self.fence_wrap(kwargs["fence_check"])}
        return self._real.recover_and_cutover(**kwargs)

    def reconcile_predecessor_and_complete(self, **kwargs):
        self.calls.append("reconcile_predecessor_and_complete")
        self.fences["predecessor"] = kwargs.get("fence_check")
        self.forwarded = {k: kwargs[k] for k in kwargs if k != "fence_check"}
        if self.fence_wrap is not None:
            kwargs = {**kwargs, "fence_check": self.fence_wrap(kwargs["fence_check"])}
        return self._real.reconcile_predecessor_and_complete(**kwargs)


def _make_engine(db_path: Path):
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 5.0},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Board.__table__,
            GlobalDiscoveryRecoveryAttempt.__table__,
            GlobalDiscoveryRecoverySlot.__table__,
            GlobalDiscoveryRecoveryDispatch.__table__,
            GlobalDiscoveryRecoveryTransition.__table__,
        ],
    )
    return engine


def _read_recovery_dispatch(engine, run_id):
    """Causally read the live RECOVERY dispatch row from the real SQL store."""

    with engine.connect() as conn:
        row = (
            conn.execute(
                select(GlobalDiscoveryRecoveryDispatch).where(
                    GlobalDiscoveryRecoveryDispatch.run_id == run_id,
                    GlobalDiscoveryRecoveryDispatch.stage
                    == RecoveryDispatchStage.RECOVERY.value,
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


def _read_recovery_dispatch_at(engine, run_id, epoch, attempt_id):
    """B8.2: read the RECOVERY dispatch row for one EXACT run+epoch+attempt (never
    a first-row helper) so per-epoch dispatch identity/claim bindings are exact."""

    with engine.connect() as conn:
        row = (
            conn.execute(
                select(GlobalDiscoveryRecoveryDispatch).where(
                    GlobalDiscoveryRecoveryDispatch.run_id == run_id,
                    GlobalDiscoveryRecoveryDispatch.epoch == int(epoch),
                    GlobalDiscoveryRecoveryDispatch.attempt_id == attempt_id,
                    GlobalDiscoveryRecoveryDispatch.stage
                    == RecoveryDispatchStage.RECOVERY.value,
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


def _sha_file(path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_attempt_row(engine, run_id, epoch):
    """Read a SPECIFIC epoch's durable attempt row (the latest-only status
    projection cannot expose a superseded predecessor)."""

    with engine.connect() as conn:
        row = (
            conn.execute(
                select(GlobalDiscoveryRecoveryAttempt).where(
                    GlobalDiscoveryRecoveryAttempt.run_id == run_id,
                    GlobalDiscoveryRecoveryAttempt.epoch == epoch,
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


_B61_NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _b61_claimed_running_recovery(store, run_id, *, budget_ms=60_000):
    """Drive a REAL store to a claimed RUNNING RECOVERY dispatch (prepare ->
    enqueue -> claim RECOVERY), mirroring the production dispatch choreography, so
    the terminal override layers can be exercised deterministically."""

    # Pin the store's transactional wall clock to the fixed base so the prepared
    # manifest never appears stale (like ``prepared_recovery_admitter`` does).
    store._wall_clock = lambda: _B61_NOW + timedelta(seconds=3)  # noqa: SLF001
    admitted, created = store.admit_preparation(
        RecoveryPreparationCommand(
            binding=RecoveryRunBinding(run_id=run_id, actor_id="agent-b61"),
            admitted_at=_B61_NOW,
            counts=RecoveryProgressCounts(sources_total=1),
            attempt_budget_ms=budget_ms,
        )
    )
    assert created is True
    prep = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.PREPARATION,
        worker_id="b61-prep",
        claimed_at=_B61_NOW,
        claim_expires_at=_B61_NOW + timedelta(seconds=30),
    )
    assert prep is not None
    store.mark_preparing(
        run_id=admitted.run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=prep.claim_token,
        at=_B61_NOW,
    )
    prepared = store.complete_preparation(
        run_id=admitted.run_id,
        attempt_id=admitted.attempt_id,
        epoch=admitted.epoch,
        claim_token=prep.claim_token,
        completed_at=_B61_NOW + timedelta(seconds=1),
        result=RecoveryPreparedResult(
            manifest_ref=f"manifest://{run_id}",
            preflight_hash=f"{run_id}-preflight",
            snapshot_fingerprint=f"sha256:{run_id}",
            prepared_at=_B61_NOW + timedelta(seconds=1),
            expires_at=_B61_NOW + timedelta(seconds=301),
            counts=admitted.counts,
        ),
    )
    store.enqueue_execution(
        RecoveryStartCommand(
            binding=replace(
                prepared.binding,
                confirmation_fingerprint="sha256:b61-confirm",
                reason="b61 dispatch",
            ),
            started_at=_B61_NOW + timedelta(seconds=2),
            counts=prepared.counts,
            attempt_budget_ms=budget_ms,
            expected_epoch=prepared.epoch,
            confirmed_by_actor_id="agent-confirmer",
            confirmation_consumed_at=_B61_NOW + timedelta(seconds=2),
        )
    )
    claim = store.claim_next_dispatch(
        stage=RecoveryDispatchStage.RECOVERY,
        worker_id="b61-rec",
        claimed_at=_B61_NOW + timedelta(seconds=3),
        claim_expires_at=_B61_NOW + timedelta(seconds=33),
    )
    assert claim is not None
    return claim


def _b61_pending_sentinel(counts):
    """The EXACT unknown post-pointer reconciliation-pending sentinel tuple."""

    return RecoveryWorkerResult(
        outcome=RecoveryTerminalOutcome.PARTIAL,
        reason_code="recovery_physical_reconciliation_pending",
        retryable=False,
        counts=counts,
        physical_truth=None,
    )


class _PreparedRevoker:
    def revoke_prepared(self, **_kwargs) -> None:
        return None

    def is_prepared_revoked(self, **_kwargs) -> bool:
        return False


def _command(run_id, *, started_at, expected_epoch=None, attempt_budget_ms=None):
    kwargs = {}
    if expected_epoch is not None:
        kwargs["expected_epoch"] = expected_epoch
    if attempt_budget_ms is not None:
        kwargs["attempt_budget_ms"] = attempt_budget_ms
    return RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id=run_id,
            actor_id="agent-r6",
            confirmation_fingerprint=f"sha256:{run_id}-confirm",
            manifest_ref=f"manifest://{run_id}",
            preflight_hash=f"{run_id}-preflight",
            reason="R6 real worker/claim/deadline chain",
        ),
        started_at=started_at,
        counts=RecoveryProgressCounts(sources_total=1),
        **kwargs,
    )


def _seed_epoch_inputs(artifact_store, run_id, epoch, *, live_sha, boards, counts):
    """Seed create-only durable worker inputs for the given epoch through the
    REAL ``GlobalDiscoveryRecoveryWorkerInputStore.put``."""

    store = GlobalDiscoveryRecoveryWorkerInputStore(artifact_store)
    command = RecoveryStartCommand(
        binding=RecoveryRunBinding(
            run_id=run_id,
            actor_id="agent-r6",
            confirmation_fingerprint=f"sha256:{run_id}-confirm",
            manifest_ref=f"manifest://{run_id}",
            preflight_hash=f"{run_id}-preflight",
            reason="R6 real worker/claim/deadline chain",
        ),
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        counts=RecoveryProgressCounts(sources_total=1),
        expected_epoch=epoch,
        confirmed_by_actor_id="agent-r6",
        confirmation_consumed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.put(
        GlobalDiscoveryRecoveryWorkerInputs(
            command=command,
            expected_live_sha256=live_sha,
            boards=boards,
            terminal_counts=counts,
        )
    )


class _RuntimeBundle:
    def __init__(self, *, engine, store, worker, control, spy, recording_port):
        self.engine = engine
        self.store = store
        self.worker = worker
        self.control = control
        self.spy = spy
        self.recording_port = recording_port

    def close(self):
        self.worker.close(timeout_seconds=3.0)
        self.engine.dispose()


def _build_bundle(
    *, engine, artifact_store, live, factory, run_id, spy=None, heartbeat_interval_ms=50
):
    # R8-B7.1: ``factory=None`` selects the PRODUCTION composition — a real
    # ``CommunityGlobalDiscoveryRuntime`` as the live runtime and the recovery
    # adapter's DEFAULT runtime factory (real Ladybug for every candidate /
    # readback).  No JSON fake participates anywhere in that mode.
    if factory is None:
        global_runtime = CommunityGlobalDiscoveryRuntime(
            graph_path_provider=lambda: live
        )
        real = CommunityGlobalDiscoveryRecovery(
            global_runtime=global_runtime,
            graph_path_provider=lambda: live,
        )
    else:
        global_runtime = _UnreadableLiveRuntime(live)
        real = CommunityGlobalDiscoveryRecovery(
            global_runtime=global_runtime,  # type: ignore[arg-type]
            graph_path_provider=lambda: live,
            runtime_factory=factory,  # type: ignore[arg-type]
        )
    spy = spy or _SpyRecovery(real)
    # REAL durable input provider (loads create-only inputs by (run_id, epoch)).
    input_provider = CommunityDurableRecoveryInputProvider(
        artifact_store=artifact_store
    )
    store = SQLAlchemyRecoveryRunStore(
        engine=engine,
        prepared_revoker=_PreparedRevoker(),
        # REAL production handoff wiring (identical to build_community_recovery_runtime).
        resume_input_handoff=getattr(input_provider, "handoff_resume_inputs", None),
    )
    spy.store = store
    spy.run_id = run_id
    native_op = CommunityGlobalDiscoveryRecoveryNativeOperation(
        recovery=spy,  # type: ignore[arg-type]
        input_provider=input_provider,
    )
    worker = CommunityRecoveryWorker(
        store=store,
        native_operation=native_op,
        heartbeat_interval_ms=heartbeat_interval_ms,
    )
    control = RecoveryControlPlane(store=store, dispatcher=worker)
    return _RuntimeBundle(
        engine=engine,
        store=store,
        worker=worker,
        control=control,
        spy=spy,
        recording_port=None,
    ), global_runtime


def _admit_and_start(bundle, prepared_recovery_admitter, command):
    prepared_recovery_admitter(bundle.store, command)
    bundle.control.start(command)


_SHA_RE = __import__("re").compile(r"^[0-9a-f]{64}$")


def _assert_incoherent_seed_rebuild(
    journal,
    boards,
    attempt_id,
    *,
    source_fingerprint,
    semantic_fingerprint,
    active,
    active_snapshot_sha,
):
    """B7: a marked incoherent primary falls back to an AUTHORITATIVE-SEED rebuild
    whose terminal journal is bound by EXACT equality (never regex-only) to the
    real active bytes / pointer / manifest and the FULL canonical seed projection —
    a fabricated self-consistent graph or empty candidate cannot pass."""

    # B7: exact stable kind for the seed-rebuild path (code vocabulary), not just
    # "not an adoption".
    assert journal["kind"] == "seed_rebuild"
    assert journal["phase"] == "completed"
    assert journal["outcome"] == "completed"
    assert journal.get("rollback_performed") is False
    assert journal["attempt_id"] == attempt_id
    # EXACT equality against the reopened active generation identity/bytes.
    assert journal["candidate_sha256"] == active_snapshot_sha
    assert journal["candidate_sha256"] == _sha_re_ok(journal["candidate_sha256"])
    assert journal["generation_manifest_sha256"] == active.manifest_sha256
    assert journal["semantic_fingerprint"] == semantic_fingerprint
    assert journal["source_fingerprint"] == source_fingerprint
    assert isinstance(journal.get("schema_object_count"), int)
    assert journal["schema_object_count"] > 0
    # The full materialized board/digest/link payload from EVERY seed (per-board
    # exact counts): one board with its digests and one containment link each.
    expected_counts = {
        seed.board_id: {
            "boards": 1,
            "digests": len(seed.digests),
            "links": len(seed.digests),
        }
        for seed in boards
    }
    assert journal["counts_by_board"] == expected_counts


def _sha_re_ok(value: str) -> str:
    assert _SHA_RE.match(str(value)), value
    return value


# --- adoption + incoherent (real lock + real inputs + delegating reconciler) --


@pytest.mark.parametrize("mode", ["adoption", "incoherent"])
def test_r6_worker_chain_real_wiring_terminal(
    tmp_path, prepared_recovery_admitter, mode
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    if mode == "adoption":
        live.write_bytes(b"complete-primary")
        live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
        shared = _coherent_adopt_state()

        def factory(path: Path):
            return _CandidateRuntime(path, shared)
    else:
        # R8-B7.1: the live primary bytes are NOT a valid persisted graph, so the
        # REAL Ladybug open of its copy fails (corrupt) -> adoption fails ->
        # authoritative-seed rebuild.  ``factory = None`` selects the PRODUCTION
        # composition: real ``CommunityGlobalDiscoveryRuntime`` + the recovery
        # adapter's DEFAULT runtime factory.  The rebuild materializes both
        # non-identical seeds into REAL Ladybug graph/WAL bytes and every fresh
        # readback derives its projection solely from those hashed bytes — no
        # JSON fake participates anywhere in this proof.
        live.write_bytes(b"partial-primary")
        live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
        factory = None

    write_bootstrap_marker(live)

    run_id = f"gdr_r6{mode}"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    engine = _make_engine(tmp_path / f"{run_id}.sqlite3")
    recording_port = _RecordingWriteLockPort(CommunityLocalWriteLockPort())

    # Incoherent fallback materializes the seeds -> use TWO distinct seeds so a
    # board-ID-only mutant dies on exact per-board counts equality.  The real
    # Ladybug schema requires 384-dim embeddings, hence the REAL seed pair.
    boards = _two_real_seeds() if mode == "incoherent" else (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )

    with _r6_env(recording_port, tmp_path / "kgbase"):
        bundle, global_runtime = _build_bundle(
            engine=engine, artifact_store=artifact_store, live=live,
            factory=factory, run_id=run_id,
        )
        bundle.recording_port = recording_port
        try:
            command = _command(run_id, started_at=datetime.now(timezone.utc))
            _admit_and_start(bundle, prepared_recovery_admitter, command)
            terminal = _wait_until(
                bundle.control,
                run_id=run_id,
                predicate=lambda s: s.state
                in (RecoveryRunState.SUCCESS, RecoveryRunState.FAILED),
                timeout_seconds=60.0,
            )
        finally:
            bundle.close()

    assert terminal.state is RecoveryRunState.SUCCESS
    attempt_id = recovery_attempt_id(run_id, terminal.epoch)
    spy = bundle.spy
    assert spy.calls == ["reconcile_artifacts", "recover_and_cutover"]

    journal_path = (
        live.parent / "quarantine" / "global-discovery"
        / attempt_id / "recovery_journal.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if mode == "adoption":
        assert journal["kind"] == "adopt_complete_primary"
    else:
        # B7: bind the seed-rebuild journal to the EXACT real physical truth.
        from okto_pulse.community.adapters.global_discovery_layout import (
            canonical_sha256,
        )
        from okto_pulse.community.adapters.global_discovery_recovery import (
            _snapshot,
        )

        ordered = tuple(sorted(boards, key=lambda b: b.board_id))
        # R8-B7.1: the validator recovery uses the REAL live runtime and the
        # DEFAULT (real Ladybug) runtime factory — no custom factory, no fake.
        validator = CommunityGlobalDiscoveryRecovery(
            global_runtime=CommunityGlobalDiscoveryRuntime(
                graph_path_provider=lambda: live
            ),
            graph_path_provider=lambda: live,
        )
        active = read_active_generation(live)
        assert active is not None
        source_fp = canonical_sha256([b.to_dict() for b in ordered])
        expected_sem_fp = canonical_sha256(
            validator._expected_semantic_projection(ordered)
        )
        active_snapshot_sha = _snapshot(active.graph_path).sha256
        _assert_incoherent_seed_rebuild(
            journal,
            boards,
            attempt_id,
            source_fingerprint=source_fp,
            semantic_fingerprint=expected_sem_fp,
            active=active,
            active_snapshot_sha=active_snapshot_sha,
        )
        # Reopen the active pointer/generation/manifest and prove mutual identity:
        # the terminal candidate SHA IS the actual active snapshot SHA, and the
        # journal's generation is the ACTIVE generation.
        assert journal["candidate_sha256"] == active_snapshot_sha
        assert journal["generation_manifest_sha256"] == active.manifest_sha256
        assert journal["generation_id"] == active.generation_id
        # R8-B7/WAL scrutiny THROUGH THE PRODUCTION RESUME: restore the marker
        # (crash-before-clear) and drive the PUBLIC reader over the pristine
        # cutover bytes.  Production validates with a REAL fresh readback and —
        # per the new post-close binding — may only bless/clear if the FINALLY
        # persisted bytes still equal the journal SHA after that readback close.
        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            bootstrap_marker_present,
        )

        write_bootstrap_marker(live)
        # Crash-window: a leftover scratch directory from a crashed prior
        # validation must be cleaned fail-closed by the resume itself.
        journal_dir = (
            live.parent / "quarantine" / "global-discovery" / attempt_id
        )
        leftover_scratch = journal_dir / "resume-validate-scratch-orphan"
        leftover_scratch.mkdir(parents=True, exist_ok=True)
        (leftover_scratch / "garbage.bin").write_bytes(b"crashed-leftover")
        # R8-B7.7 (#1): the completed+marker resume runs in a brand-new COLD
        # process through the SAME public reader — and must converge: success +
        # cleared marker, with SHA/pointer/manifest/raw byte-identical (the
        # scratch-copy validation cannot move the active bytes).
        raw_before_cold = _raw_active_state(live)
        pointer_path_p = live.parent / "active_generation.json"
        manifest_path_p = (
            live.parent / "discovery.generations" / active.generation_id
            / "generation_manifest.json"
        )
        cold = _run_cold_resume(
            tmp_path, mode="terminal", live=live, kg_base=tmp_path / "kgbase",
            run_id=run_id, epoch=terminal.epoch, attempt_id=attempt_id,
            live_sha=live_sha,
        )
        assert cold["outcome"] == "completed"
        assert bootstrap_marker_present(live) is False
        assert _snapshot(active.graph_path).sha256 == journal["candidate_sha256"]
        assert _raw_active_state(live) == raw_before_cold
        assert not leftover_scratch.exists()
        assert not list(journal_dir.glob("resume-validate-scratch*"))
        # R8-B7.7 (#4): idempotent WARMED second resume — full freeze of journal
        # / pointer / manifest / raw map, ZERO factory/runtime constructions,
        # ZERO artifact fsyncs, and the re-read active generation identity is
        # unchanged (never just outcome+marker on a stale object).
        journal_path_p = journal_dir / "recovery_journal.json"
        frozen_journal = journal_path_p.read_bytes()
        frozen_pointer = pointer_path_p.read_bytes()
        frozen_manifest = manifest_path_p.read_bytes()
        frozen_raw = _raw_active_state(live)
        factory_calls: list[str] = []
        real_factory = validator._runtime_factory

        def spying_factory(path):
            factory_calls.append(str(path))
            return real_factory(path)

        import okto_pulse.community.adapters.global_discovery_recovery as gdr_mod

        fsync_calls: list[str] = []
        real_fsync_artifacts = gdr_mod._fsync_artifacts

        def spying_fsync(path, *args, **kwargs):
            fsync_calls.append(str(path))
            return real_fsync_artifacts(path, *args, **kwargs)

        # R8-B7.8 (a): the no-op must also prove ZERO journal writes, ZERO
        # directory fsyncs, ZERO cutover notes and ZERO marker clears.
        writer_calls: list[str] = []
        real_writer_noop = gdr_mod._write_journal_with_directory_fsync

        def spying_writer(path, payload, **kwargs):
            writer_calls.append(str(path))
            return real_writer_noop(path, payload, **kwargs)

        fsync_dir_calls: list[str] = []
        real_fsync_dir = gdr_mod.fsync_directory

        def spying_fsync_dir(path, *args, **kwargs):
            fsync_dir_calls.append(str(path))
            return real_fsync_dir(path, *args, **kwargs)

        cutover_notes: list[str] = []
        real_note = validator._global_runtime.note_successful_generation_cutover

        def spying_note(*args, **kwargs):
            cutover_notes.append("note")
            return real_note(*args, **kwargs)

        clear_calls: list[str] = []
        real_clear = validator._clear_marker_crash_conservatively

        def spying_clear(*args, **kwargs):
            clear_calls.append("clear")
            return real_clear(*args, **kwargs)

        validator._runtime_factory = spying_factory
        gdr_mod._fsync_artifacts = spying_fsync
        gdr_mod._write_journal_with_directory_fsync = spying_writer
        gdr_mod.fsync_directory = spying_fsync_dir
        validator._global_runtime.note_successful_generation_cutover = spying_note
        validator._clear_marker_crash_conservatively = spying_clear
        try:
            with _r6_env(
                _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
                tmp_path / "kgbase",
            ):
                with global_discovery_writer_scope(
                    operation="global_discovery_recovery",
                    owner_id=f"{run_id}:b7-resume-probe-2",
                    ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                    admin_lane=True,
                ) as probe_lease2:
                    probe2 = validator.reconcile_attempt_terminal_truth(
                        run_id=run_id, epoch=terminal.epoch,
                        attempt_id=attempt_id,
                        expected_live_sha256=live_sha, boards=ordered,
                        fence_check=probe_lease2.assert_fenced,
                    )
        finally:
            validator._runtime_factory = real_factory
            gdr_mod._fsync_artifacts = real_fsync_artifacts
            gdr_mod._write_journal_with_directory_fsync = real_writer_noop
            gdr_mod.fsync_directory = real_fsync_dir
            validator._global_runtime.note_successful_generation_cutover = (
                real_note
            )
            del validator._clear_marker_crash_conservatively
        assert probe2 is not None
        assert probe2.outcome == "completed"
        assert bootstrap_marker_present(live) is False
        assert factory_calls == []      # zero runtime constructions
        assert fsync_calls == []        # zero artifact fsyncs
        assert writer_calls == []       # zero journal writes
        assert fsync_dir_calls == []    # zero directory fsyncs
        assert cutover_notes == []      # zero cutover notes
        assert clear_calls == []        # zero marker clears
        assert journal_path_p.read_bytes() == frozen_journal   # zero clear/write
        assert pointer_path_p.read_bytes() == frozen_pointer
        assert manifest_path_p.read_bytes() == frozen_manifest
        assert _raw_active_state(live) == frozen_raw
        reread_active = read_active_generation(live)
        assert reread_active is not None
        assert reread_active.generation_id == active.generation_id
        assert reread_active.manifest_sha256 == active.manifest_sha256
        assert _snapshot(reread_active.graph_path).sha256 == (
            journal["candidate_sha256"]
        )
        # R8-B7.1: reopen a FRESH REAL Ladybug runtime on the ACTUAL active graph
        # bytes and prove the FULL canonical projection — every board/digest/
        # title/summary/node-type/embedding/link field — equals BOTH non-identical
        # seeds, derived SOLELY from the hashed physical bytes.  _validate_runtime
        # compares field-by-field and raises on any mismatch.  (Embedding reads
        # require the vector extension => the REAL writer fence must be active.)
        # A FRESH recording port so the main-flow exactly-one-release contract
        # below stays scoped to the recovery run's own lease.
        with _r6_env(
            _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
            tmp_path / "kgbase",
        ):
            with global_discovery_writer_scope(
                operation="global_discovery_recovery",
                owner_id=f"{run_id}:b7-readback",
                ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                admin_lane=True,
            ):
                readback = validator._runtime_factory(active.graph_path)
                try:
                    schema_count, counts_by_board, sem_fp = (
                        validator._validate_runtime(readback, ordered)
                    )
                    actual_projection = validator._actual_semantic_projection(
                        readback
                    )
                finally:
                    readback.close()
        assert sem_fp == expected_sem_fp
        assert sem_fp == journal["semantic_fingerprint"]
        assert journal["expected_semantic_fingerprint"] == expected_sem_fp
        assert counts_by_board == journal["counts_by_board"]
        assert schema_count == journal["schema_object_count"]
        # R8-B7.3: compare the reopened REAL projection against an INDEPENDENTLY
        # hand-built expected dict — every board/digest field, the exact digest
        # id formula, independently recomputed embedding hashes and every link
        # of BOTH non-identical seeds (production supplies only the ACTUAL side).
        import hashlib as _hl
        import struct as _st

        def _ind_emb_sha(values) -> str:
            digest = _hl.sha256()
            items = tuple(float(item) for item in values)
            digest.update(len(items).to_bytes(8, "big"))
            for item in items:
                digest.update(_st.pack("!d", 0.0 if item == 0.0 else item))
            return digest.hexdigest()

        independent_expected = {
            "boards": sorted(
                [
                    {
                        "board_id": seed.board_id,
                        "name": seed.board_name or seed.board_id,
                        "summary": seed.summary,
                        "decision_count": len(seed.digests),
                        "summary_embedding_sha256": _ind_emb_sha(
                            seed.summary_embedding
                        ),
                    }
                    for seed in ordered
                ],
                key=lambda row: row["board_id"],
            ),
            "digests": sorted(
                [
                    {
                        "id": f"dd_{seed.board_id[:8]}_{digest.original_node_id}",
                        "board_id": seed.board_id,
                        "original_node_id": digest.original_node_id,
                        "title": digest.title,
                        "one_line_summary": digest.summary,
                        "node_type": digest.node_type,
                        "graph_layer": digest.graph_layer,
                        "embedding_sha256": _ind_emb_sha(digest.embedding),
                    }
                    for seed in ordered
                    for digest in seed.digests
                ],
                key=lambda row: (
                    row["id"], row["board_id"], row["original_node_id"]
                ),
            ),
            "links": sorted(
                [
                    {
                        "board_id": seed.board_id,
                        "digest_id": (
                            f"dd_{seed.board_id[:8]}_{digest.original_node_id}"
                        ),
                        "digest_board_id": seed.board_id,
                        "original_node_id": digest.original_node_id,
                    }
                    for seed in ordered
                    for digest in seed.digests
                ],
                key=lambda row: (
                    row["board_id"], row["digest_id"],
                    row["digest_board_id"], row["original_node_id"],
                ),
            ),
        }
        assert actual_projection == independent_expected
        # NOTE: after the marker is cleared, LATER readback closes may legitimately
        # grow/checkpoint the WAL (the design's marker-absent no-op tolerance);
        # the binding proven above is that the BLESSING itself only happened over
        # finally-persisted matching bytes.
    assert journal["phase"] == "completed"

    # R6.3: the SAME python lease object was active in the guard AND closed over
    # by the fence, across reconcile + physical operation.
    assert spy.fences["reconcile"] is spy.fences["recover"]
    assert spy.active_lease is not None
    assert spy.fence_lease is spy.active_lease

    # Exact FULL forwarded inputs (full seed tuple equality, not just board_id).
    assert spy.forwarded["run_id"] == run_id
    assert spy.forwarded["epoch"] == terminal.epoch
    assert spy.forwarded["attempt_id"] == attempt_id
    assert spy.forwarded["expected_live_sha256"] == live_sha
    assert _sorted_boards(spy.forwarded["boards"]) == _sorted_boards(boards)

    # R6.7: the live control status observed at the reconcile gate is the exact
    # claimed RECOVERY attempt.
    observed = spy.observed_status
    assert observed is not None
    assert observed.run_id == run_id
    assert observed.epoch == terminal.epoch
    assert observed.attempt_id == attempt_id
    assert observed.state is RecoveryRunState.RUNNING
    assert observed.phase is RecoveryRunPhase.CUTOVER

    # R6.1/6.2: exact acquire contract + exactly one release of that exact token;
    # the captured lease is released.
    owner_id = f"{run_id}:{attempt_id}"
    owner_token = _assert_exact_acquire_contract(recording_port, owner_id=owner_id)
    assert owner_token == spy.fence_lease.owner_token
    _assert_exactly_one_release(
        recording_port, owner_token=owner_token, lease=spy.fence_lease
    )


# --- cancellation / deadline: fence raises before the physical operation ------


@pytest.mark.parametrize("interrupt", ["cancel", "deadline"])
def test_r6_worker_chain_fence_raises_before_physical_op(
    tmp_path, prepared_recovery_admitter, interrupt, monkeypatch
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = f"gdr_r6{interrupt}"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    engine = _make_engine(tmp_path / f"{run_id}.sqlite3")
    recording_port = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    boards = (_seed(),)
    counts = RecoveryProgressCounts(sources_total=1, sources_processed=1)
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )

    real = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    spy = _SpyRecovery(real)
    spy.reconcile_gate = threading.Event()

    with _r6_env(recording_port, tmp_path / "kgbase"):
        bundle, global_runtime = _build_bundle(
            engine=engine, artifact_store=artifact_store, live=live,
            factory=factory, run_id=run_id, spy=spy,
            # A wide heartbeat so the gate-held boundary proof completes before any
            # heartbeat fires — only the native op drives terminalization.
            heartbeat_interval_ms=5_000,
        )
        bundle.recording_port = recording_port
        import time as _time

        # A monotonic clock that is SETTABLE for the boundary proof, then AFFINE
        # (base + real elapsed) so allow-deadline terminalization keeps the same
        # live SQL claim and the worker drains — no per-call increment distortion.
        clock = {"mode": "fixed", "t": 1000.0, "base": 0.0, "real0": 0.0}

        def _mono():
            if clock["mode"] == "affine":
                return clock["base"] + (_time.monotonic() - clock["real0"])
            return clock["t"]

        monkeypatch.setattr(bundle.worker, "_monotonic_clock", _mono)
        try:
            started_at = datetime.now(timezone.utc)
            # Deadline case: a SHORT attempt budget strictly below the RECOVERY
            # claim lease (15s) so the deadline is reached while the SQL claim is
            # still valid (coherent allow-deadline terminalization).
            deadline_budget_ms = 4_000
            command = _command(
                run_id,
                started_at=started_at,
                attempt_budget_ms=(
                    deadline_budget_ms if interrupt == "deadline" else None
                ),
            )
            _admit_and_start(bundle, prepared_recovery_admitter, command)
            assert spy.reconcile_entered.wait(timeout=5.0)
            # The EXACT forwarded fence captured at the reconcile gate.
            fence = spy.fences["reconcile"]
            expected_state = (
                RecoveryRunState.CANCELLED
                if interrupt == "cancel"
                else RecoveryRunState.TIMEOUT
            )

            if interrupt == "cancel":
                # R6.5: the same fence PASSES before cancel...
                fence()
                bundle.control.cancel(
                    run_id=run_id,
                    expected_epoch=1,
                    requested_at=started_at + timedelta(seconds=1),
                    requested_by_actor_id="operator-r6",
                    reason="R6 cancellation prevents physical op",
                )
                # ...and RAISES the exact typed cancel code after the real cancel.
                with pytest.raises(RecoveryWorkerFenceError) as exc_info:
                    fence()
                assert exc_info.value.code == "cancel_requested"
            else:
                # R6.1: with a SHORT budget, the RECOVERY claim lease is capped to
                # the attempt deadline (production ``min(claimed+lease, deadline)``)
                # instead of expiring long before it (the original 10-min-budget
                # bug).  The claim therefore lives EXACTLY as long as the attempt
                # (>= the deadline), so the deadline is reached with the claim
                # still valid — coherent allow-deadline terminalization.
                dispatch = _read_recovery_dispatch(bundle.store.engine, run_id)
                assert dispatch is not None
                assert dispatch["stage"] == RecoveryDispatchStage.RECOVERY.value
                assert dispatch["state"] == RecoveryDispatchState.CLAIMED.value
                claim_expires = dispatch["claim_expires_at"]
                if claim_expires.tzinfo is None:
                    claim_expires = claim_expires.replace(tzinfo=timezone.utc)
                attempt_deadline = spy.observed_status.active_deadline_at
                if attempt_deadline.tzinfo is None:
                    attempt_deadline = attempt_deadline.replace(tzinfo=timezone.utc)
                assert claim_expires >= attempt_deadline
                # And with the short budget the claim is capped to the deadline,
                # NOT expiring earlier (which was the original stale-claim bug).
                assert claim_expires == attempt_deadline

                # R6.4: boundary test of the deadline fence — the EXACT deadline.
                deadline = _deadline_from_fence(fence)
                assert deadline is not None
                clock["t"] = deadline - 0.001  # deadline - 1ms: MUST pass
                fence()
                clock["t"] = deadline  # exactly at deadline: MUST raise (>=)
                with pytest.raises(RecoveryWorkerFenceError) as exc_info:
                    fence()
                assert exc_info.value.code == "attempt_deadline_exhausted"
                # Switch to an AFFINE clock anchored at the deadline + real elapsed
                # (never per-call increments): stays at/past the deadline while
                # elapsed wall time stays coherent, so the claim remains valid and
                # the worker drains.
                clock["base"] = deadline
                clock["real0"] = _time.monotonic()
                clock["mode"] = "affine"

            spy.reconcile_gate.set()
            terminal = _wait_until(
                bundle.control,
                run_id=run_id,
                predicate=lambda s: s.state is expected_state,
            )
        finally:
            spy.reconcile_gate.set()
            bundle.close()

    # The physical operation was PREVENTED — recover_and_cutover never ran; live
    # bytes, WAL, marker, and active pointer are all unchanged (no cutover).
    assert "recover_and_cutover" not in spy.calls
    assert terminal.state is expected_state
    assert live.read_bytes() == b"complete-primary"
    assert live.with_name(live.name + ".wal").read_bytes() == b"complete-wal"
    assert bootstrap_marker_present(live) is True
    assert not global_runtime.successful_cutovers
    from okto_pulse.community.adapters.global_discovery_layout import (
        read_active_generation,
    )

    assert read_active_generation(live) is None  # never switched a pointer
    # Same claim/attempt identity preserved.
    assert terminal.attempt_id == recovery_attempt_id(run_id, 1)
    assert terminal.epoch == 1

    if interrupt == "deadline":
        # R6.1: exact deadline classification — TIMEOUT with the exact reason and
        # exactly the full attempt budget consumed (not a multistate terminal).
        assert terminal.state is RecoveryRunState.TIMEOUT
        assert terminal.terminal_outcome == "timeout"
        assert terminal.reason_code == "recovery_attempt_budget_exhausted"
        assert terminal.active_elapsed_ms == deadline_budget_ms
    else:
        assert terminal.state is RecoveryRunState.CANCELLED
        assert terminal.terminal_outcome == "cancelled"

    # The real lock was released EXACTLY once for the epoch-1 owner token.
    owner_token = _acquisition_for_owner(
        recording_port, f"{run_id}:{recovery_attempt_id(run_id, 1)}"
    )[1].owner_token
    _assert_exactly_one_release(
        recording_port, owner_token=owner_token, lease=spy.fence_lease
    )


# --- stale dispatch claim: a stolen SQL claim must PREVENT physical cutover ---


def test_r6_worker_chain_stale_dispatch_claim_steps_aside_without_cutover(
    tmp_path, prepared_recovery_admitter
):
    """R6.6 (split-brain / fabricated-physical-truth defense): while the attempt
    deadline is still valid, ANOTHER worker re-claims the RECOVERY dispatch (the
    SQL claim token is replaced).  The SAME captured production fence must raise
    exactly ``stale_dispatch_claim``, the native op must step aside WITHOUT any
    physical cutover or terminalization, and it must release the writer lease
    exactly once for its own owner token — so the stolen claim cannot manufacture
    a competing physical truth."""

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_r6stale"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    engine = _make_engine(tmp_path / f"{run_id}.sqlite3")
    recording_port = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    boards = (_seed(),)
    counts = RecoveryProgressCounts(sources_total=1, sources_processed=1)
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )

    real = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    spy = _SpyRecovery(real)
    spy.reconcile_gate = threading.Event()

    with _r6_env(recording_port, tmp_path / "kgbase"):
        bundle, global_runtime = _build_bundle(
            engine=engine, artifact_store=artifact_store, live=live,
            factory=factory, run_id=run_id, spy=spy,
            # Wide heartbeat: only the native op drives the outcome, no checkpoint
            # settles a competing terminal while the gate is held.
            heartbeat_interval_ms=5_000,
        )
        try:
            started_at = datetime.now(timezone.utc)
            command = _command(run_id, started_at=started_at)
            _admit_and_start(bundle, prepared_recovery_admitter, command)
            assert spy.reconcile_entered.wait(timeout=5.0)
            fence = spy.fences["reconcile"]

            # The live RECOVERY claim is CLAIMED and (still) valid.
            dispatch = _read_recovery_dispatch(bundle.store.engine, run_id)
            assert dispatch is not None
            assert dispatch["stage"] == RecoveryDispatchStage.RECOVERY.value
            assert dispatch["state"] == RecoveryDispatchState.CLAIMED.value
            original_token = dispatch["claim_token"]
            assert isinstance(original_token, str) and original_token

            # R6.6: ANOTHER worker re-claims the lane — replace the SQL claim token
            # while keeping the claim expiry in the future (NOT an expiry race, so
            # the run stays valid; only the token differs).
            with bundle.store.engine.begin() as conn:
                conn.execute(
                    update(GlobalDiscoveryRecoveryDispatch)
                    .where(
                        GlobalDiscoveryRecoveryDispatch.dispatch_id
                        == dispatch["dispatch_id"]
                    )
                    .values(claim_token="stolen-by-another-worker")
                )

            # The SAME captured fence now raises EXACTLY stale_dispatch_claim.
            with pytest.raises(RecoveryWorkerFenceError) as exc_info:
                fence()
            assert exc_info.value.code == "stale_dispatch_claim"

            # Release the gate: the real reconciler re-checks the same fence and the
            # native op steps aside (else: return) WITHOUT terminalizing.
            owner_token = _acquisition_for_owner(
                recording_port, f"{run_id}:{recovery_attempt_id(run_id, 1)}"
            )[1].owner_token
            spy.reconcile_gate.set()
            _wait_for_release(recording_port, owner_token)

            # The stale worker did NOT terminalize: the run stays nonterminal at the
            # same epoch/attempt (the poller would retry the same epoch after the
            # foreign claim expires — no fabricated terminal, no epoch inflation).
            status_after = bundle.control.status(run_id)
            assert status_after.state is RecoveryRunState.RUNNING
            assert status_after.terminal_outcome is None
            assert status_after.epoch == 1
            assert status_after.attempt_id == recovery_attempt_id(run_id, 1)
        finally:
            spy.reconcile_gate.set()
            bundle.close()

    # No physical cutover happened at all — bytes/WAL/marker/pointer untouched.
    assert "recover_and_cutover" not in spy.calls
    assert live.read_bytes() == b"complete-primary"
    assert live.with_name(live.name + ".wal").read_bytes() == b"complete-wal"
    assert bootstrap_marker_present(live) is True
    assert not global_runtime.successful_cutovers
    from okto_pulse.community.adapters.global_discovery_layout import (
        read_active_generation,
    )

    assert read_active_generation(live) is None  # never switched a pointer

    # The writer lease was released EXACTLY once for this owner token (the stolen
    # claim never acquired the real lease under this owner), on the captured lease.
    _assert_exactly_one_release(
        recording_port, owner_token=owner_token, lease=spy.fence_lease
    )


# --- B6.1: late cancel/deadline cannot rewrite the exact pending sentinel ------


def test_r6_b61_late_cancel_preserves_pending_sentinel_both_layers(
    recovery_store_factory, tmp_path
):
    """B6.1: a late cancel must be RECORDED but must NOT rewrite the EXACT unknown
    post-pointer reconciliation-pending sentinel to CANCELLED — proven through the
    worker ``_complete`` override AND the SQL ``_complete_recovery_in_transaction``
    override.  Any OTHER no-truth result is still rewritten to CANCELLED."""

    # SQL layer (layer 2): complete_recovery preserves the exact sentinel.
    run_a = "gdr_b61_cancel_sql"
    store_a = recovery_store_factory(f"sqlite:///{(tmp_path / (run_a + '.sqlite3')).as_posix()}")
    claim_a = _b61_claimed_running_recovery(store_a, run_a)
    store_a.request_cancel(
        run_id=run_a, expected_epoch=claim_a.epoch,
        requested_at=_B61_NOW + timedelta(seconds=40),
        requested_by_actor_id="operator-late-cancel", reason="late cancel",
    )
    cur_a = store_a.get_status(run_id=run_a)
    assert cur_a.cancel_requested_at is not None
    term_a = store_a.complete_recovery(
        dispatch_id=claim_a.dispatch_id, claim_token=claim_a.claim_token,
        expected_progress_seq=cur_a.progress_seq,
        completed_at=_B61_NOW + timedelta(seconds=41), active_elapsed_ms=1_000,
        result=_b61_pending_sentinel(cur_a.counts),
    )
    assert term_a.state is RecoveryRunState.PARTIAL
    assert term_a.terminal_outcome is RecoveryTerminalOutcome.PARTIAL
    assert term_a.reason_code == "recovery_physical_reconciliation_pending"
    assert term_a.physical_truth is None
    assert term_a.cancel_requested_at is not None  # cancel recorded, truth preserved

    # NEGATIVE (layer 2): a non-sentinel no-truth PARTIAL is still rewritten.
    run_n = "gdr_b61_cancel_neg"
    store_n = recovery_store_factory(f"sqlite:///{(tmp_path / (run_n + '.sqlite3')).as_posix()}")
    claim_n = _b61_claimed_running_recovery(store_n, run_n)
    store_n.request_cancel(
        run_id=run_n, expected_epoch=claim_n.epoch,
        requested_at=_B61_NOW + timedelta(seconds=40),
        requested_by_actor_id="operator-late-cancel", reason="late cancel",
    )
    cur_n = store_n.get_status(run_id=run_n)
    term_n = store_n.complete_recovery(
        dispatch_id=claim_n.dispatch_id, claim_token=claim_n.claim_token,
        expected_progress_seq=cur_n.progress_seq,
        completed_at=_B61_NOW + timedelta(seconds=41), active_elapsed_ms=1_000,
        result=RecoveryWorkerResult(
            outcome=RecoveryTerminalOutcome.PARTIAL,
            reason_code="global_discovery_recovery_rolled_back",
            retryable=False, counts=cur_n.counts, physical_truth=None,
        ),
    )
    assert term_n.state is RecoveryRunState.CANCELLED

    # WORKER layer (layer 1 -> chains into layer 2): worker._complete preserves it.
    run_w = "gdr_b61_cancel_worker"
    store_w = recovery_store_factory(f"sqlite:///{(tmp_path / (run_w + '.sqlite3')).as_posix()}")
    claim_w = _b61_claimed_running_recovery(store_w, run_w)
    store_w.request_cancel(
        run_id=run_w, expected_epoch=claim_w.epoch,
        requested_at=_B61_NOW + timedelta(seconds=40),
        requested_by_actor_id="operator-late-cancel", reason="late cancel",
    )
    cur_w = store_w.get_status(run_id=run_w)
    worker = CommunityRecoveryWorker(
        store=store_w, native_operation=lambda **_k: None,
        heartbeat_interval_ms=5_000,
        wall_clock=lambda: _B61_NOW + timedelta(seconds=41),
        monotonic_clock=lambda: 1_000.0,
    )
    try:
        worker._complete(
            run_id=run_w, attempt_id=claim_w.attempt_id, epoch=claim_w.epoch,
            started_monotonic=1_000.0, baseline_elapsed_ms=0,
            result=_b61_pending_sentinel(cur_w.counts),
            dispatch_id=claim_w.dispatch_id, claim_token=claim_w.claim_token,
            wall_started_at=_B61_NOW + timedelta(seconds=3),
            deadline_at_monotonic=None,
        )
    finally:
        worker.close(timeout_seconds=3.0)
    final_w = store_w.get_status(run_id=run_w)
    assert final_w.state is RecoveryRunState.PARTIAL
    assert final_w.reason_code == "recovery_physical_reconciliation_pending"
    assert final_w.physical_truth is None


def test_r6_b61_late_deadline_preserves_pending_sentinel_both_layers(
    recovery_store_factory, tmp_path
):
    """B6.1: a late deadline/budget exhaustion must NOT rewrite the exact pending
    sentinel to TIMEOUT — proven through both override layers.  Any other no-truth
    result at the deadline is still rewritten to TIMEOUT."""

    budget_ms = 60_000
    # SQL layer (layer 2): budget-exhausted completion keeps the exact sentinel.
    run_a = "gdr_b61_deadline_sql"
    store_a = recovery_store_factory(f"sqlite:///{(tmp_path / (run_a + '.sqlite3')).as_posix()}")
    claim_a = _b61_claimed_running_recovery(store_a, run_a, budget_ms=budget_ms)
    cur_a = store_a.get_status(run_id=run_a)
    term_a = store_a.complete_recovery(
        dispatch_id=claim_a.dispatch_id, claim_token=claim_a.claim_token,
        expected_progress_seq=cur_a.progress_seq,
        completed_at=_B61_NOW + timedelta(seconds=63),  # past active_deadline_at
        active_elapsed_ms=budget_ms,
        result=_b61_pending_sentinel(cur_a.counts),
    )
    assert term_a.state is RecoveryRunState.PARTIAL
    assert term_a.reason_code == "recovery_physical_reconciliation_pending"
    assert term_a.physical_truth is None

    # NEGATIVE (layer 2): a non-sentinel no-truth result at the deadline -> TIMEOUT.
    run_n = "gdr_b61_deadline_neg"
    store_n = recovery_store_factory(f"sqlite:///{(tmp_path / (run_n + '.sqlite3')).as_posix()}")
    claim_n = _b61_claimed_running_recovery(store_n, run_n, budget_ms=budget_ms)
    cur_n = store_n.get_status(run_id=run_n)
    term_n = store_n.complete_recovery(
        dispatch_id=claim_n.dispatch_id, claim_token=claim_n.claim_token,
        expected_progress_seq=cur_n.progress_seq,
        completed_at=_B61_NOW + timedelta(seconds=63), active_elapsed_ms=budget_ms,
        result=RecoveryWorkerResult(
            outcome=RecoveryTerminalOutcome.PARTIAL,
            reason_code="global_discovery_recovery_rolled_back",
            retryable=False, counts=cur_n.counts, physical_truth=None,
        ),
    )
    assert term_n.state is RecoveryRunState.TIMEOUT

    # WORKER layer (layer 1 -> chains into layer 2): worker._complete preserves it.
    run_w = "gdr_b61_deadline_worker"
    store_w = recovery_store_factory(f"sqlite:///{(tmp_path / (run_w + '.sqlite3')).as_posix()}")
    claim_w = _b61_claimed_running_recovery(store_w, run_w, budget_ms=budget_ms)
    cur_w = store_w.get_status(run_id=run_w)
    worker = CommunityRecoveryWorker(
        store=store_w, native_operation=lambda **_k: None,
        heartbeat_interval_ms=5_000,
        wall_clock=lambda: _B61_NOW + timedelta(seconds=63),
        monotonic_clock=lambda: 2_000.0,
    )
    try:
        worker._complete(
            run_id=run_w, attempt_id=claim_w.attempt_id, epoch=claim_w.epoch,
            started_monotonic=1_000.0, baseline_elapsed_ms=0,
            result=_b61_pending_sentinel(cur_w.counts),
            dispatch_id=claim_w.dispatch_id, claim_token=claim_w.claim_token,
            wall_started_at=_B61_NOW + timedelta(seconds=3),
            deadline_at_monotonic=1_000.0,  # already past -> deadline_reached
        )
    finally:
        worker.close(timeout_seconds=3.0)
    final_w = store_w.get_status(run_id=run_w)
    assert final_w.state is RecoveryRunState.PARTIAL
    assert final_w.reason_code == "recovery_physical_reconciliation_pending"
    assert final_w.physical_truth is None


# --- B7.3: seed reconciliation fails closed on a non-exact journal kind --------


def test_r6_b73_seed_reconcile_fails_closed_on_foreign_kind(tmp_path):
    """B7.3: the seed reconciliation reader must fail closed unless the journal
    carries EXACTLY the seed-rebuild kind.  An arbitrary/foreign non-adoption kind
    must NOT enter the seed reconciliation path."""

    from okto_pulse.community.adapters.global_discovery_recovery import (
        CommunityGlobalDiscoveryRecoveryError,
        _write_journal_with_directory_fsync,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"partial-primary")

    run_id = "gdr_b73_foreign_kind"
    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    boards = (_seed(),)
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=lambda path: _CandidateRuntime(path, {}),  # type: ignore[arg-type]
    )
    quarantine_dir = (
        live.parent / "quarantine" / "global-discovery" / attempt_id
    )
    quarantine_dir.mkdir(parents=True)
    # A journal with a FOREIGN (non-adoption, non-seed_rebuild) kind but valid
    # identity/hash — it must be rejected by the discriminant, not silently treated
    # as a seed rebuild.
    _write_journal_with_directory_fsync(
        quarantine_dir / "recovery_journal.json",
        {
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "kind": "totally_foreign_kind",
            "phase": "pointer_switched",
        },
    )
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc:
        adapter.reconcile_attempt_terminal_truth(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256="0" * 64,
            boards=boards,
            fence_check=lambda: None,
        )
    assert "global_discovery_recovery_journal_kind_invalid" in str(exc.value)


# --- B6: writer-fence loss after pointer-cross -> PARTIAL, then N+1 reconciles -


def test_r6_worker_chain_writer_fence_lost_partial_then_epoch2_reconciles(
    tmp_path, prepared_recovery_admitter
):
    """B6 (Option A): a REAL writer-fence loss AFTER the pointer crosses
    terminalizes epoch N as PARTIAL / recovery_physical_reconciliation_pending
    (never a fabricated FAILED/native_operation_failed without truth), and a
    distinct epoch N+1 owner reconciles the predecessor journal/pointer BEFORE any
    physical mutation (reconcile-before-mutate), finishes the already-crossed truth
    idempotently and becomes SUCCESS with the exact supersedes chain.  The
    reconciliation contract is exercised twice with no duplicate physical mutation
    or epoch fabrication."""

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_r6fencelost"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    db_path = tmp_path / f"{run_id}.sqlite3"
    boards = (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )

    attempt1 = recovery_attempt_id(run_id, 1)
    attempt2 = recovery_attempt_id(run_id, 2)

    # B8.1: resolve the ACTUAL filesystem artifact reference for the epoch-1
    # durable inputs and capture its RAW bytes + SHA-256 before resume, so a
    # delete/recreate or whitespace/order-changed rewrite by the epoch-2 handoff
    # would fail (decoded-JSON equivalence is NOT sufficient).
    _epoch1_inputs_key = GlobalDiscoveryRecoveryWorkerInputStore._key(run_id, 1)
    _epoch2_inputs_key = GlobalDiscoveryRecoveryWorkerInputStore._key(run_id, 2)
    epoch1_artifact_path = Path(artifact_store.reference(_epoch1_inputs_key))
    epoch2_artifact_path = Path(artifact_store.reference(_epoch2_inputs_key))
    epoch1_raw_bytes_before = epoch1_artifact_path.read_bytes()
    epoch1_raw_sha_before = _sha_file(epoch1_artifact_path)
    # B8.1 (R8): physical file identity via CAPABILITY GUARD — when the
    # filesystem exposes a trusted identifier (st_ino != 0), bind st_dev+st_ino
    # so an identical-byte delete/recreate fails; otherwise the path/key + RAW
    # bytes + SHA oracles below remain the binding.  No unconditional claim on
    # non-portable timestamp semantics.
    _e1_stat = epoch1_artifact_path.stat()
    epoch1_identity_before = (
        (_e1_stat.st_dev, _e1_stat.st_ino) if _e1_stat.st_ino != 0 else None
    )
    # The epoch-2 durable artifact resolves to a DISTINCT path/key (none yet).
    assert epoch2_artifact_path != epoch1_artifact_path
    assert not epoch2_artifact_path.exists()

    # --- Runtime #1: epoch 1 crosses the pointer, then LOSES the writer fence. ---
    recording_port1 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    real1 = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    spy1 = _SpyRecovery(real1)
    invalidated = {"done": False}

    def fence_wrap(original):
        def wrapped():
            # As soon as the active pointer has CROSSED, invalidate the REAL
            # external writer ownership exactly once, so the very next fence step
            # (lease.renew inside physical_fence_check) raises a genuine
            # GlobalDiscoveryWriterFenceLost mid-cutover (cutover not yet returned).
            if not invalidated["done"] and read_active_generation(live) is not None:
                invalidated["done"] = True
                token = _acquisition_for_owner(
                    recording_port1, f"{run_id}:{attempt1}"
                )[1].owner_token
                recording_port1.release_single_writer_sync(
                    board_id=GLOBAL_DISCOVERY_WRITER_SCOPE,
                    artifact_id=GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
                    owner_token=token,
                )
            return original()

        return wrapped

    spy1.fence_wrap = fence_wrap

    started_at = datetime.now(timezone.utc)
    with _r6_env(recording_port1, tmp_path / "kgbase"):
        bundle1, gr1 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy1,
            heartbeat_interval_ms=5_000,
        )
        try:
            command = _command(run_id, started_at=started_at)
            _admit_and_start(bundle1, prepared_recovery_admitter, command)
            epoch1_terminal = _wait_until(
                bundle1.control,
                run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.PARTIAL,
                timeout_seconds=8.0,
            )
        finally:
            # B8: stop the epoch-1 worker but keep its store engine ALIVE so the
            # epoch-1 fence authority can be invoked post-resume (disposed at end).
            bundle1.worker.close(timeout_seconds=3.0)

    # The writer fence was really lost, and epoch 1 had begun the physical op.
    assert invalidated["done"] is True
    assert "recover_and_cutover" in spy1.calls
    # Epoch 1 is EXACTLY the Option-A PARTIAL record; never a fabricated FAILED.
    assert epoch1_terminal.state is RecoveryRunState.PARTIAL
    assert epoch1_terminal.terminal_outcome is RecoveryTerminalOutcome.PARTIAL
    assert epoch1_terminal.reason_code == "recovery_physical_reconciliation_pending"
    assert epoch1_terminal.retryable is False
    assert epoch1_terminal.physical_truth is None
    assert epoch1_terminal.epoch == 1
    # The pointer DID cross (physical truth already on disk) ...
    crossed = read_active_generation(live)
    assert crossed is not None
    # ... but epoch 1 did NOT complete the adoption (marker not cleared) and
    # performed NO post-loss physical mutation (active bytes byte-stable on reread).
    assert bootstrap_marker_present(live) is True
    active_sha_at_loss = _sha_file(crossed.graph_path)
    assert _sha_file(read_active_generation(live).graph_path) == active_sha_at_loss

    # --- Runtime #2: resume -> epoch 2 reconciles predecessor BEFORE mutating. ---
    recording_port2 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    real2 = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    spy2 = _SpyRecovery(real2)
    with _r6_env(recording_port2, tmp_path / "kgbase"):
        bundle2, gr2 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy2,
            heartbeat_interval_ms=5_000,
        )
        try:
            bundle2.control.resume(
                run_id=run_id,
                expected_epoch=1,
                # Derive from the epoch-1 terminal (real wall-clock) so the resume
                # never moves backwards regardless of epoch-1 duration.
                requested_at=epoch1_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6",
                reason="B6 resume to reconcile predecessor",
            )
            epoch2_terminal = _wait_until(
                bundle2.control,
                run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.SUCCESS,
                timeout_seconds=8.0,
            )
        finally:
            bundle2.close()

    # Reconcile-before-mutate: epoch 2 reconciled+completed the BOUND predecessor
    # (writing its OWN journal) and did NOT run a fresh recover_and_cutover.
    assert "reconcile_predecessor_and_complete" in spy2.calls
    assert "recover_and_cutover" not in spy2.calls
    # B6.2 EXACT call order: the bound predecessor is reconciled BEFORE any
    # retention/pruning (reconcile_attempt_artifacts) or other physical mutation.
    assert "reconcile_artifacts" in spy2.calls
    assert spy2.calls.index(
        "reconcile_predecessor_and_complete"
    ) < spy2.calls.index("reconcile_artifacts")
    assert epoch2_terminal.state is RecoveryRunState.SUCCESS
    assert epoch2_terminal.epoch == 2
    assert epoch2_terminal.attempt_id == attempt2
    assert epoch2_terminal.supersedes_epoch == 1
    assert epoch2_terminal.physical_truth is not None
    assert epoch2_terminal.physical_truth.attempt_id == attempt2

    # B6.5: epoch 2 wrote its OWN completed-first reconciliation journal, bound to
    # the EXACT predecessor by attempt id + SHA-256 of the RAW predecessor journal
    # bytes; the SUCCESS physical truth references THIS journal, never the
    # predecessor's, and the predecessor result is never relabeled.
    from okto_pulse.community.adapters.global_discovery_recovery import _snapshot

    epoch1_journal_path = (
        live.parent / "quarantine" / "global-discovery"
        / attempt1 / "recovery_journal.json"
    )
    epoch2_journal_path = (
        live.parent / "quarantine" / "global-discovery"
        / attempt2 / "recovery_journal.json"
    )
    predecessor_journal_sha = _sha_file(epoch1_journal_path)
    own_journal = json.loads(epoch2_journal_path.read_text(encoding="utf-8"))
    assert own_journal["kind"] == "reconcile_predecessor_cutover"
    assert own_journal["phase"] == "completed"
    assert own_journal["outcome"] == "completed"
    assert own_journal["attempt_id"] == attempt2
    assert own_journal["predecessor_epoch"] == 1
    assert own_journal["predecessor_attempt_id"] == attempt1
    assert own_journal["predecessor_journal_sha256"] == predecessor_journal_sha
    assert own_journal["reconciled_outcome"] == "completed"
    assert own_journal["rollback_performed"] is False
    _active_now = read_active_generation(live)
    assert _active_now is not None
    assert own_journal["generation_id"] == _active_now.generation_id
    assert own_journal["generation_manifest_sha256"] == _active_now.manifest_sha256
    assert own_journal["candidate_sha256"] == _snapshot(_active_now.graph_path).sha256
    # The SUCCESS physical truth's evidence is epoch 2's OWN journal (current owner).
    assert (
        epoch2_terminal.physical_truth.evidence_ref
        == own_journal["quarantine_ref"]
        == f"community-global-discovery-quarantine:{attempt2}"
    )

    # The epoch chain is exact and old/new authorities are distinct; epoch 1 stays
    # the historically correct PARTIAL record (not overwritten to success).
    read_engine = _make_engine(db_path)
    try:
        row1 = _read_attempt_row(read_engine, run_id, 1)
        row2 = _read_attempt_row(read_engine, run_id, 2)
        row3 = _read_attempt_row(read_engine, run_id, 3)
    finally:
        read_engine.dispose()
    assert row1 is not None and row2 is not None
    assert row1["attempt_id"] == attempt1
    assert row2["attempt_id"] == attempt2
    assert row1["supersedes_epoch"] is None
    assert row1["superseded_by_epoch"] == 2
    assert row1["state"] == RecoveryRunState.PARTIAL.value
    assert row2["supersedes_epoch"] == 1
    assert row2["superseded_by_epoch"] is None
    assert row2["state"] == RecoveryRunState.SUCCESS.value
    assert row3 is None  # no epoch fabrication
    # LITERAL closure of the chain: the TOTAL persisted epoch set is exactly
    # {1, 2} - not merely the absence of row 3.
    def _all_epochs():
        engine_all = _make_engine(db_path)
        try:
            with engine_all.connect() as conn:
                rows = conn.execute(
                    select(GlobalDiscoveryRecoveryAttempt.epoch).where(
                        GlobalDiscoveryRecoveryAttempt.run_id == run_id
                    )
                ).all()
        finally:
            engine_all.dispose()
        return {int(r[0]) for r in rows}

    assert _all_epochs() == {1, 2}
    owner1 = _acquisition_for_owner(
        recording_port1, f"{run_id}:{attempt1}"
    )[1].owner_token
    owner2 = _acquisition_for_owner(
        recording_port2, f"{run_id}:{attempt2}"
    )[1].owner_token
    assert owner1 != owner2
    # Distinct attempt identities across the two epochs.
    assert epoch1_terminal.attempt_id == attempt1
    assert attempt1 != attempt2

    # B8.2: per-epoch RECOVERY dispatch rows read by EXACT run+epoch+attempt; the
    # dispatch ids, claim tokens and worker ids are distinct and correctly bound.
    read_engine2 = _make_engine(db_path)
    try:
        d1 = _read_recovery_dispatch_at(read_engine2, run_id, 1, attempt1)
        d2 = _read_recovery_dispatch_at(read_engine2, run_id, 2, attempt2)
    finally:
        read_engine2.dispose()
    assert d1 is not None and d2 is not None
    assert d1["epoch"] == 1 and d1["attempt_id"] == attempt1
    assert d2["epoch"] == 2 and d2["attempt_id"] == attempt2
    assert d1["dispatch_id"] != d2["dispatch_id"]
    assert d1["claim_token"] and d2["claim_token"]
    assert d1["claim_token"] != d2["claim_token"]
    assert d1["worker_id"] != d2["worker_id"]

    # B8.2: distinct writer lease OBJECTS + owner tokens + composite physical-fence
    # closures across the two epochs (no lease/fence rebinding).
    assert spy1.fence_lease is not None and spy2.fence_lease is not None
    assert spy1.fence_lease is not spy2.fence_lease
    assert spy1.fence_lease.owner_token == owner1
    assert spy2.fence_lease.owner_token == owner2
    assert spy1.fence_lease.owner_token != spy2.fence_lease.owner_token
    # B8.2 (R8) closure relations, LITERAL: within each epoch reconcile/physical
    # use that epoch's SAME composite closure object; across epochs the closures
    # are distinct objects.
    assert spy1.fences["reconcile"] is spy1.fences["recover"]
    assert spy2.fences["predecessor"] is spy2.fences["reconcile"]
    assert spy1.fences["recover"] is not spy2.fences["predecessor"]
    assert spy1.fences["recover"] is not spy2.fences["reconcile"]
    # Each composite closure is bound to its epoch's EXACT dispatch claim
    # (dispatch id + claim token + worker id) and EXACT lease object/owner token
    # — recovered from the real closure cells, no monkeypatching.
    claim1 = _claim_from_fence(spy1.fences["recover"])
    claim2 = _claim_from_fence(spy2.fences["predecessor"])
    assert claim1 is not None and claim2 is not None
    assert claim1 is not claim2
    # COMPLETE exact claim identity per epoch, cross-checked against the rows.
    assert claim1.run_id == run_id and claim2.run_id == run_id
    assert claim1.attempt_id == attempt1
    assert claim2.attempt_id == attempt2
    assert claim1.epoch == 1 and claim2.epoch == 2
    assert claim1.stage is RecoveryDispatchStage.RECOVERY
    assert claim2.stage is RecoveryDispatchStage.RECOVERY
    assert claim1.state is RecoveryDispatchState.CLAIMED
    assert claim2.state is RecoveryDispatchState.CLAIMED
    assert claim1.dispatch_id == d1["dispatch_id"]
    assert claim1.claim_token == d1["claim_token"]
    assert claim1.worker_id == d1["worker_id"]
    assert claim2.dispatch_id == d2["dispatch_id"]
    assert claim2.claim_token == d2["claim_token"]
    assert claim2.worker_id == d2["worker_id"]
    assert claim1.dispatch_id != claim2.dispatch_id
    assert claim1.claim_token != claim2.claim_token
    # EXACT physical leases: the closure lease IS the epoch's ACTIVE guard lease
    # (identity, not just an equal token) — a lease decoy with a copied token
    # dies here.
    lease1 = _lease_from_fence(spy1.fences["recover"])
    lease2 = _lease_from_fence(spy2.fences["predecessor"])
    assert lease1 is spy1.fence_lease
    assert lease2 is spy2.fence_lease
    assert lease1 is spy1.active_lease
    assert lease2 is spy2.active_lease
    assert lease1 is not lease2

    # B8.1: the epoch-1 durable inputs artifact is preserved byte-for-byte (raw
    # bytes + SHA + same path) after the epoch-2 handoff created its OWN artifact
    # at a DISTINCT path/key.
    assert epoch1_artifact_path.read_bytes() == epoch1_raw_bytes_before
    assert _sha_file(epoch1_artifact_path) == epoch1_raw_sha_before
    if epoch1_identity_before is not None:
        _e1_stat_after = epoch1_artifact_path.stat()
        assert (
            _e1_stat_after.st_dev, _e1_stat_after.st_ino,
        ) == epoch1_identity_before  # same trusted inode — not a recreate
    assert epoch2_artifact_path.exists()
    assert epoch2_artifact_path != epoch1_artifact_path
    assert epoch2_artifact_path.read_bytes() != epoch1_raw_bytes_before

    # B8.3: the CAPTURED old composite physical fence (lease + dispatch-claim +
    # worker-epoch binding), invoked AFTER epoch 2 owns the run, rejects with
    # EXACTLY stale_epoch and performs no physical mutation.  (bundle1's store
    # engine was kept alive for this; the check TRAVERSES the adapter's real
    # _assert_fenced API layer.)
    active_before_fence = read_active_generation(live)
    assert active_before_fence is not None
    fence_sha_before = _sha_file(active_before_fence.graph_path)
    # B8.3 (R8): capture EVERY relevant raw byte surface around the rejected old
    # authority — pointer file, generation manifest, active graph/WAL snapshot
    # (full raw map), both epochs' durable input artifacts and both journals.
    _pointer_path_b8 = live.parent / "active_generation.json"
    _manifest_path_b8 = (
        live.parent / "discovery.generations"
        / active_before_fence.generation_id / "generation_manifest.json"
    )
    import okto_pulse.community.adapters.global_discovery_recovery as gdr_mod

    _marker_path_b8 = live.parent / "discovery_bootstrap_incomplete.json"
    _artifacts_root_b8 = tmp_path / "artifacts"

    def _artifacts_tree():
        return {
            path.relative_to(_artifacts_root_b8).as_posix(): (
                path.read_bytes() if path.is_file() else "<dir>"
            )
            for path in sorted(_artifacts_root_b8.rglob("*"))
        }

    def _sql_state():
        # (R2 fix #1) TOTAL SQL snapshot for the run: EVERY attempt row and
        # EVERY dispatch row, deterministically ordered — an inserted extra
        # dispatch/attempt for this run can no longer hide.
        engine_sql = _make_engine(db_path)
        try:
            with engine_sql.connect() as conn:
                attempt_rows = [
                    dict(row)
                    for row in conn.execute(
                        select(GlobalDiscoveryRecoveryAttempt)
                        .where(
                            GlobalDiscoveryRecoveryAttempt.run_id == run_id
                        )
                        .order_by(GlobalDiscoveryRecoveryAttempt.epoch)
                    ).mappings()
                ]
                dispatch_rows = [
                    dict(row)
                    for row in conn.execute(
                        select(GlobalDiscoveryRecoveryDispatch)
                        .where(
                            GlobalDiscoveryRecoveryDispatch.run_id == run_id
                        )
                        .order_by(GlobalDiscoveryRecoveryDispatch.dispatch_id)
                    ).mappings()
                ]
        finally:
            engine_sql.dispose()
        return (attempt_rows, dispatch_rows)

    def _b8_raw_state():
        # (R2 fix #2) B8 snapshot of the physical surface: EVERY entry under
        # the generations tree — tmp files INCLUDED, directories marked — plus
        # the pointer file and the legacy primary/WAL pair.  A temp generation
        # or an empty new directory can no longer hide.
        root = live.parent
        captured: dict[str, object] = {}
        pointer = root / "active_generation.json"
        if pointer.exists():
            captured["active_generation.json"] = pointer.read_bytes()
        generations = root / "discovery.generations"
        if generations.exists():
            for path in sorted(generations.rglob("*")):
                rel = path.relative_to(root).as_posix()
                captured[rel] = (
                    path.read_bytes() if path.is_file() else "<dir>"
                )
        for legacy_file in (live, live.with_name(live.name + ".wal")):
            if legacy_file.exists():
                captured[legacy_file.name] = legacy_file.read_bytes()
        return captured

    def _b8_snapshot():
        # (R2 fix #3) ONE composite snapshot — marker included — so no surface
        # can be omitted from any later comparison.
        return {
            "manifest": _manifest_path_b8.read_bytes(),
            "raw": _b8_raw_state(),
            "marker": (
                _marker_path_b8.read_bytes()
                if _marker_path_b8.exists()
                else None
            ),
            "quarantine": _quarantine_tree(live),
            "artifacts": _artifacts_tree(),
            "sql": _sql_state(),
            "e1_inputs": epoch1_artifact_path.read_bytes(),
            "e2_inputs": epoch2_artifact_path.read_bytes(),
            "e1_journal": epoch1_journal_path.read_bytes(),
            "e2_journal": epoch2_journal_path.read_bytes(),
        }

    _pre_fence = _b8_snapshot()
    # B8.3 via the REAL API layer: the captured epoch-1 composite fence is
    # traversed through gdr_mod._assert_fenced - a mutant that neutralizes
    # _assert_fenced raises nothing and dies on pytest.raises.
    from okto_pulse.community.adapters.global_discovery_recovery import (
        CommunityGlobalDiscoveryRecoveryFenceError,
    )

    with pytest.raises(CommunityGlobalDiscoveryRecoveryFenceError) as stale_exc:
        gdr_mod._assert_fenced(spy1.fences["recover"])
    # The adapter API wraps the fence signal; the ORIGINAL must be the exact
    # worker-typed stale_epoch (no relabeling, no neutralized guard).
    assert isinstance(stale_exc.value.original, RecoveryWorkerFenceError)
    assert stale_exc.value.original.code == "stale_epoch"
    active_after_fence = read_active_generation(live)
    assert active_after_fence is not None
    assert active_after_fence.generation_id == active_before_fence.generation_id
    assert _sha_file(active_after_fence.graph_path) == fence_sha_before
    # ONE composite comparison: manifest, full generations tree (tmp + dirs),
    # marker, complete quarantine/artifacts trees, TOTAL SQL rows, inputs and
    # journals of both epochs — all byte-identical across the rejection.
    assert _b8_snapshot() == _pre_fence
    bundle1.engine.dispose()

    # Idempotency: reconciling the SUCCESSOR's OWN completed journal again (the
    # supported terminal-truth primitive, fresh valid fence) validates it
    # fail-closed and returns the same completed truth with NO duplicate physical
    # mutation, NO journal rewrite, and NO epoch fabrication.
    active_before = read_active_generation(live)
    assert active_before is not None
    sha_before = _sha_file(active_before.graph_path)
    own_journal_bytes_before = epoch2_journal_path.read_bytes()
    with _r6_env(_RecordingWriteLockPort(CommunityLocalWriteLockPort()), tmp_path / "kgbase"):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:idem",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            again = real2.reconcile_attempt_terminal_truth(
                run_id=run_id,
                epoch=2,
                attempt_id=attempt2,
                expected_live_sha256=live_sha,
                boards=boards,
                fence_check=lease.assert_fenced,
            )
    assert again is not None
    assert again.outcome == "completed"
    active_after = read_active_generation(live)
    assert active_after is not None
    assert active_after.generation_id == active_before.generation_id
    assert _sha_file(active_after.graph_path) == sha_before
    # The successor's own completed-first journal was NOT rewritten.
    assert epoch2_journal_path.read_bytes() == own_journal_bytes_before
    # Byte-idempotent across the COMPLETE composite snapshot (marker included)
    # + SQL requery: the 2nd reconciliation changed nothing and fabricated no
    # epoch anywhere.
    assert _b8_snapshot() == _pre_fence
    assert _all_epochs() == {1, 2}


# --- B6.4/B6.6: pending-ancestry walk heals after a mid-chain reconciler crash -


def _invalidate_after_cross_wrap(recording_port, run_id, attempt, live, flag):
    """A fence wrapper that, once the active pointer has crossed, invalidates the
    REAL external writer ownership for ``attempt`` exactly once — producing a
    genuine GlobalDiscoveryWriterFenceLost at the next fence step."""

    def fence_wrap(original):
        def wrapped():
            if not flag["done"] and read_active_generation(live) is not None:
                flag["done"] = True
                token = _acquisition_for_owner(
                    recording_port, f"{run_id}:{attempt}"
                )[1].owner_token
                recording_port.release_single_writer_sync(
                    board_id=GLOBAL_DISCOVERY_WRITER_SCOPE,
                    artifact_id=GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
                    owner_token=token,
                )
            return original()

        return wrapped

    return fence_wrap


def test_r6_worker_chain_pending_ancestry_walk_heals_after_midchain_crash(
    tmp_path, prepared_recovery_admitter
):
    """B6.4/B6.6: epoch 1 crosses the pointer then loses the fence (PARTIAL);
    epoch 2 (N+1) resumes but LOSES its fence during the bridge BEFORE writing its
    own completed journal (PARTIAL, no own journal, zero stale post-loss mutation);
    epoch 3 (N+2) resumes, walks the persisted pending ancestry (epoch2 -> epoch1),
    finds the exact unresolved SOURCE (epoch 1), heals it and becomes SUCCESS bound
    to epoch 1 by raw journal SHA.  Active snapshot/pointer/manifest and the source
    journal stay byte-stable throughout; the epoch chain is exact with no
    fabrication and no journal rewrite."""

    from okto_pulse.community.adapters.global_discovery_recovery import _snapshot

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_r6ancestry"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    db_path = tmp_path / f"{run_id}.sqlite3"
    boards = (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )

    attempt1 = recovery_attempt_id(run_id, 1)
    attempt2 = recovery_attempt_id(run_id, 2)
    attempt3 = recovery_attempt_id(run_id, 3)
    q_root = live.parent / "quarantine" / "global-discovery"
    epoch1_journal_path = q_root / attempt1 / "recovery_journal.json"
    epoch2_journal_path = q_root / attempt2 / "recovery_journal.json"
    epoch3_journal_path = q_root / attempt3 / "recovery_journal.json"

    def _build(spy):
        return _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy,
            heartbeat_interval_ms=5_000,
        )

    def _real():
        return CommunityGlobalDiscoveryRecovery(
            global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
            graph_path_provider=lambda: live,
            runtime_factory=factory,  # type: ignore[arg-type]
        )

    # --- Epoch 1: cross the pointer, then LOSE the writer fence -> PARTIAL. ---
    port1 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spy1 = _SpyRecovery(_real())
    spy1.fence_wrap = _invalidate_after_cross_wrap(
        port1, run_id, attempt1, live, {"done": False}
    )
    started_at = datetime.now(timezone.utc)
    with _r6_env(port1, tmp_path / "kgbase"):
        bundle1, _gr1 = _build(spy1)
        try:
            _admit_and_start(
                bundle1, prepared_recovery_admitter,
                _command(run_id, started_at=started_at),
            )
            epoch1_terminal = _wait_until(
                bundle1.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.PARTIAL,
                timeout_seconds=8.0,
            )
        finally:
            bundle1.close()
    assert epoch1_terminal.state is RecoveryRunState.PARTIAL
    assert epoch1_terminal.reason_code == "recovery_physical_reconciliation_pending"
    assert epoch1_terminal.physical_truth is None

    # Capture the crossed physical truth (must stay stable through the whole heal).
    crossed = read_active_generation(live)
    assert crossed is not None
    crossed_gen = crossed.generation_id
    crossed_manifest = crossed.manifest_sha256
    crossed_snapshot_sha = _snapshot(crossed.graph_path).sha256
    epoch1_journal_bytes = epoch1_journal_path.read_bytes()

    # --- Epoch 2 (N+1): resume, but LOSE the fence during the bridge BEFORE its
    #     own completed journal exists -> PARTIAL, no own journal. ---
    port2 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spy2 = _SpyRecovery(_real())
    # The pointer is ALREADY active, so this invalidates on the first bridge fence.
    spy2.fence_wrap = _invalidate_after_cross_wrap(
        port2, run_id, attempt2, live, {"done": False}
    )
    with _r6_env(port2, tmp_path / "kgbase"):
        bundle2, _gr2 = _build(spy2)
        try:
            bundle2.control.resume(
                run_id=run_id, expected_epoch=1,
                requested_at=epoch1_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6", reason="B6.4 resume epoch 2",
            )
            epoch2_terminal = _wait_until(
                bundle2.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.PARTIAL,
                timeout_seconds=8.0,
            )
        finally:
            bundle2.close()
    # Epoch 2 attempted the bridge but crashed BEFORE its own journal.
    assert "reconcile_predecessor_and_complete" in spy2.calls
    assert epoch2_terminal.state is RecoveryRunState.PARTIAL
    assert epoch2_terminal.reason_code == "recovery_physical_reconciliation_pending"
    assert epoch2_terminal.physical_truth is None
    assert not epoch2_journal_path.exists()  # no completed-first own journal
    # Zero stale post-loss mutation: the crossed state is byte-identical.
    active2 = read_active_generation(live)
    assert active2 is not None
    assert active2.generation_id == crossed_gen
    assert active2.manifest_sha256 == crossed_manifest
    assert _snapshot(active2.graph_path).sha256 == crossed_snapshot_sha
    assert epoch1_journal_path.read_bytes() == epoch1_journal_bytes

    # --- Epoch 3 (N+2): resume, walk the ancestry to the SOURCE and heal. ---
    port3 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spy3 = _SpyRecovery(_real())
    real3 = spy3._real
    with _r6_env(port3, tmp_path / "kgbase"):
        bundle3, _gr3 = _build(spy3)
        try:
            bundle3.control.resume(
                run_id=run_id, expected_epoch=2,
                requested_at=epoch2_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6", reason="B6.4 resume epoch 3",
            )
            epoch3_terminal = _wait_until(
                bundle3.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.SUCCESS,
                timeout_seconds=8.0,
            )
        finally:
            bundle3.close()
    assert epoch3_terminal.state is RecoveryRunState.SUCCESS
    assert epoch3_terminal.epoch == 3
    assert epoch3_terminal.attempt_id == attempt3
    assert epoch3_terminal.supersedes_epoch == 2
    assert epoch3_terminal.physical_truth is not None
    assert epoch3_terminal.physical_truth.attempt_id == attempt3
    assert "reconcile_predecessor_and_complete" in spy3.calls
    assert "recover_and_cutover" not in spy3.calls

    # Epoch 3's OWN journal binds the exact SOURCE (epoch 1), not the crashed N+1.
    own_journal = json.loads(epoch3_journal_path.read_text(encoding="utf-8"))
    assert own_journal["kind"] == "reconcile_predecessor_cutover"
    assert own_journal["predecessor_epoch"] == 1
    assert own_journal["predecessor_attempt_id"] == attempt1
    assert own_journal["predecessor_journal_sha256"] == _sha_file(epoch1_journal_path)
    assert own_journal["reconciled_outcome"] == "completed"
    assert (
        epoch3_terminal.physical_truth.evidence_ref
        == own_journal["quarantine_ref"]
        == f"community-global-discovery-quarantine:{attempt3}"
    )

    # Exact epoch chain, no fabrication, and the source journal was never rewritten.
    read_engine = _make_engine(db_path)
    try:
        row1 = _read_attempt_row(read_engine, run_id, 1)
        row2 = _read_attempt_row(read_engine, run_id, 2)
        row3 = _read_attempt_row(read_engine, run_id, 3)
        row4 = _read_attempt_row(read_engine, run_id, 4)
    finally:
        read_engine.dispose()
    assert row1["superseded_by_epoch"] == 2
    assert row1["state"] == RecoveryRunState.PARTIAL.value
    assert row2["supersedes_epoch"] == 1
    assert row2["superseded_by_epoch"] == 3
    assert row2["state"] == RecoveryRunState.PARTIAL.value
    assert row3["supersedes_epoch"] == 2
    assert row3["state"] == RecoveryRunState.SUCCESS.value
    assert row4 is None  # no epoch fabrication
    # The source journal was completed EXACTLY once during the heal (pointer_switched
    # -> completed); epoch 3 bound that exact completed-source SHA.
    assert own_journal["predecessor_journal_sha256"] == _sha_file(epoch1_journal_path)
    active3 = read_active_generation(live)
    assert active3 is not None
    assert active3.generation_id == crossed_gen
    assert active3.manifest_sha256 == crossed_manifest
    assert _snapshot(active3.graph_path).sha256 == crossed_snapshot_sha

    # Idempotency: reconciling epoch 3's OWN journal again returns the same
    # completed truth with NO mutation and NO journal rewrite.
    own_bytes_before = epoch3_journal_path.read_bytes()
    with _r6_env(_RecordingWriteLockPort(CommunityLocalWriteLockPort()), tmp_path / "kgbase"):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:idem",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            again = real3.reconcile_attempt_terminal_truth(
                run_id=run_id, epoch=3, attempt_id=attempt3,
                expected_live_sha256=live_sha, boards=boards,
                fence_check=lease.assert_fenced,
            )
    assert again is not None
    assert again.outcome == "completed"
    assert epoch3_journal_path.read_bytes() == own_bytes_before
    assert _snapshot(read_active_generation(live).graph_path).sha256 == crossed_snapshot_sha


# --- B8/R8 B6.1: fail-closed pending-ancestry walk (stop at valid boundary) ----


def test_r6_b8r1_cancelled_predecessor_then_pending_source_heals(
    tmp_path, prepared_recovery_admitter
):
    """R8 B6#1 positive: epoch 1 CANCELLED, epoch 2 crosses+loses fence (exact
    pending), epoch 3 resumes.  The resolver must STOP at the valid non-pending
    epoch-1 boundary and bind ancestry=[epoch 2] (the unresolved source), then heal
    — never return None and start fresh physical recovery."""

    from okto_pulse.community.adapters.global_discovery_recovery import _snapshot

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)
    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_b8r1heal"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    db_path = tmp_path / f"{run_id}.sqlite3"
    boards = (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )
    attempt2 = recovery_attempt_id(run_id, 2)
    attempt3 = recovery_attempt_id(run_id, 3)

    def _real():
        return CommunityGlobalDiscoveryRecovery(
            global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
            graph_path_provider=lambda: live,
            runtime_factory=factory,  # type: ignore[arg-type]
        )

    def _build(spy):
        return _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy,
            heartbeat_interval_ms=5_000,
        )

    # Epoch 1: cancel in-flight (no pointer cross) -> CANCELLED.
    spy1 = _SpyRecovery(_real())
    spy1.reconcile_gate = threading.Event()
    started_at = datetime.now(timezone.utc)
    with _r6_env(_RecordingWriteLockPort(CommunityLocalWriteLockPort()), tmp_path / "kgbase"):
        bundle1, _gr1 = _build(spy1)
        try:
            _admit_and_start(
                bundle1, prepared_recovery_admitter,
                _command(run_id, started_at=started_at),
            )
            assert spy1.reconcile_entered.wait(timeout=5.0)
            bundle1.control.cancel(
                run_id=run_id, expected_epoch=1,
                requested_at=started_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6", reason="B6.1 cancel epoch 1",
            )
            spy1.reconcile_gate.set()
            epoch1_terminal = _wait_until(
                bundle1.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.CANCELLED,
            )
            assert "recover_and_cutover" not in spy1.calls
        finally:
            bundle1.close()

    # Epoch 2: resume from a CANCELLED predecessor -> NO plan -> fresh recovery that
    # crosses the pointer then loses the fence -> exact pending sentinel.
    port2 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spy2 = _SpyRecovery(_real())
    spy2.fence_wrap = _invalidate_after_cross_wrap(
        port2, run_id, attempt2, live, {"done": False}
    )
    with _r6_env(port2, tmp_path / "kgbase"):
        bundle2, _gr2 = _build(spy2)
        try:
            bundle2.control.resume(
                run_id=run_id, expected_epoch=1,
                requested_at=epoch1_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6", reason="B6.1 resume epoch 2",
            )
            epoch2_terminal = _wait_until(
                bundle2.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.PARTIAL,
                timeout_seconds=8.0,
            )
        finally:
            bundle2.close()
    # Epoch 2 did fresh recovery (no predecessor plan for a CANCELLED predecessor).
    assert "recover_and_cutover" in spy2.calls
    assert "reconcile_predecessor_and_complete" not in spy2.calls
    assert epoch2_terminal.reason_code == "recovery_physical_reconciliation_pending"
    crossed = read_active_generation(live)
    assert crossed is not None
    crossed_sha = _snapshot(crossed.graph_path).sha256

    # Epoch 3: resume from epoch 2 (pending) -> ancestry STOPS at the epoch-1
    # CANCELLED boundary and binds [epoch 2] -> heal, no fresh recovery.
    spy3 = _SpyRecovery(_real())
    with _r6_env(_RecordingWriteLockPort(CommunityLocalWriteLockPort()), tmp_path / "kgbase"):
        bundle3, _gr3 = _build(spy3)
        try:
            bundle3.control.resume(
                run_id=run_id, expected_epoch=2,
                requested_at=epoch2_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6", reason="B6.1 resume epoch 3",
            )
            epoch3_terminal = _wait_until(
                bundle3.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.SUCCESS,
                timeout_seconds=8.0,
            )
        finally:
            bundle3.close()
    assert epoch3_terminal.state is RecoveryRunState.SUCCESS
    assert epoch3_terminal.epoch == 3
    assert epoch3_terminal.supersedes_epoch == 2
    assert "reconcile_predecessor_and_complete" in spy3.calls
    assert "recover_and_cutover" not in spy3.calls
    own = json.loads(
        (live.parent / "quarantine" / "global-discovery" / attempt3
         / "recovery_journal.json").read_text(encoding="utf-8")
    )
    assert own["kind"] == "reconcile_predecessor_cutover"
    assert own["predecessor_epoch"] == 2  # the pending source, NOT epoch 1
    assert own["predecessor_attempt_id"] == attempt2
    read_engine = _make_engine(db_path)
    try:
        r1 = _read_attempt_row(read_engine, run_id, 1)
        r2 = _read_attempt_row(read_engine, run_id, 2)
        r3 = _read_attempt_row(read_engine, run_id, 3)
        r4 = _read_attempt_row(read_engine, run_id, 4)
    finally:
        read_engine.dispose()
    assert r1["state"] == RecoveryRunState.CANCELLED.value
    assert r1["superseded_by_epoch"] == 2
    assert r2["state"] == RecoveryRunState.PARTIAL.value
    assert r2["supersedes_epoch"] == 1 and r2["superseded_by_epoch"] == 3
    assert r3["state"] == RecoveryRunState.SUCCESS.value and r3["supersedes_epoch"] == 2
    assert r4 is None
    assert _snapshot(read_active_generation(live).graph_path).sha256 == crossed_sha


@pytest.mark.parametrize(
    "corruption,expected_code",
    [
        ("broken_link", "recovery_pending_ancestry_broken_link"),
        ("missing_row", "recovery_pending_ancestry_missing_row"),
        ("bad_ordering", "recovery_pending_ancestry_bad_ordering"),
        ("over_bound", "recovery_pending_ancestry_over_bound"),
    ],
)
def test_r6_b8r1_corrupt_ancestry_resolver_fails_closed(
    recovery_store_factory, tmp_path, corruption, expected_code
):
    """R8 B6#1 negative: a corrupt/anomalous persisted ancestry is a TYPED
    fail-closed result, never None-then-fresh-recover."""

    from types import SimpleNamespace

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / (corruption + '.sqlite3')).as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store, native_operation=lambda **_k: None, heartbeat_interval_ms=5_000
    )
    run_id = "gdr_b8r1_corrupt"
    _PENDING = "recovery_physical_reconciliation_pending"

    def _pending(epoch, superseded_by, supersedes):
        return SimpleNamespace(
            epoch=epoch,
            superseded_by_epoch=superseded_by,
            supersedes_epoch=supersedes,
            attempt_id=recovery_attempt_id(run_id, epoch),
            state=RecoveryRunState.PARTIAL,
            terminal_outcome=RecoveryTerminalOutcome.PARTIAL,
            reason_code=_PENDING,
            retryable=False,
            physical_truth=None,
        )

    try:
        if corruption == "over_bound":
            store.get_status_at_epoch = lambda *, run_id, epoch: _pending(
                epoch, epoch + 1, (epoch - 1) if epoch > 1 else None
            )
            running = SimpleNamespace(run_id=run_id, epoch=200, supersedes_epoch=199)
        else:
            if corruption == "broken_link":
                rows = {
                    2: SimpleNamespace(
                        epoch=2, superseded_by_epoch=99, supersedes_epoch=1,
                        attempt_id=recovery_attempt_id(run_id, 2),
                        state=RecoveryRunState.PARTIAL,
                        terminal_outcome=RecoveryTerminalOutcome.PARTIAL,
                        reason_code=_PENDING, retryable=False, physical_truth=None,
                    )
                }
            elif corruption == "missing_row":
                rows = {}
            else:  # bad_ordering: supersedes points to self (not strictly less)
                rows = {2: _pending(2, 3, 2)}
            store.get_status_at_epoch = lambda *, run_id, epoch: rows.get(epoch)
            running = SimpleNamespace(run_id=run_id, epoch=3, supersedes_epoch=2)
        with pytest.raises(RecoveryPendingAncestryError) as exc:
            worker._resolve_predecessor_reconcile_plan(running)
        assert exc.value.code == expected_code
    finally:
        worker.close(timeout_seconds=1.0)


def test_r6_b8r1_corrupt_chain_terminalizes_failed_with_zero_mutation(
    tmp_path, prepared_recovery_admitter
):
    """R8 B6#1 negative integration: a corrupt persisted supersedes chain
    terminalizes the resuming attempt FAILED with the typed reason and performs
    ZERO physical work (no reconcile_artifacts/recover, no pointer/graph mutation,
    no epoch fabrication)."""

    from okto_pulse.community.adapters.global_discovery_recovery import _snapshot

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)
    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_b8r1corrupt"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    db_path = tmp_path / f"{run_id}.sqlite3"
    boards = (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )
    attempt1 = recovery_attempt_id(run_id, 1)

    def _real():
        return CommunityGlobalDiscoveryRecovery(
            global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
            graph_path_provider=lambda: live,
            runtime_factory=factory,  # type: ignore[arg-type]
        )

    # Epoch 1: cross the pointer, lose the fence -> exact pending sentinel.
    port1 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spy1 = _SpyRecovery(_real())
    spy1.fence_wrap = _invalidate_after_cross_wrap(
        port1, run_id, attempt1, live, {"done": False}
    )
    started_at = datetime.now(timezone.utc)
    with _r6_env(port1, tmp_path / "kgbase"):
        bundle1, _gr1 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy1,
            heartbeat_interval_ms=5_000,
        )
        try:
            _admit_and_start(
                bundle1, prepared_recovery_admitter,
                _command(run_id, started_at=started_at),
            )
            epoch1_terminal = _wait_until(
                bundle1.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.PARTIAL,
                timeout_seconds=8.0,
            )
        finally:
            bundle1.close()
    crossed = read_active_generation(live)
    assert crossed is not None
    crossed_sha = _snapshot(crossed.graph_path).sha256
    epoch1_journal_path = (
        live.parent / "quarantine" / "global-discovery" / attempt1
        / "recovery_journal.json"
    )
    epoch1_journal_bytes = epoch1_journal_path.read_bytes()

    # CORRUPT the persisted chain: epoch 1 is the first attempt (supersedes None);
    # forcing it to supersede itself is an impossible ordering the resolver must
    # reject fail-closed when epoch 2 resumes.
    corrupt_engine = _make_engine(db_path)
    try:
        with corrupt_engine.begin() as conn:
            conn.execute(
                update(GlobalDiscoveryRecoveryAttempt)
                .where(
                    GlobalDiscoveryRecoveryAttempt.run_id == run_id,
                    GlobalDiscoveryRecoveryAttempt.epoch == 1,
                )
                .values(supersedes_epoch=1)
            )
    finally:
        corrupt_engine.dispose()

    # Epoch 2 resumes onto the corrupt chain -> typed fail-closed FAILED, ZERO
    # physical work.
    spy2 = _SpyRecovery(_real())
    with _r6_env(_RecordingWriteLockPort(CommunityLocalWriteLockPort()), tmp_path / "kgbase"):
        bundle2, gr2 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy2,
            heartbeat_interval_ms=5_000,
        )
        try:
            bundle2.control.resume(
                run_id=run_id, expected_epoch=1,
                requested_at=epoch1_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6", reason="B6.1 corrupt resume",
            )
            epoch2_terminal = _wait_until(
                bundle2.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.FAILED,
                timeout_seconds=8.0,
            )
        finally:
            bundle2.close()
    assert epoch2_terminal.state is RecoveryRunState.FAILED
    assert epoch2_terminal.reason_code == "recovery_pending_ancestry_bad_ordering"
    # ZERO physical work by the failed attempt.
    assert spy2.calls == []
    assert not gr2.successful_cutovers
    active_after = read_active_generation(live)
    assert active_after is not None
    assert active_after.generation_id == crossed.generation_id
    assert _snapshot(active_after.graph_path).sha256 == crossed_sha
    assert epoch1_journal_path.read_bytes() == epoch1_journal_bytes
    read_engine = _make_engine(db_path)
    try:
        assert _read_attempt_row(read_engine, run_id, 3) is None  # no fabrication
    finally:
        read_engine.dispose()


# --- R8 Step-1 fix: exact resolver predicate + native full-ancestry validation --


def test_r6_b8r1s2_resolver_predicate_and_metadata(recovery_store_factory, tmp_path):
    """R8 Step-1 #1: the resolver uses the EXACT B6.1 pending sentinel (retryable
    False), treats a retryable=True reserved-reason record as a non-pending
    boundary, and fails closed on corrupt running-chain metadata (missing
    supersedes for epoch>1, unavailable exact-epoch reader)."""

    from types import SimpleNamespace

    store = recovery_store_factory(
        f"sqlite:///{(tmp_path / 'predicate.sqlite3').as_posix()}"
    )
    worker = CommunityRecoveryWorker(
        store=store, native_operation=lambda **_k: None, heartbeat_interval_ms=5_000
    )
    run_id = "gdr_b8r1s2"
    _PENDING = "recovery_physical_reconciliation_pending"

    def _rec(epoch, superseded_by, supersedes, *, retryable, reason=_PENDING):
        return SimpleNamespace(
            epoch=epoch, superseded_by_epoch=superseded_by, supersedes_epoch=supersedes,
            attempt_id=recovery_attempt_id(run_id, epoch),
            state=RecoveryRunState.PARTIAL,
            terminal_outcome=RecoveryTerminalOutcome.PARTIAL,
            reason_code=reason, retryable=retryable, physical_truth=None,
        )

    try:
        # retryable=True reserved-reason immediate predecessor -> NO plan.
        store.get_status_at_epoch = lambda *, run_id, epoch: (
            _rec(1, 2, None, retryable=True) if epoch == 1 else None
        )
        running = SimpleNamespace(run_id=run_id, epoch=2, supersedes_epoch=1)
        assert worker._resolve_predecessor_reconcile_plan(running) is None

        # retryable=True reserved-reason MID-CHAIN terminates the exact pending
        # ancestry successfully at that non-pending boundary.
        rows = {2: _rec(2, 3, 1, retryable=False), 1: _rec(1, 2, None, retryable=True)}
        store.get_status_at_epoch = lambda *, run_id, epoch: rows.get(epoch)
        running = SimpleNamespace(run_id=run_id, epoch=3, supersedes_epoch=2)
        plan = worker._resolve_predecessor_reconcile_plan(running)
        assert plan is not None
        assert plan.ancestry == ((2, recovery_attempt_id(run_id, 2)),)

        # epoch>1 with missing supersedes metadata -> typed fail-closed.
        running = SimpleNamespace(run_id=run_id, epoch=2, supersedes_epoch=None)
        with pytest.raises(RecoveryPendingAncestryError) as exc:
            worker._resolve_predecessor_reconcile_plan(running)
        assert exc.value.code == "recovery_pending_ancestry_missing_supersedes"

        # unavailable exact-epoch reader -> typed fail-closed.
        store.get_status_at_epoch = None
        running = SimpleNamespace(run_id=run_id, epoch=2, supersedes_epoch=1)
        with pytest.raises(RecoveryPendingAncestryError) as exc:
            worker._resolve_predecessor_reconcile_plan(running)
        assert exc.value.code == "recovery_pending_ancestry_reader_unavailable"
    finally:
        worker.close(timeout_seconds=1.0)


@pytest.mark.parametrize("forgery", ["skips_immediate", "forged_later_entry", "duplicate"])
def test_r6_b8r1s2_native_op_forged_plan_zero_physical(tmp_path, forgery):
    """R8 Step-1 #2: the native operation rejects a forged plan (wrong immediate
    predecessor, forged later entry, duplicate epoch) BEFORE the input provider /
    lease / any physical mutation, preserving the exact typed code."""

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    shared = _coherent_adopt_state()
    spy = _SpyRecovery(
        CommunityGlobalDiscoveryRecovery(
            global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
            graph_path_provider=lambda: live,
            runtime_factory=lambda p: _CandidateRuntime(p, shared),  # type: ignore[arg-type]
        )
    )
    provider_calls = {"n": 0}

    def provider(*, run_id, epoch):
        provider_calls["n"] += 1
        return None

    native_op = CommunityGlobalDiscoveryRecoveryNativeOperation(
        recovery=spy, input_provider=provider  # type: ignore[arg-type]
    )
    run_id = "gdr_b8r1s2native"
    a1 = recovery_attempt_id(run_id, 1)
    a2 = recovery_attempt_id(run_id, 2)
    if forgery == "skips_immediate":
        ancestry = ((1, a1),)  # first entry is epoch 1, not the immediate epoch 2
    elif forgery == "forged_later_entry":
        ancestry = ((2, a2), (1, "forged-attempt-id"))
    else:  # duplicate epoch
        ancestry = ((2, a2), (2, a2))
    plan = RecoveryPredecessorReconcilePlan(
        run_id=run_id, successor_epoch=3, ancestry=ancestry
    )
    with pytest.raises(RecoveryPendingAncestryError):
        native_op(
            run_id=run_id, epoch=3, attempt_id=recovery_attempt_id(run_id, 3),
            fence_check=lambda: None, predecessor_reconcile=plan,
        )
    assert provider_calls["n"] == 0  # no input provider => no lease/physical
    assert spy.calls == []


# --- R8 B6.2: deep provenance/physical-truth validation on every read ----------


def _b8r2_direct_heal(tmp_path):
    """Produce a REAL consistent (source epoch-1 completed journal + successor
    epoch-2 own reconcile journal + active generation) via the production adapter,
    for provenance counter-oracles exercised through the public read path."""

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)
    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_b8r2"
    boards = (_seed(),)
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    live_sha = adapter.inspect_live_artifact().sha256
    attempt1 = recovery_attempt_id(run_id, 1)
    attempt2 = recovery_attempt_id(run_id, 2)
    # Epoch 1 adopts the complete primary -> active generation + completed source.
    adapter.recover_and_cutover(
        run_id=run_id, epoch=1, attempt_id=attempt1,
        expected_live_sha256=live_sha, boards=boards, fence_check=lambda: None,
    )
    # Epoch 2 reconciles the bound predecessor -> writes its OWN completed journal.
    adapter.reconcile_predecessor_and_complete(
        run_id=run_id, epoch=2, attempt_id=attempt2,
        ancestry=((1, attempt1),),
        expected_live_sha256=live_sha, boards=boards, fence_check=lambda: None,
    )
    q = live.parent / "quarantine" / "global-discovery"
    return {
        "adapter": adapter, "live": live, "run_id": run_id,
        "attempt1": attempt1, "attempt2": attempt2, "boards": boards,
        "live_sha": live_sha,
        "own_path": q / attempt2 / "recovery_journal.json",
        "source_path": q / attempt1 / "recovery_journal.json",
        "active": read_active_generation(live),
    }


@pytest.mark.parametrize(
    "field,forged,code",
    [
        ("predecessor_journal_sha256", "f" * 64, "predecessor_journal_sha256_mismatch"),
        ("predecessor_evidence_ref", "forged-evidence", "predecessor_evidence_ref"),
        ("generation_id", "forged_generation", "generation_id_source_mismatch"),
        ("generation_manifest_sha256", "a" * 64, "generation_manifest_source_mismatch"),
        ("candidate_sha256", "b" * 64, "candidate_source_mismatch"),
    ],
)
def test_r6_b8r2_forged_own_journal_field_rejected_on_read(
    tmp_path, field, forged, code
):
    """R8 B6#2: a self-hashed forged successor own-journal field (recomputed valid
    journal_sha256) is rejected through the public reconcile read path by reopening
    the exact source journal + active physical truth; marker and all bytes stay."""

    from okto_pulse.community.adapters.global_discovery_recovery import (
        CommunityGlobalDiscoveryRecoveryError,
        _snapshot,
        _write_journal_with_directory_fsync,
    )

    ctx = _b8r2_direct_heal(tmp_path)
    live, adapter = ctx["live"], ctx["adapter"]
    source_bytes = ctx["source_path"].read_bytes()
    marker_before = bootstrap_marker_present(live)
    active_before = read_active_generation(live)
    active_sha_before = _snapshot(active_before.graph_path).sha256

    own = json.loads(ctx["own_path"].read_text(encoding="utf-8"))
    forged_journal = {k: v for k, v in own.items() if k != "journal_sha256"}
    forged_journal[field] = forged
    _write_journal_with_directory_fsync(ctx["own_path"], forged_journal)
    forged_bytes = ctx["own_path"].read_bytes()

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc:
        adapter.reconcile_attempt_terminal_truth(
            run_id=ctx["run_id"], epoch=2, attempt_id=ctx["attempt2"],
            expected_live_sha256=ctx["live_sha"], boards=ctx["boards"],
            fence_check=lambda: None,
        )
    assert code in str(exc.value)
    # Zero mutation from the rejected read.
    assert ctx["source_path"].read_bytes() == source_bytes
    assert ctx["own_path"].read_bytes() == forged_bytes
    assert bootstrap_marker_present(live) == marker_before
    active_after = read_active_generation(live)
    assert active_after.generation_id == active_before.generation_id
    assert _snapshot(active_after.graph_path).sha256 == active_sha_before


@pytest.mark.parametrize("mutation", ["missing_graph", "arbitrary_replacement"])
def test_r6_b8r2_forged_physical_state_rejected_on_read(tmp_path, mutation):
    """R8 B6#2: a real successor own-journal with a mutated/absent active graph is
    rejected on the public read path (the recorded physical truth no longer binds
    the actual active snapshot); marker and journal bytes stay."""

    from okto_pulse.community.adapters.global_discovery_recovery import (
        CommunityGlobalDiscoveryRecoveryError,
    )

    ctx = _b8r2_direct_heal(tmp_path)
    live, adapter = ctx["live"], ctx["adapter"]
    active = read_active_generation(live)
    marker_before = bootstrap_marker_present(live)
    own_bytes = ctx["own_path"].read_bytes()
    source_bytes = ctx["source_path"].read_bytes()

    if mutation == "missing_graph":
        active.graph_path.unlink()
        expected = "active_snapshot_missing"
    else:
        active.graph_path.write_bytes(b"arbitrary-corrupt-active-bytes")
        expected = "candidate_sha256_mismatch"

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc:
        adapter.reconcile_attempt_terminal_truth(
            run_id=ctx["run_id"], epoch=2, attempt_id=ctx["attempt2"],
            expected_live_sha256=ctx["live_sha"], boards=ctx["boards"],
            fence_check=lambda: None,
        )
    assert expected in str(exc.value)
    assert ctx["own_path"].read_bytes() == own_bytes
    assert ctx["source_path"].read_bytes() == source_bytes
    assert bootstrap_marker_present(live) == marker_before


# --- R8 B6.2 #3: self-consistent forged set dies on fresh semantic reopen ------


def test_r6_b8r2s2_self_consistent_forgery_dies_on_fresh_reopen(tmp_path):
    """R8 Step-1 #3: a fully self-consistent forged set (forged source + own
    journals with recomputed hashes + matching pointer/manifest/candidate over
    ARBITRARY active bytes) gets past every hash/pointer/manifest check and dies
    ONLY on the fresh real-runtime semantic reopen; marker + every forged byte
    stay.  Uses a byte-backed runtime so arbitrary active bytes are actually read."""

    from okto_pulse.community.adapters.global_discovery_recovery import (
        CommunityGlobalDiscoveryRecoveryError,
        _snapshot,
        _write_journal_with_directory_fsync,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"partial-primary")
    live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
    write_bootstrap_marker(live)
    boards = _two_seeds()

    def factory(path: Path):
        return _PersistentGlobalDiscoveryRuntime(path)

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    live_sha = adapter.inspect_live_artifact().sha256
    a1 = recovery_attempt_id("gdr_b8r2s2", 1)
    a2 = recovery_attempt_id("gdr_b8r2s2", 2)
    run_id = "gdr_b8r2s2"
    # Epoch 1 seed-rebuilds into REAL byte-backed storage; epoch 2 reconciles it.
    adapter.recover_and_cutover(
        run_id=run_id, epoch=1, attempt_id=a1,
        expected_live_sha256=live_sha, boards=boards, fence_check=lambda: None,
    )
    adapter.reconcile_predecessor_and_complete(
        run_id=run_id, epoch=2, attempt_id=a2, ancestry=((1, a1),),
        expected_live_sha256=live_sha, boards=boards, fence_check=lambda: None,
    )
    q = live.parent / "quarantine" / "global-discovery"
    source_path = q / a1 / "recovery_journal.json"
    own_path = q / a2 / "recovery_journal.json"
    active = read_active_generation(live)
    marker_before = bootstrap_marker_present(live)

    # Forge a FULLY self-consistent set: arbitrary active graph bytes + matching
    # candidate SHA in source+own + recomputed valid journal hashes; pointer and
    # manifest are left intact so generation/manifest bindings still match.
    active.graph_path.write_bytes(b"arbitrary-but-self-consistent-active-bytes")
    forged_sha = _snapshot(active.graph_path).sha256
    source = json.loads(source_path.read_bytes())
    source_forged = {k: v for k, v in source.items() if k != "journal_sha256"}
    source_forged["candidate_sha256"] = forged_sha
    _write_journal_with_directory_fsync(source_path, source_forged)
    forged_source_bytes = source_path.read_bytes()
    own = json.loads(own_path.read_bytes())
    own_forged = {k: v for k, v in own.items() if k != "journal_sha256"}
    own_forged["candidate_sha256"] = forged_sha
    own_forged["predecessor_journal_sha256"] = _sha_file(source_path)
    _write_journal_with_directory_fsync(own_path, own_forged)
    forged_own_bytes = own_path.read_bytes()

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc:
        adapter.reconcile_attempt_terminal_truth(
            run_id=run_id, epoch=2, attempt_id=a2,
            expected_live_sha256=live_sha, boards=boards, fence_check=lambda: None,
        )
    # It got PAST all hash/pointer/manifest checks and died on the fresh reopen.
    assert "candidate_schema_missing" in str(exc.value)
    assert bootstrap_marker_present(live) == marker_before
    assert source_path.read_bytes() == forged_source_bytes
    assert own_path.read_bytes() == forged_own_bytes
    assert active.graph_path.read_bytes() == b"arbitrary-but-self-consistent-active-bytes"


# --- epoch-2 resume: real durable handoff across a runtime restart ------------


def test_r6_worker_chain_real_durable_resume_handoff_epoch_two(
    tmp_path, prepared_recovery_admitter
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_r6resume"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "artifacts")
    db_path = tmp_path / f"{run_id}.sqlite3"
    boards = (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )

    # Runtime #1: cancel epoch 1 in flight so recover never runs at epoch 1.
    recording_port1 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    real1 = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    spy1 = _SpyRecovery(real1)
    spy1.reconcile_gate = threading.Event()
    started_at = datetime.now(timezone.utc)
    with _r6_env(recording_port1, tmp_path / "kgbase"):
        bundle1, _gr1 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy1,
        )
        try:
            command = _command(run_id, started_at=started_at)
            _admit_and_start(bundle1, prepared_recovery_admitter, command)
            assert spy1.reconcile_entered.wait(timeout=5.0)
            bundle1.control.cancel(
                run_id=run_id,
                expected_epoch=1,
                requested_at=started_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r6",
                reason="R6 cancel then resume",
            )
            spy1.reconcile_gate.set()
            _wait_until(
                bundle1.control,
                run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.CANCELLED,
            )
            assert "recover_and_cutover" not in spy1.calls
        finally:
            bundle1.close()  # CLOSE epoch-1 runtime -> no lease-release race

    # No epoch-2 durable inputs exist yet.
    provider_check = GlobalDiscoveryRecoveryWorkerInputStore(artifact_store)
    assert provider_check.load(run_id, epoch=2) is None

    # Runtime #2: fresh runtime over the SAME db + artifact store; resume -> ep2.
    recording_port2 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    real2 = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    spy2 = _SpyRecovery(real2)
    with _r6_env(recording_port2, tmp_path / "kgbase"):
        bundle2, _gr2 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy2,
        )
        try:
            bundle2.control.resume(
                run_id=run_id,
                expected_epoch=1,
                requested_at=started_at + timedelta(seconds=2),
                requested_by_actor_id="operator-r6",
                reason="R6 resume to epoch 2",
            )
            terminal = _wait_until(
                bundle2.control,
                run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.SUCCESS,
                timeout_seconds=60.0,
            )
        finally:
            bundle2.close()

    assert terminal.epoch == 2
    attempt2 = recovery_attempt_id(run_id, 2)
    assert terminal.attempt_id == attempt2

    # The REAL durable handoff CREATED the epoch-2 artifact, and a FRESH provider
    # loads the EXACT sha / full board tuple / counts for epoch 2.
    fresh_provider = CommunityDurableRecoveryInputProvider(artifact_store=artifact_store)
    epoch2_inputs = fresh_provider(run_id=run_id, epoch=2)
    assert isinstance(epoch2_inputs, RecoveryNativeInputs)
    assert epoch2_inputs.expected_live_sha256 == live_sha
    assert epoch2_inputs.boards == boards
    assert epoch2_inputs.terminal_counts == counts

    # The epoch-2 physical operation ran with the exact new identity + inputs.
    assert "recover_and_cutover" in spy2.calls
    # B6.3 negative unrelated-resume oracle: the predecessor epoch 1 was CANCELLED
    # (not a pending physical-reconciliation record), so NO predecessor-reconcile
    # bridge runs — epoch 2 performs its own normal recovery and never adopts an
    # unrelated old truth.
    assert "reconcile_predecessor_and_complete" not in spy2.calls
    assert spy2.forwarded["epoch"] == 2
    assert spy2.forwarded["attempt_id"] == attempt2
    assert spy2.forwarded["boards"] == boards
    assert spy2.fences["reconcile"] is spy2.fences["recover"]
    assert spy2.fence_lease is spy2.active_lease

    # R6.8 ownership: epoch-2 acquired the production writer lease EXACTLY once
    # for the epoch-2 owner and released it EXACTLY once on the captured fence
    # lease object (no leaked ownership across the resume boundary).
    owner_token2 = _assert_exact_acquire_contract(
        recording_port2, owner_id=f"{run_id}:{attempt2}"
    )
    _assert_exactly_one_release(
        recording_port2, owner_token=owner_token2, lease=spy2.fence_lease
    )

    journal_path = (
        live.parent / "quarantine" / "global-discovery"
        / attempt2 / "recovery_journal.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["kind"] == "adopt_complete_primary"
    assert journal["phase"] == "completed"

    # R6.8 anti split-brain: epoch-1 held and RELEASED the writer lease exactly
    # once BEFORE epoch-2 ever acquired it (distinct owners/tokens, clean handoff,
    # no overlapping ownership), and epoch-2 is bound as the successor of epoch-1.
    attempt1 = recovery_attempt_id(run_id, 1)
    assert attempt1 != attempt2
    owner_token1 = _assert_exact_acquire_contract(
        recording_port1, owner_id=f"{run_id}:{attempt1}"
    )
    _assert_exactly_one_release(
        recording_port1, owner_token=owner_token1, lease=spy1.fence_lease
    )
    assert owner_token1 != owner_token2
    assert terminal.supersedes_epoch == 1


# --- R8 B6#3/#5: N+1 loss in the EXACT interval (post-source-reconcile, --------
# --- pre-own-journal) with boundary-captured zero-mutation observation ---------


def _raw_active_state(live: Path) -> dict[str, bytes]:
    """Byte-exact capture of the COMPLETE physical Global Discovery surface: the
    raw active pointer file (``active_generation.json``), EVERY file under the
    generations tree (``generation_manifest.json``, graph primary, WAL and any
    sidecars — all generations, not only the active one), and the legacy
    primary/WAL pair.  Compared with ``==`` this kills a semantically-equivalent
    stale rewrite of pointer/manifest that would survive id/SHA comparison."""

    root = live.parent
    captured: dict[str, bytes] = {}
    pointer = root / "active_generation.json"
    if pointer.exists():
        captured["active_generation.json"] = pointer.read_bytes()
    generations = root / "discovery.generations"
    if generations.exists():
        for path in sorted(generations.rglob("*")):
            if path.is_file() and not path.name.endswith(".tmp"):
                captured[path.relative_to(root).as_posix()] = path.read_bytes()
    for legacy in (live, live.with_name(live.name + ".wal")):
        if legacy.exists():
            captured[legacy.name] = legacy.read_bytes()
    return captured


_COLD_RESUME_SCRIPT = r'''
import json, os, sys

tests_dir = sys.argv[1]
community_src = sys.argv[9]
core_src = sys.argv[10]
# R8-B7.8 (#1): build the checkout source roots DETERMINISTICALLY before any
# import — never rely on inherited PYTHONPATH or an installed distribution.
# Editable environments may already expose these exact roots through a ``.pth``
# file *after* site-packages.  Merely skipping an existing entry would then let
# an older installed ``okto_pulse.core`` win before the checkout.  Remove every
# occurrence first and reinsert the roots at the front, matching the suite's
# repository-checkout activation contract.
for root in (core_src, community_src):
    if root:
        while root in sys.path:
            sys.path.remove(root)
        sys.path.insert(0, root)
sys.path.insert(0, tests_dir)

import okto_pulse.community.adapters.global_discovery_recovery as production_mod
import okto_pulse.core.kg.global_discovery_recovery_control as core_mod
import test_global_discovery_recovery_worker_chain as t

# R8-B7.9: BOTH the community AND the core modules MUST belong to the expected
# checkouts — REAL Path containment (is_relative_to), never string prefixes;
# resolving site-packages is a hard failure.
from pathlib import Path as _P

prod_path = _P(production_mod.__file__).resolve()
core_path = _P(core_mod.__file__).resolve()
test_path = _P(t.__file__).resolve()
assert prod_path.is_relative_to(_P(community_src).resolve()), prod_path
assert core_path.is_relative_to(_P(core_src).resolve()), core_path
assert test_path.is_relative_to(_P(tests_dir).resolve()), test_path
for checked in (prod_path, core_path, test_path):
    assert "site-packages" not in str(checked).lower(), checked
prod_file = str(prod_path)
core_file = str(core_path)
test_file = str(test_path)

from pathlib import Path
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.infra.config import configure_settings

configure_settings(CommunitySettings())
mode = sys.argv[2]
live = Path(sys.argv[3])
kg_base = Path(sys.argv[4])
run_id = sys.argv[5]
epoch = int(sys.argv[6])
attempt_id = sys.argv[7]
live_sha = sys.argv[8]
boards = t._two_real_seeds()
ordered = tuple(sorted(boards, key=lambda b: b.board_id))
adapter = t.CommunityGlobalDiscoveryRecovery(
    global_runtime=t.CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: live
    ),
    graph_path_provider=lambda: live,
)
with t._kg_base_dir_configured(kg_base):
    with t._registered_recording_port(
        t._RecordingWriteLockPort(t.CommunityLocalWriteLockPort())
    ):
        with t.global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:cold-resume",
            ttl_seconds=t._RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            if mode == "legacy":
                result = adapter.recover_and_cutover(
                    run_id=run_id, expected_live_sha256=live_sha,
                    boards=boards, fence_check=lease.assert_fenced,
                )
            else:
                result = adapter.reconcile_attempt_terminal_truth(
                    run_id=run_id, epoch=epoch, attempt_id=attempt_id,
                    expected_live_sha256=live_sha, boards=ordered,
                    fence_check=lease.assert_fenced,
                )
print("COLD-RESULT:" + json.dumps({
    "outcome": result.outcome,
    "prod_file": prod_file,
    "core_file": core_file,
    "test_file": test_file,
}))
'''


def _run_cold_resume(
    tmp_path, *, mode, live, kg_base, run_id, epoch, attempt_id, live_sha
):
    """R8-B7.7 (#1): run a completed+marker RESUME in a brand-new COLD python
    process (fresh interpreter, fresh Ladybug/vector state) through the SAME
    public production reader, and return its reported outcome."""

    import os
    import subprocess
    import sys

    script_path = tmp_path / "cold_resume_child.py"
    script_path.write_text(_COLD_RESUME_SCRIPT, encoding="utf-8")
    tests_dir = Path(__file__).resolve().parent
    community_src = tests_dir.parent / "src"
    core_src = resolve_core_repo(tests_dir.parent) / "src"
    # Hermetic child env: NO inherited PYTHONPATH; neutral cwd (tmp) so the
    # child cannot silently rely on repo-relative resolution.
    child_env = dict(os.environ)
    child_env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [
            sys.executable, str(script_path), str(tests_dir), mode,
            str(live), str(kg_base), run_id, str(epoch), attempt_id, live_sha,
            str(community_src), str(core_src),
        ],
        capture_output=True, text=True, timeout=300,
        env=child_env, cwd=str(tmp_path),
    )
    if proc.returncode != 0:
        # Diagnostics: the full child stderr survives pytest's repr truncation.
        (tmp_path / "cold_child_stderr.txt").write_text(
            proc.stderr or "", encoding="utf-8"
        )
        (tmp_path / "cold_child_stdout.txt").write_text(
            proc.stdout or "", encoding="utf-8"
        )
    assert proc.returncode == 0, str(tmp_path / "cold_child_stderr.txt")
    lines = [
        row for row in proc.stdout.splitlines() if row.startswith("COLD-RESULT:")
    ]
    assert lines, (proc.stdout[-2000:], proc.stderr[-2000:])
    payload = json.loads(lines[-1][len("COLD-RESULT:"):])
    # R8-B7.9: the PARENT independently re-verifies (REAL Path containment)
    # that the child exercised THIS checkout's bytes for BOTH community and
    # core — never an installed distribution.
    prod_file = Path(payload["prod_file"]).resolve()
    core_file = Path(payload["core_file"]).resolve()
    test_file = Path(payload["test_file"]).resolve()
    assert prod_file.is_relative_to(community_src.resolve()), prod_file
    assert core_file.is_relative_to(core_src.resolve()), core_file
    assert test_file.is_relative_to(tests_dir), test_file
    for checked in (prod_file, core_file, test_file):
        assert "site-packages" not in str(checked).lower(), checked
    return payload


def _claim_from_fence(physical_fence_check):
    """Recover the exact ``RecoveryDispatchClaim`` the composite physical fence
    closure is bound to, WITHOUT monkeypatching — by inspecting the real nested
    closures (analogous to ``_lease_from_fence``).  Binds the fence's dispatch
    claim check to the EXACT dispatch_id/claim_token/worker_id authority."""

    from okto_pulse.community.adapters.global_discovery_recovery_worker import (
        RecoveryDispatchClaim,
    )

    seen: list[object] = []
    stack = [physical_fence_check]
    while stack:
        fn = stack.pop()
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, RecoveryDispatchClaim):
                return val
            if callable(val) and getattr(val, "__closure__", None) and val not in seen:
                seen.append(val)
                stack.append(val)
    return None


def _quarantine_tree(live: Path, *, exclude_scratch: bool = False):
    """Byte-exact capture of the COMPLETE quarantine tree: every directory
    (marked) and every file's raw bytes."""

    root = live.parent / "quarantine"
    captured: dict[str, object] = {}
    if not root.exists():
        return captured
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if exclude_scratch and "resume-validate-scratch" in rel:
            continue
        captured[rel] = path.read_bytes() if path.is_file() else "<dir>"
    return captured


def _tree_plus_orphans(tree_before, live: Path, orphan_dirs):
    """Expected quarantine tree = the frozen BEFORE tree plus EXACTLY the
    enumerated own-orphan entries — never a blanket scratch exclusion."""

    root = live.parent / "quarantine"
    expected = dict(tree_before)
    for orphan in orphan_dirs:
        expected[orphan.relative_to(root).as_posix()] = "<dir>"
        for path in sorted(orphan.rglob("*")):
            rel = path.relative_to(root).as_posix()
            expected[rel] = path.read_bytes() if path.is_file() else "<dir>"
    return expected


def _crossed_pending_epoch1(
    tmp_path, prepared_recovery_admitter, *, run_id, live, factory, db_path,
    artifact_store, boards, counts,
):
    """Drive epoch 1 to the exact pending sentinel (pointer crossed, REAL fence
    lost) and return its terminal status.  Shared by the R8 B6#3/#4 tests."""

    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )
    attempt1 = recovery_attempt_id(run_id, 1)
    port1 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    real1 = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
    )
    spy1 = _SpyRecovery(real1)
    spy1.fence_wrap = _invalidate_after_cross_wrap(
        port1, run_id, attempt1, live, {"done": False}
    )
    with _r6_env(port1, tmp_path / "kgbase"):
        bundle1, _gr1 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy1,
            heartbeat_interval_ms=5_000,
        )
        try:
            _admit_and_start(
                bundle1, prepared_recovery_admitter,
                _command(run_id, started_at=datetime.now(timezone.utc)),
            )
            epoch1_terminal = _wait_until(
                bundle1.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.PARTIAL,
                timeout_seconds=8.0,
            )
        finally:
            bundle1.close()
    assert epoch1_terminal.state is RecoveryRunState.PARTIAL
    assert epoch1_terminal.reason_code == "recovery_physical_reconciliation_pending"
    assert epoch1_terminal.physical_truth is None
    return epoch1_terminal


def test_r8_b63_loss_between_source_reconcile_and_own_journal(
    tmp_path, prepared_recovery_admitter, monkeypatch
):
    """R8 B6#3 + B6#5: epoch 1 crosses the pointer then loses the fence (exact
    pending PARTIAL).  Epoch 2 (N+1) resumes; its bridge FINISHES the source
    reconciliation (the epoch-1 journal heals to ``completed``), then the REAL
    external writer ownership is invalidated at the precise successor-own-journal
    write boundary (seam wraps the module journal writer for
    ``kind=reconcile_predecessor_cutover``; the production writer asserts the
    fence BEFORE any byte is written).  Epoch 2 must terminalize as the exact
    pending sentinel with NO own journal, and the physical state captured AT the
    injected loss boundary (pointer, manifest, active snapshot, completed source
    journal bytes) must be byte-identical after the worker settles — a stale
    mutation between the loss and a later first observation cannot survive
    (B6#5).  Epoch 3 (N+2) walks the persisted pending chain and heals binding
    the EXACT completed-source raw SHA captured at the boundary."""

    import hashlib

    import okto_pulse.community.adapters.global_discovery_recovery as gdr_mod
    from okto_pulse.community.adapters.global_discovery_recovery import _snapshot

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_r8b63"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "artifacts"
    )
    db_path = tmp_path / f"{run_id}.sqlite3"
    boards = (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )

    attempt1 = recovery_attempt_id(run_id, 1)
    attempt2 = recovery_attempt_id(run_id, 2)
    attempt3 = recovery_attempt_id(run_id, 3)
    q_root = live.parent / "quarantine" / "global-discovery"
    epoch1_journal_path = q_root / attempt1 / "recovery_journal.json"
    epoch2_journal_path = q_root / attempt2 / "recovery_journal.json"
    epoch3_journal_path = q_root / attempt3 / "recovery_journal.json"

    epoch1_terminal = _crossed_pending_epoch1(
        tmp_path, prepared_recovery_admitter, run_id=run_id, live=live,
        factory=factory, db_path=db_path, artifact_store=artifact_store,
        boards=boards, counts=counts,
    )

    def _real():
        return CommunityGlobalDiscoveryRecovery(
            global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
            graph_path_provider=lambda: live,
            runtime_factory=factory,  # type: ignore[arg-type]
        )

    # --- Epoch 2 (N+1): the bridge completes the SOURCE reconciliation, then the
    #     REAL ownership is invalidated at the own-journal write boundary. ---
    port2 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spy2 = _SpyRecovery(_real())
    boundary: dict[str, object] = {}
    real_writer = gdr_mod._write_journal_with_directory_fsync

    def losing_writer(path, payload, *, fence_check=None):
        if (
            payload.get("kind") == "reconcile_predecessor_cutover"
            and "source_bytes" not in boundary
        ):
            # The bridge builds its own journal only AFTER the source
            # reconciliation finished: capture the exact physical state at THIS
            # loss boundary (B6#5), then invalidate the REAL external ownership
            # so the production writer's fence assert raises the genuine
            # GlobalDiscoveryWriterFenceLost BEFORE any byte is written.
            active = read_active_generation(live)
            assert active is not None
            boundary["generation_id"] = active.generation_id
            boundary["manifest"] = active.manifest_sha256
            boundary["snapshot_sha"] = _snapshot(active.graph_path).sha256
            boundary["raw"] = _raw_active_state(live)
            boundary["source_bytes"] = epoch1_journal_path.read_bytes()
            boundary["source_sha"] = hashlib.sha256(
                boundary["source_bytes"]
            ).hexdigest()
            boundary["own_absent"] = not epoch2_journal_path.exists()
            token = _acquisition_for_owner(
                port2, f"{run_id}:{attempt2}"
            )[1].owner_token
            port2.release_single_writer_sync(
                board_id=GLOBAL_DISCOVERY_WRITER_SCOPE,
                artifact_id=GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
                owner_token=token,
            )
        return real_writer(path, payload, fence_check=fence_check)

    monkeypatch.setattr(
        gdr_mod, "_write_journal_with_directory_fsync", losing_writer
    )
    with _r6_env(port2, tmp_path / "kgbase"):
        bundle2, _gr2 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy2,
            heartbeat_interval_ms=5_000,
        )
        try:
            bundle2.control.resume(
                run_id=run_id, expected_epoch=1,
                requested_at=epoch1_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r8", reason="B6#3 resume epoch 2",
            )
            epoch2_terminal = _wait_until(
                bundle2.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.PARTIAL,
                timeout_seconds=8.0,
            )
        finally:
            bundle2.close()

    # The loss fired in the REQUIRED interval: after the source reconciliation
    # (the captured source journal is COMPLETED) and before any own journal.
    assert "source_bytes" in boundary, "seam never reached the own-journal write"
    source_at_boundary = json.loads(boundary["source_bytes"].decode("utf-8"))
    assert source_at_boundary["phase"] == "completed"
    assert source_at_boundary["outcome"] == "completed"
    assert boundary["own_absent"] is True
    assert spy2.calls.count("reconcile_predecessor_and_complete") == 1

    # Epoch 2 is the EXACT pending sentinel; no own journal was ever written.
    assert epoch2_terminal.state is RecoveryRunState.PARTIAL
    assert epoch2_terminal.terminal_outcome is RecoveryTerminalOutcome.PARTIAL
    assert epoch2_terminal.reason_code == "recovery_physical_reconciliation_pending"
    assert epoch2_terminal.retryable is False
    assert epoch2_terminal.physical_truth is None
    assert not epoch2_journal_path.exists()

    # B6#5: compare AFTER the worker settled against the state captured AT the
    # exact injected loss boundary — zero post-loss mutation, byte-for-byte over
    # the COMPLETE raw physical surface (pointer file, manifest, graph/WAL and
    # sidecars of every generation) plus the completed source journal bytes.  A
    # semantically-equivalent stale rewrite of pointer/manifest dies here.
    active_after = read_active_generation(live)
    assert active_after is not None
    assert active_after.generation_id == boundary["generation_id"]
    assert active_after.manifest_sha256 == boundary["manifest"]
    assert _snapshot(active_after.graph_path).sha256 == boundary["snapshot_sha"]
    assert _raw_active_state(live) == boundary["raw"]
    assert epoch1_journal_path.read_bytes() == boundary["source_bytes"]

    # --- Epoch 3 (N+2): walk the persisted chain and heal from the exact source.
    port3 = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spy3 = _SpyRecovery(_real())
    with _r6_env(port3, tmp_path / "kgbase"):
        bundle3, _gr3 = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spy3,
            heartbeat_interval_ms=5_000,
        )
        try:
            bundle3.control.resume(
                run_id=run_id, expected_epoch=2,
                requested_at=epoch2_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r8", reason="B6#3 resume epoch 3",
            )
            epoch3_terminal = _wait_until(
                bundle3.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.SUCCESS,
                timeout_seconds=8.0,
            )
        finally:
            bundle3.close()
    assert epoch3_terminal.state is RecoveryRunState.SUCCESS
    assert epoch3_terminal.epoch == 3
    assert epoch3_terminal.attempt_id == attempt3
    assert epoch3_terminal.physical_truth is not None
    assert epoch3_terminal.physical_truth.attempt_id == attempt3
    assert spy3.calls.count("reconcile_predecessor_and_complete") == 1
    assert "recover_and_cutover" not in spy3.calls

    # The heal bound the EXACT completed-source raw SHA captured at the loss
    # boundary — the source journal was never rewritten after the loss.
    own_journal = json.loads(epoch3_journal_path.read_text(encoding="utf-8"))
    assert own_journal["kind"] == "reconcile_predecessor_cutover"
    assert own_journal["predecessor_epoch"] == 1
    assert own_journal["predecessor_attempt_id"] == attempt1
    assert own_journal["predecessor_journal_sha256"] == boundary["source_sha"]
    assert epoch1_journal_path.read_bytes() == boundary["source_bytes"]
    active_final = read_active_generation(live)
    assert active_final is not None
    assert active_final.generation_id == boundary["generation_id"]
    assert active_final.manifest_sha256 == boundary["manifest"]
    assert _snapshot(active_final.graph_path).sha256 == boundary["snapshot_sha"]
    # The heal is PURE reconciliation: the complete raw physical surface is still
    # byte-identical to the loss boundary after epoch 3's SUCCESS.
    assert _raw_active_state(live) == boundary["raw"]

    # Exact epoch chain: 1 (pending source, physically healed) <- 2 (journal-less
    # pending) <- 3 (SUCCESS); no epoch fabrication.
    read_engine = _make_engine(db_path)
    try:
        row1 = _read_attempt_row(read_engine, run_id, 1)
        row2 = _read_attempt_row(read_engine, run_id, 2)
        row3 = _read_attempt_row(read_engine, run_id, 3)
        row4 = _read_attempt_row(read_engine, run_id, 4)
    finally:
        read_engine.dispose()
    assert row1["state"] == RecoveryRunState.PARTIAL.value
    assert row1["superseded_by_epoch"] == 2
    assert row2["state"] == RecoveryRunState.PARTIAL.value
    assert row2["supersedes_epoch"] == 1
    assert row2["superseded_by_epoch"] == 3
    assert row3["state"] == RecoveryRunState.SUCCESS.value
    assert row3["supersedes_epoch"] == 2
    assert row4 is None


# --- R8 B6#4: completed-first own-journal crash floor through worker/store x2 --


def test_r8_b64_own_journal_crash_floor_two_owners_same_epoch(
    tmp_path, prepared_recovery_admitter, monkeypatch
):
    """R8 B6#4: the completed-first crash floor proven through the NORMAL
    worker/store flow TWICE for the SAME successor epoch.  Owner A (epoch 2)
    reconciles the source and durably writes its OWN reconciliation journal; its
    SQL dispatch claim is then STOLEN (controlled crash) so SQL completion is
    prevented — the production composite fence raises ``stale_dispatch_claim``
    and the attempt steps aside NON-terminally.  After the stolen claim is made
    reclaimable (exact claim expiry, no wall-clock sleep), a DISTINCT
    worker/store owner B claims that SAME epoch through the normal poller flow
    and consumes the existing own journal idempotently, producing SQL SUCCESS.
    Both owners entered the bridge (two reconciliation entries), same epoch, no
    new epoch; own-journal raw bytes/SHA, source journal bytes, pointer, manifest
    and active snapshot are unchanged; the new dispatch claim and writer-lease
    authorities are distinct; no late clear or journal rewrite."""

    import hashlib
    import time

    import okto_pulse.community.adapters.global_discovery_recovery as gdr_mod
    from okto_pulse.community.adapters.global_discovery_recovery import _snapshot

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    run_id = "gdr_r8b64"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "artifacts"
    )
    db_path = tmp_path / f"{run_id}.sqlite3"
    boards = (_seed(),)
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )

    attempt1 = recovery_attempt_id(run_id, 1)
    attempt2 = recovery_attempt_id(run_id, 2)
    q_root = live.parent / "quarantine" / "global-discovery"
    epoch1_journal_path = q_root / attempt1 / "recovery_journal.json"
    epoch2_journal_path = q_root / attempt2 / "recovery_journal.json"

    epoch1_terminal = _crossed_pending_epoch1(
        tmp_path, prepared_recovery_admitter, run_id=run_id, live=live,
        factory=factory, db_path=db_path, artifact_store=artifact_store,
        boards=boards, counts=counts,
    )

    def _real():
        return CommunityGlobalDiscoveryRecovery(
            global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
            graph_path_provider=lambda: live,
            runtime_factory=factory,  # type: ignore[arg-type]
        )

    # --- Owner A (epoch 2): durably write the own journal, then STEAL the SQL
    #     dispatch claim so SQL completion is prevented (controlled crash). ---
    portA = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spyA = _SpyRecovery(_real())
    steal_engine = _make_engine(db_path)
    floor: dict[str, object] = {}
    owner_phase = {"current": "A"}
    # Writer instrumentation by kind/path: EVERY own-journal write attempt is
    # recorded with the owner phase — final byte equality alone cannot kill an
    # identical rewrite, so owner B must show ZERO write attempts.
    cutover_write_attempts: list[tuple[str, str]] = []
    real_writer = gdr_mod._write_journal_with_directory_fsync

    def stealing_writer(path, payload, *, fence_check=None):
        if payload.get("kind") == "reconcile_predecessor_cutover":
            cutover_write_attempts.append(
                (owner_phase["current"], Path(path).as_posix())
            )
        supported = real_writer(path, payload, fence_check=fence_check)
        if (
            payload.get("kind") == "reconcile_predecessor_cutover"
            and "own_bytes" not in floor
        ):
            # The own journal is DURABLE (completed-first floor).  Capture the
            # exact floor state — including the COMPLETE raw physical surface —
            # then steal the SQL dispatch claim: owner A can no longer complete
            # SQL and must step aside non-terminally.
            floor["own_bytes"] = epoch2_journal_path.read_bytes()
            floor["own_sha"] = hashlib.sha256(floor["own_bytes"]).hexdigest()
            floor["source_bytes"] = epoch1_journal_path.read_bytes()
            active = read_active_generation(live)
            assert active is not None
            floor["generation_id"] = active.generation_id
            floor["manifest"] = active.manifest_sha256
            floor["snapshot_sha"] = _snapshot(active.graph_path).sha256
            floor["raw"] = _raw_active_state(live)
            row = _read_recovery_dispatch_at(steal_engine, run_id, 2, attempt2)
            assert row is not None
            floor["dispatch_id"] = row["dispatch_id"]
            floor["claim_a"] = row["claim_token"]
            floor["worker_a"] = row["worker_id"]
            with steal_engine.begin() as conn:
                conn.execute(
                    update(GlobalDiscoveryRecoveryDispatch)
                    .where(
                        GlobalDiscoveryRecoveryDispatch.dispatch_id
                        == row["dispatch_id"]
                    )
                    .values(claim_token="stolen-r8-b64")
                )
            # ADENDO: prove the composite fence's CLAIM CHECK is live — invoked
            # immediately post-theft it must fail as EXACTLY stale_dispatch_claim
            # BEFORE any SQL completion.  A mutant that removes/ignores the SQL
            # dispatch-claim check cannot produce this code.
            if fence_check is not None:
                try:
                    fence_check()
                except RecoveryWorkerFenceError as exc:
                    floor["post_theft_fence_code"] = exc.code
        return supported

    monkeypatch.setattr(
        gdr_mod, "_write_journal_with_directory_fsync", stealing_writer
    )
    with _r6_env(portA, tmp_path / "kgbase"):
        bundleA, _grA = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spyA,
            heartbeat_interval_ms=5_000,
        )
        try:
            bundleA.control.resume(
                run_id=run_id, expected_epoch=1,
                requested_at=epoch1_terminal.updated_at + timedelta(seconds=1),
                requested_by_actor_id="operator-r8", reason="B6#4 owner A",
            )
            deadline = time.monotonic() + 8.0
            while "own_bytes" not in floor and time.monotonic() < deadline:
                time.sleep(0.02)
            assert "own_bytes" in floor, "owner A never reached the journal write"
            token_a = _acquisition_for_owner(
                portA, f"{run_id}:{attempt2}"
            )[1].owner_token
            # Owner A drains and steps aside WITHOUT terminalizing.
            _wait_for_release(portA, token_a)
            status_mid = bundleA.control.status(run_id)
            assert status_mid.state is RecoveryRunState.RUNNING
            assert status_mid.terminal_outcome is None
            assert status_mid.epoch == 2
            assert status_mid.attempt_id == attempt2
        finally:
            bundleA.close()

    # The completed-first floor survived the crash: the own journal is durable
    # and byte-identical; SQL was NOT completed by the stale owner.  The COMPLETE
    # raw physical surface is compared IMMEDIATELY after owner A settled — BEFORE
    # the claim is expired/reclaimed — so a stale mutation in that window cannot
    # hide behind owner B's later observation.
    assert epoch2_journal_path.read_bytes() == floor["own_bytes"]
    assert _raw_active_state(live) == floor["raw"]
    assert epoch1_journal_path.read_bytes() == floor["source_bytes"]
    assert spyA.calls.count("reconcile_predecessor_and_complete") == 1
    # Owner A wrote the own journal EXACTLY once (no rewrite attempt).
    assert cutover_write_attempts == [
        ("A", epoch2_journal_path.as_posix())
    ]
    # ADENDO: the post-theft claim check failed as EXACTLY stale_dispatch_claim
    # (recorded inside the seam, before SQL completion was even possible).
    assert floor["post_theft_fence_code"] == "stale_dispatch_claim"
    # ADENDO: owner A's composite fence is bound to the EXACT authority — the
    # RecoveryDispatchClaim recovered from the fence closure carries A's original
    # dispatch_id/claim_token/worker_id, and the fence's lease closure carries
    # the exact owner_token acquired on port A.
    claim_obj_a = _claim_from_fence(spyA.fences["predecessor"])
    assert claim_obj_a is not None
    assert claim_obj_a.dispatch_id == floor["dispatch_id"]
    assert claim_obj_a.claim_token == floor["claim_a"]
    assert claim_obj_a.worker_id == floor["worker_a"]
    lease_obj_a = _lease_from_fence(spyA.fences["predecessor"])
    assert lease_obj_a is not None
    assert lease_obj_a.owner_token == token_a

    # Make the stolen claim reclaimable via its EXACT expiry (no wall-clock wait).
    with steal_engine.begin() as conn:
        conn.execute(
            update(GlobalDiscoveryRecoveryDispatch)
            .where(
                GlobalDiscoveryRecoveryDispatch.dispatch_id == floor["dispatch_id"]
            )
            .values(
                claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5)
            )
        )

    # --- Owner B: a DISTINCT worker/store owner adopts the SAME epoch through
    #     the normal poller flow and consumes the existing own journal. ---
    portB = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    spyB = _SpyRecovery(_real())
    spyB.reconcile_gate = threading.Event()
    owner_phase["current"] = "B"
    with _r6_env(portB, tmp_path / "kgbase"):
        bundleB, _grB = _build_bundle(
            engine=_make_engine(db_path), artifact_store=artifact_store,
            live=live, factory=factory, run_id=run_id, spy=spyB,
            heartbeat_interval_ms=5_000,
        )
        try:
            bundleB.worker.start()  # restart-adoption: claim reclaimable work
            # ADENDO: hold owner B at the reconcile gate and prove its composite
            # fence carries the NEW valid claim: invoking the captured fence must
            # NOT raise (the claim check passes with B's reclaimed authority).
            assert spyB.reconcile_entered.wait(timeout=8.0)
            fence_b = spyB.fences["reconcile"]
            fence_b()  # must not raise: new claim is valid
            row_b = _read_recovery_dispatch_at(steal_engine, run_id, 2, attempt2)
            assert row_b is not None
            claim_obj_b = _claim_from_fence(fence_b)
            assert claim_obj_b is not None
            assert claim_obj_b.dispatch_id == floor["dispatch_id"]
            assert claim_obj_b.claim_token == row_b["claim_token"]
            assert claim_obj_b.claim_token != floor["claim_a"]
            assert claim_obj_b.claim_token != "stolen-r8-b64"
            assert claim_obj_b.worker_id == row_b["worker_id"]
            assert claim_obj_b.worker_id != floor["worker_a"]
            token_b_live = _acquisition_for_owner(
                portB, f"{run_id}:{attempt2}"
            )[1].owner_token
            lease_obj_b = _lease_from_fence(fence_b)
            assert lease_obj_b is not None
            assert lease_obj_b.owner_token == token_b_live
            assert lease_obj_b.owner_token != token_a
            spyB.reconcile_gate.set()
            terminal = _wait_until(
                bundleB.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.SUCCESS,
                timeout_seconds=8.0,
            )
        finally:
            spyB.reconcile_gate.set()
            bundleB.close()

    # SQL SUCCESS by owner B for the SAME epoch — no new epoch was created.
    assert terminal.state is RecoveryRunState.SUCCESS
    assert terminal.epoch == 2
    assert terminal.attempt_id == attempt2
    assert terminal.supersedes_epoch == 1
    assert terminal.physical_truth is not None
    assert terminal.physical_truth.attempt_id == attempt2
    assert (
        terminal.physical_truth.evidence_ref
        == f"community-global-discovery-quarantine:{attempt2}"
    )

    # Exactly TWO reconciliation entries through the bridge — one per owner, same
    # epoch — and neither owner ran fresh physical recovery.
    assert spyA.calls.count("reconcile_predecessor_and_complete") == 1
    assert spyB.calls.count("reconcile_predecessor_and_complete") == 1
    assert "recover_and_cutover" not in spyA.calls
    assert "recover_and_cutover" not in spyB.calls
    # Owner B performed ZERO own-journal write attempts (idempotent consume; an
    # identical rewrite would be invisible to byte equality but is killed here).
    assert cutover_write_attempts == [
        ("A", epoch2_journal_path.as_posix())
    ]

    # No late clear/rewrite: own journal raw bytes/SHA and source journal bytes
    # unchanged; the COMPLETE raw physical surface (pointer file, manifest,
    # graph/WAL/sidecars) byte-identical to the floor captured at the crash.
    final_own = epoch2_journal_path.read_bytes()
    assert final_own == floor["own_bytes"]
    assert hashlib.sha256(final_own).hexdigest() == floor["own_sha"]
    assert epoch1_journal_path.read_bytes() == floor["source_bytes"]
    active_final = read_active_generation(live)
    assert active_final is not None
    assert active_final.generation_id == floor["generation_id"]
    assert active_final.manifest_sha256 == floor["manifest"]
    assert _snapshot(active_final.graph_path).sha256 == floor["snapshot_sha"]
    assert _raw_active_state(live) == floor["raw"]

    # Exact distinct authorities: the reclaimed dispatch row is the SAME dispatch
    # (same epoch) under a NEW claim token and a DISTINCT worker id; the writer
    # lease owner tokens of the two owners are distinct.
    final_dispatch = _read_recovery_dispatch_at(steal_engine, run_id, 2, attempt2)
    assert final_dispatch is not None
    assert final_dispatch["dispatch_id"] == floor["dispatch_id"]
    assert final_dispatch["claim_token"] != floor["claim_a"]
    assert final_dispatch["claim_token"] != "stolen-r8-b64"
    assert final_dispatch["worker_id"] != floor["worker_a"]
    token_b = _acquisition_for_owner(
        portB, f"{run_id}:{attempt2}"
    )[1].owner_token
    assert token_b != token_a
    steal_engine.dispose()

    # Same-epoch attempt row is SUCCESS; no epoch 3 exists (no fabrication).
    read_engine = _make_engine(db_path)
    try:
        row2 = _read_attempt_row(read_engine, run_id, 2)
        row3 = _read_attempt_row(read_engine, run_id, 3)
    finally:
        read_engine.dispose()
    assert row2["state"] == RecoveryRunState.SUCCESS.value
    assert row2["supersedes_epoch"] == 1
    assert row3 is None


# --- R8 B7.4: fabricated self-consistent seed truth dies ONLY on the fresh ----
# --- REAL Ladybug reopen, through the PUBLIC production reader ----------------


def test_r8_b7_fabricated_seed_truth_dies_on_fresh_real_reopen(
    tmp_path, prepared_recovery_admitter, monkeypatch
):
    """R8-B7.4: starting from a COMPLETED real seed-rebuild journal, build a
    fully self-consistent FORGED physical state: arbitrary/corrupt active graph
    bytes plus a matching candidate SHA, a rewritten generation manifest bound to
    those bytes, a rewritten active pointer bound to that manifest, rewritten
    terminal journal fields and a recomputed VALID ``journal_sha256``; the
    bootstrap marker is restored.  The PUBLIC production reader
    (``reconcile_attempt_terminal_truth``) must pass every strict-journal /
    pointer / manifest / bytes-hash equality, reach the fresh REAL-runtime reopen
    and fail THERE ONLY — preserving the marker and every forged byte (zero
    clear, zero mutation).  Kills a mutant that removes the fresh validation in
    completed-journal resume."""

    from okto_pulse.community.adapters.global_discovery_layout import (
        canonical_sha256 as _canon,
    )
    from okto_pulse.community.adapters.global_discovery_layout import (
        write_generation_manifest,
    )
    from okto_pulse.community.adapters.global_discovery_recovery import (
        _journal_binding,
        _snapshot,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"partial-primary")
    live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
    write_bootstrap_marker(live)

    run_id = "gdr_r8b7forge"
    artifact_store = CommunityFileSystemRebuildAuditArtifactStore(
        tmp_path / "artifacts"
    )
    engine = _make_engine(tmp_path / f"{run_id}.sqlite3")
    recording_port = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    boards = _two_real_seeds()
    ordered = tuple(sorted(boards, key=lambda b: b.board_id))
    counts = RecoveryProgressCounts(
        sources_total=1, sources_processed=1, nodes_written=2, edges_written=1
    )
    live_sha = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
    ).inspect_live_artifact().sha256
    _seed_epoch_inputs(
        artifact_store, run_id, 1, live_sha=live_sha, boards=boards, counts=counts
    )

    # --- A REAL completed seed rebuild through the production composition. ---
    with _r6_env(recording_port, tmp_path / "kgbase"):
        bundle, _gr = _build_bundle(
            engine=engine, artifact_store=artifact_store, live=live,
            factory=None, run_id=run_id,
        )
        try:
            _admit_and_start(
                bundle, prepared_recovery_admitter,
                _command(run_id, started_at=datetime.now(timezone.utc)),
            )
            terminal = _wait_until(
                bundle.control, run_id=run_id,
                predicate=lambda s: s.state is RecoveryRunState.SUCCESS,
                timeout_seconds=60.0,
            )
        finally:
            bundle.close()
    assert terminal.state is RecoveryRunState.SUCCESS
    epoch = terminal.epoch
    attempt_id = recovery_attempt_id(run_id, epoch)
    journal_path = (
        live.parent / "quarantine" / "global-discovery"
        / attempt_id / "recovery_journal.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["kind"] == "seed_rebuild"
    active = read_active_generation(live)
    assert active is not None
    generation_id = active.generation_id

    # --- FORGE a fully self-consistent physical state over arbitrary bytes. ---
    active.graph_path.write_bytes(
        b"forged-arbitrary-active-bytes-not-a-ladybug-graph"
    )
    forged_wal = active.graph_path.with_name(active.graph_path.name + ".wal")
    if forged_wal.exists():
        forged_wal.write_bytes(b"")
    forged_snapshot = _snapshot(active.graph_path)
    forged_sha = forged_snapshot.sha256
    manifest_sha, _fs = write_generation_manifest(
        live, generation_id,
        {
            "run_id": run_id,
            "epoch": epoch,
            "attempt_id": attempt_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "graph_filename": live.name,
            "artifact_sha256_at_cutover": forged_sha,
            "artifact_count_at_cutover": forged_snapshot.artifact_count,
            "artifact_bytes_at_cutover": forged_snapshot.total_bytes,
            "source_fingerprint": journal["source_fingerprint"],
            "semantic_fingerprint": journal["semantic_fingerprint"],
            "schema_object_count": journal["schema_object_count"],
            "counts_by_board": journal["counts_by_board"],
        },
    )
    pointer_path = live.parent / "active_generation.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer.pop("pointer_sha256", None)
    pointer["manifest_sha256"] = manifest_sha
    pointer = {**pointer, "pointer_sha256": _canon(pointer)}
    pointer_path.write_text(
        json.dumps(pointer, sort_keys=True), encoding="utf-8"
    )
    forged_journal = {
        **journal,
        "candidate_sha256": forged_sha,
        "generation_manifest_sha256": manifest_sha,
        "clear_settled": False,
    }
    binding = _journal_binding(forged_journal)
    forged_journal = {**binding, "journal_sha256": _canon(binding)}
    journal_path.write_text(
        json.dumps(forged_journal, sort_keys=True, indent=2), encoding="utf-8"
    )
    write_bootstrap_marker(live)
    assert bootstrap_marker_present(live) is True

    forged_journal_bytes = journal_path.read_bytes()
    forged_pointer_bytes = pointer_path.read_bytes()
    manifest_path = (
        live.parent / "discovery.generations" / generation_id
        / "generation_manifest.json"
    )
    forged_manifest_bytes = manifest_path.read_bytes()
    forged_graph_bytes = active.graph_path.read_bytes()
    marker_path = live.parent / "discovery_bootstrap_incomplete.json"
    forged_marker_bytes = marker_path.read_bytes()
    forged_raw_map = _raw_active_state(live)
    quarantine_tree_before = _quarantine_tree(live)

    # R8-B7.4: observation seam WITHOUT swapping the default factory — record
    # effective entry into the REAL fresh reopen (class-level wrap that
    # DELEGATES; the production factory and runtime stay untouched).
    reopen_probe: dict[str, object] = {}
    real_list_schema = CommunityGlobalDiscoveryRuntime.list_schema_objects

    def observing_list_schema(self):
        reopen_probe["entered"] = True
        return real_list_schema(self)

    monkeypatch.setattr(
        CommunityGlobalDiscoveryRuntime,
        "list_schema_objects",
        observing_list_schema,
    )

    # --- PUBLIC production reader: must die ONLY on the fresh REAL reopen. ---
    validator = CommunityGlobalDiscoveryRecovery(
        global_runtime=CommunityGlobalDiscoveryRuntime(
            graph_path_provider=lambda: live
        ),
        graph_path_provider=lambda: live,
    )
    with _r6_env(
        _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
        tmp_path / "kgbase",
    ):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:forgery-probe",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            with pytest.raises(Exception) as exc_info:
                validator.reconcile_attempt_terminal_truth(
                    run_id=run_id, epoch=epoch, attempt_id=attempt_id,
                    expected_live_sha256=live_sha, boards=ordered,
                    fence_check=lease.assert_fenced,
                )
    # It got PAST the strict journal validator and every pointer/manifest/bytes
    # equality (all self-consistent) and died at the fresh real-runtime reopen.
    message = str(exc_info.value)
    assert "journal_invalid" not in message
    assert "evidence_missing" not in message
    assert "generation_mismatch" not in message
    assert "manifest_mismatch" not in message
    assert "candidate_sha_mismatch" not in message
    assert "post_close_mismatch" not in message
    assert "resume_source_drift" not in message
    assert "phase_invalid" not in message
    # R8-B7.4: EFFECTIVE entry into the real fresh reopen was observed, and the
    # raised error is the REAL storage open failure (found in the causal chain),
    # not an unrelated pre-readback error.
    chain_texts: list[str] = []
    cursor: BaseException | None = exc_info.value
    seen_exc: set[int] = set()
    while cursor is not None and id(cursor) not in seen_exc:
        seen_exc.add(id(cursor))
        chain_texts.append(f"{type(cursor).__name__}: {cursor}")
        cursor = cursor.__cause__ or cursor.__context__
    chain_blob = " | ".join(chain_texts).lower()
    assert reopen_probe.get("entered") is True, chain_blob
    assert (
        "unable to open" in chain_blob
        or "failed to open" in chain_blob
        or "not a valid" in chain_blob
    ), chain_blob

    # Marker preserved BYTE-FOR-BYTE, zero clear, every forged byte unchanged —
    # including the COMPLETE raw active-artifact map (primary+WAL/sidecars).
    assert bootstrap_marker_present(live) is True
    assert marker_path.read_bytes() == forged_marker_bytes
    assert journal_path.read_bytes() == forged_journal_bytes
    assert pointer_path.read_bytes() == forged_pointer_bytes
    assert manifest_path.read_bytes() == forged_manifest_bytes
    assert active.graph_path.read_bytes() == forged_graph_bytes
    assert _raw_active_state(live) == forged_raw_map
    # R8-B7.8 (#5/d): the ONLY residue is the reader's OWN orphan scratch —
    # the exceptional path performs zero unfenced mutation, so the scratch is
    # left for the next owner's fenced entry sweep.  No atomic-write temp files
    # exist and the rest of the quarantine tree is byte-identical.
    orphans = list(journal_path.parent.glob("resume-validate-scratch*"))
    assert len(orphans) == 1
    assert not list(live.parent.rglob("*.tmp"))
    assert _quarantine_tree(live) == _tree_plus_orphans(
        quarantine_tree_before, live, orphans
    )
    # A SECOND public read sweeps the old orphan under its fence, dies again at
    # the fresh reopen, and leaves exactly ITS one orphan — deterministic, no
    # accumulation, all forged bytes still frozen.
    with _r6_env(
        _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
        tmp_path / "kgbase",
    ):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:forgery-probe-2",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease2:
            with pytest.raises(Exception):
                validator.reconcile_attempt_terminal_truth(
                    run_id=run_id, epoch=epoch, attempt_id=attempt_id,
                    expected_live_sha256=live_sha, boards=ordered,
                    fence_check=lease2.assert_fenced,
                )
    orphans_after = list(journal_path.parent.glob("resume-validate-scratch*"))
    assert len(orphans_after) == 1
    assert orphans_after[0] != orphans[0]
    assert bootstrap_marker_present(live) is True
    assert journal_path.read_bytes() == forged_journal_bytes
    assert _quarantine_tree(live) == _tree_plus_orphans(
        quarantine_tree_before, live, orphans_after
    )


# --- R8 B7.5 #1: unknown/malformed resume phase fails closed, zero mutation ---


def test_r8_b75_unknown_phase_fails_closed_zero_mutation(tmp_path):
    """R8-B7.5 (#1): a valid SELF-HASHED seed journal whose phase is unknown/
    malformed must fail closed in the PUBLIC rebuild resume BEFORE any physical
    mutation — typed ``journal_phase_invalid`` error, marker preserved
    byte-for-byte, zero clear, zero journal write, zero candidate work and ZERO
    runtime construction (a must-not-construct factory proves it)."""

    from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
        bootstrap_marker_present,
    )
    from okto_pulse.community.adapters.global_discovery_layout import (
        canonical_sha256 as _canon,
    )
    from okto_pulse.community.adapters.global_discovery_recovery import (
        _journal_binding,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"partial-primary")
    live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
    write_bootstrap_marker(live)

    run_id = "gdr_r8b75phase"
    attempt1 = recovery_attempt_id(run_id, 1)
    boards = (_seed(),)
    ordered = tuple(sorted(boards, key=lambda b: b.board_id))

    def must_not_construct(path):
        raise AssertionError(f"runtime constructed for {path}")

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=must_not_construct,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    live_sha = adapter.inspect_live_artifact().sha256
    source_fp = _canon([b.to_dict() for b in ordered])
    expected_sem = _canon(adapter._expected_semantic_projection(ordered))
    q_dir = live.parent / "quarantine" / "global-discovery" / attempt1
    q_dir.mkdir(parents=True)
    rogue = {
        "run_id": run_id,
        "epoch": 1,
        "attempt_id": attempt1,
        "kind": "seed_rebuild",
        "phase": "mystery_phase",
        "source_fingerprint": source_fp,
        "expected_semantic_fingerprint": expected_sem,
        "quarantine_ref": f"community-global-discovery-quarantine:{attempt1}",
    }
    binding = _journal_binding(rogue)
    rogue = {**binding, "journal_sha256": _canon(binding)}
    rogue_path = q_dir / "recovery_journal.json"
    rogue_path.write_text(
        json.dumps(rogue, sort_keys=True, indent=2), encoding="utf-8"
    )

    marker_path = live.parent / "discovery_bootstrap_incomplete.json"
    before_marker = marker_path.read_bytes()
    before_journal = rogue_path.read_bytes()
    before_raw = _raw_active_state(live)

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc_info:
        adapter.rebuild_candidate_and_cutover(
            run_id=run_id, epoch=1, attempt_id=attempt1,
            expected_live_sha256=live_sha, boards=boards,
            fence_check=lambda: None,
        )
    assert "journal_phase_invalid" in str(exc_info.value)
    assert bootstrap_marker_present(live) is True
    assert marker_path.read_bytes() == before_marker
    assert rogue_path.read_bytes() == before_journal
    assert _raw_active_state(live) == before_raw


# --- R8 B7.5 #2: self-hashed forged SEED source rejected on public read -------


@pytest.mark.parametrize(
    "forgery,code",
    [
        ("source_fingerprint", "source_fingerprint_binding"),
        ("semantic_fingerprint", "source_semantic_binding"),
    ],
)
def test_r8_b75_forged_seed_source_rejected_on_read(tmp_path, forgery, code):
    """R8-B7.5 (#2): a SELF-HASHED forged SEED source journal — with the own
    journal re-bound to its exact raw bytes (recomputed predecessor SHA + valid
    own journal_sha256) — is rejected by the PUBLIC read path through the
    centralized strict seed validation + fingerprint binding, with zero
    mutation of any byte."""

    import hashlib as _hl

    from okto_pulse.community.adapters.global_discovery_layout import (
        canonical_sha256 as _canon,
    )
    from okto_pulse.community.adapters.global_discovery_recovery import (
        _journal_binding,
    )

    fixture = _b8r2_direct_heal(tmp_path)
    adapter = fixture["adapter"]
    boards = fixture["boards"]
    ordered = tuple(sorted(boards, key=lambda b: b.board_id))
    source = json.loads(fixture["source_path"].read_text(encoding="utf-8"))
    own = json.loads(fixture["own_path"].read_text(encoding="utf-8"))

    good_source_fp = _canon([b.to_dict() for b in ordered])
    good_expected_sem = _canon(adapter._expected_semantic_projection(ordered))
    forged_source = {
        **source,
        "kind": "seed_rebuild",
        "source_fingerprint": (
            "c" * 64 if forgery == "source_fingerprint" else good_source_fp
        ),
        "expected_semantic_fingerprint": good_expected_sem,
    }
    if forgery == "semantic_fingerprint":
        forged_source["semantic_fingerprint"] = "d" * 64
    src_binding = _journal_binding(forged_source)
    forged_source = {**src_binding, "journal_sha256": _canon(src_binding)}
    fixture["source_path"].write_text(
        json.dumps(forged_source, sort_keys=True, indent=2), encoding="utf-8"
    )
    source_bytes = fixture["source_path"].read_bytes()
    own = {
        **own,
        "predecessor_journal_sha256": _hl.sha256(source_bytes).hexdigest(),
    }
    own_binding = _journal_binding(own)
    own = {**own_binding, "journal_sha256": _canon(own_binding)}
    fixture["own_path"].write_text(
        json.dumps(own, sort_keys=True, indent=2), encoding="utf-8"
    )

    before_source = fixture["source_path"].read_bytes()
    before_own = fixture["own_path"].read_bytes()
    before_raw = _raw_active_state(fixture["live"])

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc_info:
        adapter.reconcile_attempt_terminal_truth(
            run_id=fixture["run_id"], epoch=2, attempt_id=fixture["attempt2"],
            expected_live_sha256=fixture["live_sha"], boards=boards,
            fence_check=lambda: None,
        )
    assert code in str(exc_info.value)
    assert fixture["source_path"].read_bytes() == before_source
    assert fixture["own_path"].read_bytes() == before_own
    assert _raw_active_state(fixture["live"]) == before_raw


# --- R8 B7.6 #2: legacy-mode completed resume converges through public read ---


def test_r8_b76_legacy_completed_resume_converges(tmp_path):
    """R8-B7.6 (#2): a LEGACY-mode public rebuild (NO supplied attempt id — the
    generation is named by the bare run_id) that completed must RESUME through
    the SAME public reader with the marker restored: the strict seed validator
    binds the CALLER-mode expected generation (legacy run_id), the non-mutating
    scratch-copy validation converges, the marker clears, and a second resume is
    a pure idempotent no-op."""

    from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
        bootstrap_marker_present,
    )
    from okto_pulse.community.adapters.global_discovery_recovery import (
        _snapshot,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"partial-primary")
    live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
    write_bootstrap_marker(live)

    run_id = "gdr_r8b76legacy"
    attempt1 = recovery_attempt_id(run_id, 1)
    boards = _two_real_seeds()
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=CommunityGlobalDiscoveryRuntime(
            graph_path_provider=lambda: live
        ),
        graph_path_provider=lambda: live,
    )
    live_sha = adapter.inspect_live_artifact().sha256

    # LEGACY public rebuild: attempt_id intentionally NOT supplied.
    with _r6_env(
        _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
        tmp_path / "kgbase",
    ):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:legacy-build",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            built = adapter.recover_and_cutover(
                run_id=run_id,
                expected_live_sha256=live_sha,
                boards=boards,
                fence_check=lease.assert_fenced,
            )
    assert built.outcome == "completed"
    # LEGACY naming end-to-end: the quarantine directory is keyed by the bare
    # run_id (no attempt segment), exactly like the generation.
    journal_path = (
        live.parent / "quarantine" / "global-discovery"
        / run_id / "recovery_journal.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["kind"] == "seed_rebuild"
    assert journal["generation_id"] == run_id  # LEGACY naming: bare run_id
    active = read_active_generation(live)
    assert active is not None
    assert active.generation_id == run_id

    # R8-B7.7 (#1): crash-before-clear resume in a brand-new COLD process —
    # must converge (success + cleared marker) with the raw surface intact.
    import shutil as _shutil

    import okto_pulse.community.adapters.global_discovery_recovery as gdr_mod

    write_bootstrap_marker(live)
    raw_before_cold = _raw_active_state(live)
    cold = _run_cold_resume(
        tmp_path, mode="legacy", live=live, kg_base=tmp_path / "kgbase",
        run_id=run_id, epoch=1, attempt_id=attempt1, live_sha=live_sha,
    )
    assert cold["outcome"] == "completed"
    assert bootstrap_marker_present(live) is False
    assert _snapshot(active.graph_path).sha256 == journal["candidate_sha256"]
    assert _raw_active_state(live) == raw_before_cold

    # R8-B7.7 (#2a): factory-open failure INSIDE the cleanup scope fails closed
    # — the error surfaces, the marker is preserved, and no scratch remains.
    write_bootstrap_marker(live)
    legacy_q_dir = live.parent / "quarantine" / "global-discovery" / run_id

    def scratch_dirs():
        return sorted(legacy_q_dir.glob("resume-validate-scratch*"))

    marker_path_l = live.parent / "discovery_bootstrap_incomplete.json"
    pointer_path_l = live.parent / "active_generation.json"
    manifest_path_l = (
        live.parent / "discovery.generations" / run_id
        / "generation_manifest.json"
    )
    # R8-B7.8 (c): freeze the COMPLETE quarantine tree (dirs + bytes) plus
    # marker/journal/pointer/manifest/raw before each injection.
    inj_tree = _quarantine_tree(live)
    inj_marker = marker_path_l.read_bytes()
    inj_journal = journal_path.read_bytes()
    inj_pointer = pointer_path_l.read_bytes()
    inj_manifest = manifest_path_l.read_bytes()
    inj_raw = _raw_active_state(live)
    real_factory = adapter._runtime_factory

    def exploding_factory(path):
        raise RuntimeError("injected factory-open failure")

    adapter._runtime_factory = exploding_factory
    try:
        with _r6_env(
            _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
            tmp_path / "kgbase",
        ):
            with global_discovery_writer_scope(
                operation="global_discovery_recovery",
                owner_id=f"{run_id}:legacy-inject-factory",
                ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                admin_lane=True,
            ) as lease:
                with pytest.raises(RuntimeError, match="injected factory-open"):
                    adapter.recover_and_cutover(
                        run_id=run_id, expected_live_sha256=live_sha,
                        boards=boards, fence_check=lease.assert_fenced,
                    )
    finally:
        adapter._runtime_factory = real_factory
    assert bootstrap_marker_present(live) is True   # zero clear
    # R8-B7.8 (d): the exceptional path performs ZERO unfenced filesystem
    # mutation — the OWN scratch stays behind as an orphan for the next
    # owner's fenced entry sweep.
    orphans_2a = scratch_dirs()
    assert len(orphans_2a) == 1
    assert _quarantine_tree(live) == _tree_plus_orphans(
        inj_tree, live, orphans_2a
    )
    assert marker_path_l.read_bytes() == inj_marker
    assert journal_path.read_bytes() == inj_journal
    assert pointer_path_l.read_bytes() == inj_pointer
    assert manifest_path_l.read_bytes() == inj_manifest
    assert _raw_active_state(live) == inj_raw
    # Test-side orphan removal so the NEXT injection targets its own cleanup,
    # not the entry sweep.
    for orphan_dir in scratch_dirs():
        _shutil.rmtree(orphan_dir)
    assert not scratch_dirs()

    # R8-B7.7 (#2b): a FINAL-cleanup failure on the success path surfaces
    # BEFORE the marker clear — typed error, marker preserved, scratch remains
    # (permanent evidence) until the next entry removes it fail-closed.
    real_remove_tree_2b = gdr_mod._remove_tree_fenced
    fired = {"n": 0}

    def failing_remove_tree(root, *, fence_check):
        if "resume-validate-scratch" in str(root) and fired["n"] == 0:
            fired["n"] = 1
            raise PermissionError("injected cleanup failure")
        return real_remove_tree_2b(root, fence_check=fence_check)

    gdr_mod._remove_tree_fenced = failing_remove_tree
    try:
        with _r6_env(
            _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
            tmp_path / "kgbase",
        ):
            with global_discovery_writer_scope(
                operation="global_discovery_recovery",
                owner_id=f"{run_id}:legacy-inject-cleanup",
                ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                admin_lane=True,
            ) as lease:
                with pytest.raises(
                    CommunityGlobalDiscoveryRecoveryError,
                    match="scratch_cleanup_failed",
                ):
                    adapter.recover_and_cutover(
                        run_id=run_id, expected_live_sha256=live_sha,
                        boards=boards, fence_check=lease.assert_fenced,
                    )
    finally:
        gdr_mod._remove_tree_fenced = real_remove_tree_2b
    assert fired["n"] == 1
    assert bootstrap_marker_present(live) is True   # zero clear
    orphans_2b = scratch_dirs()                     # un-removable scratch stays
    assert len(orphans_2b) == 1
    assert _quarantine_tree(live) == _tree_plus_orphans(
        inj_tree, live, orphans_2b
    )
    assert marker_path_l.read_bytes() == inj_marker
    assert journal_path.read_bytes() == inj_journal
    assert pointer_path_l.read_bytes() == inj_pointer
    assert manifest_path_l.read_bytes() == inj_manifest
    assert _raw_active_state(live) == inj_raw

    # WARMED converge after the injections: the entry removes the orphan
    # scratch fail-closed and the resume converges.  The factory path spy
    # proves every runtime construction received the SCRATCH graph path and
    # NEVER the active graph path (#3).
    factory_paths: list[str] = []

    def path_spy_factory(path):
        factory_paths.append(str(path))
        return real_factory(path)

    adapter._runtime_factory = path_spy_factory
    try:
        with _r6_env(
            _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
            tmp_path / "kgbase",
        ):
            with global_discovery_writer_scope(
                operation="global_discovery_recovery",
                owner_id=f"{run_id}:legacy-resume-warm",
                ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                admin_lane=True,
            ) as lease:
                resumed = adapter.recover_and_cutover(
                    run_id=run_id, expected_live_sha256=live_sha,
                    boards=boards, fence_check=lease.assert_fenced,
                )
    finally:
        adapter._runtime_factory = real_factory
    assert resumed.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert not scratch_dirs()
    assert _snapshot(active.graph_path).sha256 == journal["candidate_sha256"]
    assert factory_paths, "the converge must construct a validation runtime"
    # R8-B7.8 (b): containment by Path.resolve(), never substring matching.
    active_resolved = active.graph_path.resolve()
    q_dir_resolved = legacy_q_dir.resolve()
    for constructed in factory_paths:
        constructed_path = Path(constructed).resolve()
        assert constructed_path != active_resolved
        assert constructed_path.name == active.graph_path.name
        assert constructed_path.parent.parent == q_dir_resolved
        assert constructed_path.parent.name.startswith(
            "resume-validate-scratch-"
        )

    # R8-B7.8 (d/#1): FENCE-LOSS mid-validation — the losing claimant performs
    # ZERO unfenced removal (its own scratch stays as an orphan) and the NEXT
    # owner's fenced entry sweep removes it and converges without interference.
    from okto_pulse.core.kg.global_discovery_writer import (
        GlobalDiscoveryWriterFenceLost,
    )

    write_bootstrap_marker(live)

    def losing_validate(runtime, boards_arg, *, fence_check=None):
        raise GlobalDiscoveryWriterFenceLost()

    adapter._validate_runtime = losing_validate
    try:
        with _r6_env(
            _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
            tmp_path / "kgbase",
        ):
            with global_discovery_writer_scope(
                operation="global_discovery_recovery",
                owner_id=f"{run_id}:legacy-inject-fence-loss",
                ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                admin_lane=True,
            ) as lease:
                with pytest.raises(Exception) as loss_info:
                    adapter.recover_and_cutover(
                        run_id=run_id, expected_live_sha256=live_sha,
                        boards=boards, fence_check=lease.assert_fenced,
                    )
    finally:
        del adapter._validate_runtime
    assert "fence" in str(loss_info.value).lower() or "writer" in str(
        loss_info.value
    ).lower()
    assert bootstrap_marker_present(live) is True   # zero clear
    assert scratch_dirs()                           # own orphan left behind
    # The NEXT owner (new fence) sweeps the orphan and converges.
    with _r6_env(
        _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
        tmp_path / "kgbase",
    ):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:legacy-after-loss",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            healed = adapter.recover_and_cutover(
                run_id=run_id, expected_live_sha256=live_sha,
                boards=boards, fence_check=lease.assert_fenced,
            )
    assert healed.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert not scratch_dirs()

    # R8-B7.9 (#1): REAL fence loss DURING the fenced removal itself — the
    # production per-entry revalidation must stop ALL mutation immediately.
    # (a) during the ORPHAN SWEEP; (b) during the OWN success-path cleanup.
    real_remove_tree = gdr_mod._remove_tree_fenced
    loss_port: dict[str, object] = {}

    def lease_dropping_remove(root, *, fence_check):
        # R8-B7.10: delegate to the REAL helper with a COUNTED fence.  Internal
        # check #1 (helper entry) passes; at internal check #2 (the per-entry
        # revalidation) the COMPLETE state is captured and the REAL lease is
        # released, so the ORIGINAL fence fails BEFORE the first unlink.  A
        # mutant helper with a single entry check never reaches #2: the removal
        # succeeds, "dropped" stays unset and the post asserts kill the mutant.
        def counting_fence():
            loss_port["checks"] = loss_port.get("checks", 0) + 1
            if loss_port["checks"] == 2 and not loss_port.get("dropped"):
                loss_port["dropped"] = True
                loss_port["captured_tree"] = _quarantine_tree(live)
                loss_port["captured_raw"] = _raw_active_state(live)
                loss_port["captured_marker"] = marker_path_l.read_bytes()
                loss_port["captured_journal"] = journal_path.read_bytes()
                loss_port["captured_pointer"] = pointer_path_l.read_bytes()
                loss_port["captured_manifest"] = manifest_path_l.read_bytes()
                token = _acquisition_for_owner(
                    loss_port["port"], loss_port["owner"]
                )[1].owner_token
                loss_port["port"].release_single_writer_sync(
                    board_id=GLOBAL_DISCOVERY_WRITER_SCOPE,
                    artifact_id=GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
                    owner_token=token,
                )
            return fence_check()

        return real_remove_tree(root, fence_check=counting_fence)

    # (a) SWEEP loss: pre-seed an orphan with real content; the sweep's fenced
    # removal loses the lease and stops with the orphan bytes INTACT.
    write_bootstrap_marker(live)
    seeded_orphan = legacy_q_dir / "resume-validate-scratch-preseeded"
    seeded_orphan.mkdir(parents=True)
    (seeded_orphan / "evidence.bin").write_bytes(b"orphan-evidence-bytes")
    sweep_tree = _quarantine_tree(live)
    sweep_port = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    loss_port.clear()
    loss_port["port"] = sweep_port
    loss_port["owner"] = f"{run_id}:legacy-loss-sweep"
    gdr_mod._remove_tree_fenced = lease_dropping_remove
    try:
        with _r6_env(sweep_port, tmp_path / "kgbase"):
            try:
                with global_discovery_writer_scope(
                    operation="global_discovery_recovery",
                    owner_id=f"{run_id}:legacy-loss-sweep",
                    ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                    admin_lane=True,
                ) as lease:
                    with pytest.raises(Exception) as sweep_loss_info:
                        adapter.recover_and_cutover(
                            run_id=run_id, expected_live_sha256=live_sha,
                            boards=boards, fence_check=lease.assert_fenced,
                        )
            except Exception:
                # The scope exit may itself complain: the lease is REALLY gone.
                pass
    finally:
        gdr_mod._remove_tree_fenced = real_remove_tree
    blob = str(sweep_loss_info.value).lower()
    assert "fence" in blob or "writer" in blob, blob
    # The INTERNAL revalidation (#2) really happened — a single-entry-check
    # mutant never reaches it and dies here.
    assert loss_port.get("dropped") is True
    assert loss_port["checks"] >= 2
    assert bootstrap_marker_present(live) is True       # zero clear
    assert seeded_orphan.exists()                       # zero unlinks happened
    assert (seeded_orphan / "evidence.bin").read_bytes() == (
        b"orphan-evidence-bytes"
    )
    # Byte-exact equality against the state captured AT internal check #2 —
    # complete quarantine tree (scratch included), marker, journal, pointer,
    # manifest and raw active surface.  No exclusions, no post-failure expected.
    assert _quarantine_tree(live) == loss_port["captured_tree"]
    assert _raw_active_state(live) == loss_port["captured_raw"]
    assert marker_path_l.read_bytes() == loss_port["captured_marker"]
    assert journal_path.read_bytes() == loss_port["captured_journal"]
    assert pointer_path_l.read_bytes() == loss_port["captured_pointer"]
    assert manifest_path_l.read_bytes() == loss_port["captured_manifest"]
    assert _quarantine_tree(live) == sweep_tree          # byte-identical tree
    # Recovery: the next owner sweeps and converges.
    with _r6_env(
        _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
        tmp_path / "kgbase",
    ):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:legacy-after-sweep-loss",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            healed_sweep = adapter.recover_and_cutover(
                run_id=run_id, expected_live_sha256=live_sha,
                boards=boards, fence_check=lease.assert_fenced,
            )
    assert healed_sweep.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert not scratch_dirs()

    # (b) OWN-CLEANUP loss: no orphans at entry, so the FIRST fenced removal is
    # the success-path cleanup of the own scratch — losing the lease there must
    # stop before ANY unlink (scratch intact) and never clear the marker.
    write_bootstrap_marker(live)
    cleanup_port = _RecordingWriteLockPort(CommunityLocalWriteLockPort())
    loss_port.clear()
    loss_port["port"] = cleanup_port
    loss_port["owner"] = f"{run_id}:legacy-loss-cleanup"
    gdr_mod._remove_tree_fenced = lease_dropping_remove
    try:
        with _r6_env(cleanup_port, tmp_path / "kgbase"):
            try:
                with global_discovery_writer_scope(
                    operation="global_discovery_recovery",
                    owner_id=f"{run_id}:legacy-loss-cleanup",
                    ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                    admin_lane=True,
                ) as lease:
                    with pytest.raises(Exception) as cleanup_loss_info:
                        adapter.recover_and_cutover(
                            run_id=run_id, expected_live_sha256=live_sha,
                            boards=boards, fence_check=lease.assert_fenced,
                        )
            except Exception:
                pass
    finally:
        gdr_mod._remove_tree_fenced = real_remove_tree
    blob = str(cleanup_loss_info.value).lower()
    assert "fence" in blob or "writer" in blob, blob
    assert loss_port.get("dropped") is True             # internal #2 happened
    assert loss_port["checks"] >= 2
    assert bootstrap_marker_present(live) is True       # zero clear
    own_orphans = scratch_dirs()
    assert len(own_orphans) == 1                        # own scratch intact
    assert (own_orphans[0] / live.name).exists()        # copied bytes intact
    # Byte-exact equality against the state captured AT internal check #2 —
    # the COMPLETE quarantine tree already contains the POPULATED own scratch.
    assert _quarantine_tree(live) == loss_port["captured_tree"]
    assert _raw_active_state(live) == loss_port["captured_raw"]
    assert marker_path_l.read_bytes() == loss_port["captured_marker"]
    assert journal_path.read_bytes() == loss_port["captured_journal"]
    assert pointer_path_l.read_bytes() == loss_port["captured_pointer"]
    assert manifest_path_l.read_bytes() == loss_port["captured_manifest"]
    with _r6_env(
        _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
        tmp_path / "kgbase",
    ):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:legacy-after-cleanup-loss",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            healed_cleanup = adapter.recover_and_cutover(
                run_id=run_id, expected_live_sha256=live_sha,
                boards=boards, fence_check=lease.assert_fenced,
            )
    assert healed_cleanup.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert not scratch_dirs()

    # R8-B7.8 (e/#2): DRIFT + cleanup-failure injected TOGETHER — the semantic
    # drift is the in-flight cause and can never be masked: the exceptional
    # path never even attempts a cleanup (wrapper must not fire).
    write_bootstrap_marker(live)

    def drifting_validate(runtime, boards_arg, *, fence_check=None):
        return 999, {"forged": {"boards": 1, "digests": 1, "links": 1}}, "0" * 64

    drift_fired = {"n": 0}
    real_remove_tree_drift = gdr_mod._remove_tree_fenced

    def drift_failing_remove_tree(root, *, fence_check):
        if "resume-validate-scratch" in str(root):
            drift_fired["n"] += 1
            raise PermissionError("injected cleanup failure during drift")
        return real_remove_tree_drift(root, fence_check=fence_check)

    # R8-B7.9 (#2): ALSO inject a BENIGN readback-close failure — with the
    # drift raised INSIDE the inner try (before the close), the benign close
    # error on the exceptional path is suppressed and can never mask the drift.
    close_fired = {"n": 0}
    real_runtime_close = CommunityGlobalDiscoveryRuntime.close

    def benign_failing_close(self):
        provider = getattr(self, "_graph_path_provider", None)
        target = str(provider()) if callable(provider) else ""
        if "resume-validate-scratch" in target and close_fired["n"] == 0:
            close_fired["n"] += 1
            raise RuntimeError("injected benign close failure")
        return real_runtime_close(self)

    adapter._validate_runtime = drifting_validate
    gdr_mod._remove_tree_fenced = drift_failing_remove_tree
    CommunityGlobalDiscoveryRuntime.close = benign_failing_close
    try:
        with _r6_env(
            _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
            tmp_path / "kgbase",
        ):
            with global_discovery_writer_scope(
                operation="global_discovery_recovery",
                owner_id=f"{run_id}:legacy-inject-drift",
                ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                admin_lane=True,
            ) as lease:
                with pytest.raises(
                    CommunityGlobalDiscoveryRecoveryError,
                    match="semantic_drift",
                ):
                    adapter.recover_and_cutover(
                        run_id=run_id, expected_live_sha256=live_sha,
                        boards=boards, fence_check=lease.assert_fenced,
                    )
    finally:
        del adapter._validate_runtime
        gdr_mod._remove_tree_fenced = real_remove_tree_drift
        CommunityGlobalDiscoveryRuntime.close = real_runtime_close
    assert drift_fired["n"] == 0    # exceptional path never attempts cleanup
    assert close_fired["n"] == 1    # the benign close failure DID fire...
    assert bootstrap_marker_present(live) is True   # ...and did not mask drift
    assert scratch_dirs()                           # own orphan left behind
    with _r6_env(
        _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
        tmp_path / "kgbase",
    ):
        with global_discovery_writer_scope(
            operation="global_discovery_recovery",
            owner_id=f"{run_id}:legacy-after-drift",
            ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
            admin_lane=True,
        ) as lease:
            healed2 = adapter.recover_and_cutover(
                run_id=run_id, expected_live_sha256=live_sha,
                boards=boards, fence_check=lease.assert_fenced,
            )
    assert healed2.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert not scratch_dirs()

    # R8-B7.7 (#4): idempotent WARMED second resume — full freeze of journal /
    # pointer / manifest / raw map, ZERO factory constructions, ZERO artifact
    # fsyncs, and the re-read active generation identity unchanged.
    frozen_journal = journal_path.read_bytes()
    frozen_pointer = pointer_path_l.read_bytes()
    frozen_manifest = manifest_path_l.read_bytes()
    frozen_raw = _raw_active_state(live)
    noop_factory_calls: list[str] = []

    def noop_spy_factory(path):
        noop_factory_calls.append(str(path))
        return real_factory(path)

    fsync_calls: list[str] = []
    real_fsync_artifacts = gdr_mod._fsync_artifacts

    def spying_fsync(path, *args, **kwargs):
        fsync_calls.append(str(path))
        return real_fsync_artifacts(path, *args, **kwargs)

    # R8-B7.8 (a): zero journal writes / directory fsyncs / cutover notes /
    # marker clears during the no-op.
    writer_calls: list[str] = []
    real_writer_noop = gdr_mod._write_journal_with_directory_fsync

    def spying_writer(path, payload, **kwargs):
        writer_calls.append(str(path))
        return real_writer_noop(path, payload, **kwargs)

    fsync_dir_calls: list[str] = []
    real_fsync_dir = gdr_mod.fsync_directory

    def spying_fsync_dir(path, *args, **kwargs):
        fsync_dir_calls.append(str(path))
        return real_fsync_dir(path, *args, **kwargs)

    cutover_notes: list[str] = []
    real_note = adapter._global_runtime.note_successful_generation_cutover

    def spying_note(*args, **kwargs):
        cutover_notes.append("note")
        return real_note(*args, **kwargs)

    clear_calls: list[str] = []
    real_clear = adapter._clear_marker_crash_conservatively

    def spying_clear(*args, **kwargs):
        clear_calls.append("clear")
        return real_clear(*args, **kwargs)

    adapter._runtime_factory = noop_spy_factory
    gdr_mod._fsync_artifacts = spying_fsync
    gdr_mod._write_journal_with_directory_fsync = spying_writer
    gdr_mod.fsync_directory = spying_fsync_dir
    adapter._global_runtime.note_successful_generation_cutover = spying_note
    adapter._clear_marker_crash_conservatively = spying_clear
    try:
        with _r6_env(
            _RecordingWriteLockPort(CommunityLocalWriteLockPort()),
            tmp_path / "kgbase",
        ):
            with global_discovery_writer_scope(
                operation="global_discovery_recovery",
                owner_id=f"{run_id}:legacy-resume-noop",
                ttl_seconds=_RECOVERY_WRITER_LEASE_SECONDS,
                admin_lane=True,
            ) as lease:
                again = adapter.recover_and_cutover(
                    run_id=run_id, expected_live_sha256=live_sha,
                    boards=boards, fence_check=lease.assert_fenced,
                )
    finally:
        adapter._runtime_factory = real_factory
        gdr_mod._fsync_artifacts = real_fsync_artifacts
        gdr_mod._write_journal_with_directory_fsync = real_writer_noop
        gdr_mod.fsync_directory = real_fsync_dir
        adapter._global_runtime.note_successful_generation_cutover = real_note
        del adapter._clear_marker_crash_conservatively
    assert again.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert noop_factory_calls == []
    assert fsync_calls == []
    assert writer_calls == []
    assert fsync_dir_calls == []
    assert cutover_notes == []
    assert clear_calls == []
    assert journal_path.read_bytes() == frozen_journal
    assert pointer_path_l.read_bytes() == frozen_pointer
    assert manifest_path_l.read_bytes() == frozen_manifest
    assert _raw_active_state(live) == frozen_raw
    reread_active = read_active_generation(live)
    assert reread_active is not None
    assert reread_active.generation_id == run_id
    assert _snapshot(reread_active.graph_path).sha256 == (
        journal["candidate_sha256"]
    )
