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
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    CommunityRecoveryWorker,
    RecoveryDispatchStage,
    RecoveryPendingAncestryError,
    _RECOVERY_WRITER_LEASE_SECONDS,
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
    RecoveryPreparationCommand,
    RecoveryPreparedResult,
    RecoveryProgressCounts,
    RecoveryRunBinding,
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
)
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryDigestSeed,
)


def _seed():
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
        self.path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
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


# --- cancellation / deadline: fence raises before the physical operation ------


# --- stale dispatch claim: a stolen SQL claim must PREVENT physical cutover ---


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
    store_a = recovery_store_factory(
        f"sqlite:///{(tmp_path / (run_a + '.sqlite3')).as_posix()}"
    )
    claim_a = _b61_claimed_running_recovery(store_a, run_a)
    store_a.request_cancel(
        run_id=run_a,
        expected_epoch=claim_a.epoch,
        requested_at=_B61_NOW + timedelta(seconds=40),
        requested_by_actor_id="operator-late-cancel",
        reason="late cancel",
    )
    cur_a = store_a.get_status(run_id=run_a)
    assert cur_a.cancel_requested_at is not None
    term_a = store_a.complete_recovery(
        dispatch_id=claim_a.dispatch_id,
        claim_token=claim_a.claim_token,
        expected_progress_seq=cur_a.progress_seq,
        completed_at=_B61_NOW + timedelta(seconds=41),
        active_elapsed_ms=1_000,
        result=_b61_pending_sentinel(cur_a.counts),
    )
    assert term_a.state is RecoveryRunState.PARTIAL
    assert term_a.terminal_outcome is RecoveryTerminalOutcome.PARTIAL
    assert term_a.reason_code == "recovery_physical_reconciliation_pending"
    assert term_a.physical_truth is None
    assert term_a.cancel_requested_at is not None  # cancel recorded, truth preserved

    # NEGATIVE (layer 2): a non-sentinel no-truth PARTIAL is still rewritten.
    run_n = "gdr_b61_cancel_neg"
    store_n = recovery_store_factory(
        f"sqlite:///{(tmp_path / (run_n + '.sqlite3')).as_posix()}"
    )
    claim_n = _b61_claimed_running_recovery(store_n, run_n)
    store_n.request_cancel(
        run_id=run_n,
        expected_epoch=claim_n.epoch,
        requested_at=_B61_NOW + timedelta(seconds=40),
        requested_by_actor_id="operator-late-cancel",
        reason="late cancel",
    )
    cur_n = store_n.get_status(run_id=run_n)
    term_n = store_n.complete_recovery(
        dispatch_id=claim_n.dispatch_id,
        claim_token=claim_n.claim_token,
        expected_progress_seq=cur_n.progress_seq,
        completed_at=_B61_NOW + timedelta(seconds=41),
        active_elapsed_ms=1_000,
        result=RecoveryWorkerResult(
            outcome=RecoveryTerminalOutcome.PARTIAL,
            reason_code="global_discovery_recovery_rolled_back",
            retryable=False,
            counts=cur_n.counts,
            physical_truth=None,
        ),
    )
    assert term_n.state is RecoveryRunState.CANCELLED

    # WORKER layer (layer 1 -> chains into layer 2): worker._complete preserves it.
    run_w = "gdr_b61_cancel_worker"
    store_w = recovery_store_factory(
        f"sqlite:///{(tmp_path / (run_w + '.sqlite3')).as_posix()}"
    )
    claim_w = _b61_claimed_running_recovery(store_w, run_w)
    store_w.request_cancel(
        run_id=run_w,
        expected_epoch=claim_w.epoch,
        requested_at=_B61_NOW + timedelta(seconds=40),
        requested_by_actor_id="operator-late-cancel",
        reason="late cancel",
    )
    cur_w = store_w.get_status(run_id=run_w)
    worker = CommunityRecoveryWorker(
        store=store_w,
        native_operation=lambda **_k: None,
        heartbeat_interval_ms=5_000,
        wall_clock=lambda: _B61_NOW + timedelta(seconds=41),
        monotonic_clock=lambda: 1_000.0,
    )
    try:
        worker._complete(
            run_id=run_w,
            attempt_id=claim_w.attempt_id,
            epoch=claim_w.epoch,
            started_monotonic=1_000.0,
            baseline_elapsed_ms=0,
            result=_b61_pending_sentinel(cur_w.counts),
            dispatch_id=claim_w.dispatch_id,
            claim_token=claim_w.claim_token,
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
    store_a = recovery_store_factory(
        f"sqlite:///{(tmp_path / (run_a + '.sqlite3')).as_posix()}"
    )
    claim_a = _b61_claimed_running_recovery(store_a, run_a, budget_ms=budget_ms)
    cur_a = store_a.get_status(run_id=run_a)
    term_a = store_a.complete_recovery(
        dispatch_id=claim_a.dispatch_id,
        claim_token=claim_a.claim_token,
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
    store_n = recovery_store_factory(
        f"sqlite:///{(tmp_path / (run_n + '.sqlite3')).as_posix()}"
    )
    claim_n = _b61_claimed_running_recovery(store_n, run_n, budget_ms=budget_ms)
    cur_n = store_n.get_status(run_id=run_n)
    term_n = store_n.complete_recovery(
        dispatch_id=claim_n.dispatch_id,
        claim_token=claim_n.claim_token,
        expected_progress_seq=cur_n.progress_seq,
        completed_at=_B61_NOW + timedelta(seconds=63),
        active_elapsed_ms=budget_ms,
        result=RecoveryWorkerResult(
            outcome=RecoveryTerminalOutcome.PARTIAL,
            reason_code="global_discovery_recovery_rolled_back",
            retryable=False,
            counts=cur_n.counts,
            physical_truth=None,
        ),
    )
    assert term_n.state is RecoveryRunState.TIMEOUT

    # WORKER layer (layer 1 -> chains into layer 2): worker._complete preserves it.
    run_w = "gdr_b61_deadline_worker"
    store_w = recovery_store_factory(
        f"sqlite:///{(tmp_path / (run_w + '.sqlite3')).as_posix()}"
    )
    claim_w = _b61_claimed_running_recovery(store_w, run_w, budget_ms=budget_ms)
    cur_w = store_w.get_status(run_id=run_w)
    worker = CommunityRecoveryWorker(
        store=store_w,
        native_operation=lambda **_k: None,
        heartbeat_interval_ms=5_000,
        wall_clock=lambda: _B61_NOW + timedelta(seconds=63),
        monotonic_clock=lambda: 2_000.0,
    )
    try:
        worker._complete(
            run_id=run_w,
            attempt_id=claim_w.attempt_id,
            epoch=claim_w.epoch,
            started_monotonic=1_000.0,
            baseline_elapsed_ms=0,
            result=_b61_pending_sentinel(cur_w.counts),
            dispatch_id=claim_w.dispatch_id,
            claim_token=claim_w.claim_token,
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


# --- B6: writer-fence loss after pointer-cross -> PARTIAL, then N+1 reconciles -


# --- B6.4/B6.6: pending-ancestry walk heals after a mid-chain reconciler crash -


def _invalidate_after_cross_wrap(recording_port, run_id, attempt, live, flag):
    """A fence wrapper that, once the active pointer has crossed, invalidates the
    REAL external writer ownership for ``attempt`` exactly once — producing a
    genuine GlobalDiscoveryWriterFenceLost at the next fence step."""

    def fence_wrap(original):
        def wrapped():
            if not flag["done"] and read_active_generation(live) is not None:
                flag["done"] = True
                token = _acquisition_for_owner(recording_port, f"{run_id}:{attempt}")[
                    1
                ].owner_token
                recording_port.release_single_writer_sync(
                    board_id=GLOBAL_DISCOVERY_WRITER_SCOPE,
                    artifact_id=GLOBAL_DISCOVERY_WRITER_ARTIFACT_ID,
                    owner_token=token,
                )
            return original()

        return wrapped

    return fence_wrap


# --- B8/R8 B6.1: fail-closed pending-ancestry walk (stop at valid boundary) ----


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
                        epoch=2,
                        superseded_by_epoch=99,
                        supersedes_epoch=1,
                        attempt_id=recovery_attempt_id(run_id, 2),
                        state=RecoveryRunState.PARTIAL,
                        terminal_outcome=RecoveryTerminalOutcome.PARTIAL,
                        reason_code=_PENDING,
                        retryable=False,
                        physical_truth=None,
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
            epoch=epoch,
            superseded_by_epoch=superseded_by,
            supersedes_epoch=supersedes,
            attempt_id=recovery_attempt_id(run_id, epoch),
            state=RecoveryRunState.PARTIAL,
            terminal_outcome=RecoveryTerminalOutcome.PARTIAL,
            reason_code=reason,
            retryable=retryable,
            physical_truth=None,
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


# --- R8 B6.2: deep provenance/physical-truth validation on every read ----------


# --- R8 B6.2 #3: self-consistent forged set dies on fresh semantic reopen ------


# --- epoch-2 resume: real durable handoff across a runtime restart ------------


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


_COLD_RESUME_SCRIPT = r"""
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
"""


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
            sys.executable,
            str(script_path),
            str(tests_dir),
            mode,
            str(live),
            str(kg_base),
            run_id,
            str(epoch),
            attempt_id,
            live_sha,
            str(community_src),
            str(core_src),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=child_env,
        cwd=str(tmp_path),
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
    lines = [row for row in proc.stdout.splitlines() if row.startswith("COLD-RESULT:")]
    assert lines, (proc.stdout[-2000:], proc.stderr[-2000:])
    payload = json.loads(lines[-1][len("COLD-RESULT:") :])
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


# --- R8 B6#4: completed-first own-journal crash floor through worker/store x2 --


# --- R8 B7.4: fabricated self-consistent seed truth dies ONLY on the fresh ----
# --- REAL Ladybug reopen, through the PUBLIC production reader ----------------


# --- R8 B7.5 #1: unknown/malformed resume phase fails closed, zero mutation ---


# --- R8 B7.5 #2: self-hashed forged SEED source rejected on public read -------


# --- R8 B7.6 #2: legacy-mode completed resume converges through public read ---
