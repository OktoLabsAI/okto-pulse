from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters import (
    grafx_graph_lifecycle as lifecycle_module,
)
from okto_pulse.community.adapters import (
    grafx_graph_runtime_store as runtime_module,
)
from okto_pulse.community.adapters import (
    grafx_graph_schema_manager as schema_module,
)
from okto_pulse.community.adapters.filesystem_erasure import is_filesystem_alias
from okto_pulse.community.adapters.grafx_graph_lifecycle import (
    CommunityGrafxGraphLifecycle,
)
from okto_pulse.community.adapters.grafx_graph_runtime_store import (
    CommunityGrafxGraphRuntimeStore,
)
from okto_pulse.community.adapters.grafx_graph_schema_manager import (
    CommunityGrafxGraphSchemaManager,
)
from okto_pulse.community.adapters.grafx_schema_bootstrap import _commit_statements
from okto_pulse.community.adapters.grafx_schema_evolution import (
    GrafxSchemaCandidateResult,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.graph_lifecycle import GraphLifecycle
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
    GraphRuntimeStore,
)
from okto_pulse.core.kg.interfaces.graph_schema_manager import GraphSchemaManager
from okto_pulse.core.kg.safe_write_lifecycle import (
    STEP_CHECKPOINT,
    STEP_CLOSE_REOPEN_PROBE,
    STEP_FLUSH,
    STEP_FSYNC,
)


class _Database:
    def __init__(self, page_size: int = 8192) -> None:
        self.identity = SimpleNamespace(page_size=page_size)
        self.closed = False
        self.events: list[str] = []

    def checkpoint(self) -> None:
        self.events.append("checkpoint")

    def flush(self) -> None:
        self.events.append("flush")


def _write_foundation_binding(
    board_root: Path,
    *,
    generation: str,
) -> Path:
    """Write the exact format persisted by the M6 Foundation binding store."""

    data_root = board_root.parents[1]
    physical_path = board_root / "grafx" / generation
    body = {
        "binding_format": "okto-pulse-community-graph-binding/1",
        "scope": "board",
        "scope_id": board_root.name,
        "backend": "grafx",
        "generation": generation,
        "physical_path": physical_path.relative_to(data_root).as_posix(),
        "page_size": 8192,
    }
    encoded = json.dumps(
        body,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    binding = board_root / "graph_backend_binding.json"
    binding.write_text(
        json.dumps(
            {**body, "binding_sha256": hashlib.sha256(encoded).hexdigest()},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return binding


def _foundation_bound_path(board_root: Path) -> Path:
    binding = json.loads(
        (board_root / "graph_backend_binding.json").read_text(encoding="utf-8")
    )
    data_root = board_root.parents[1]
    physical_path = data_root.joinpath(*binding["physical_path"].split("/"))
    if not physical_path.is_dir():
        raise RuntimeError("physical_database_missing")
    return physical_path


def _candidate() -> GrafxSchemaCandidateResult:
    return GrafxSchemaCandidateResult(
        source_schema_version="0.3.12",
        target_schema_version=PULSE_GRAFX_SCHEMA_MANIFEST.schema_version,
        source_schema_fingerprint="source-schema",
        target_schema_fingerprint="target-schema",
        source_snapshot_lsn=17,
        logical_data_fingerprint="logical-data",
        node_row_counts=(("Decision", 2),),
        relationship_row_counts=(("supersedes__Decision__Decision", 1),),
        candidate_database_uuid=b"candidate-uuid",
        changed=True,
    )


def test_grafx_operational_providers_satisfy_all_three_core_ports(tmp_path) -> None:
    database = _Database()

    def fence(_board_id, _phase) -> None:
        return None

    def close(_board_id) -> None:
        return None

    def path(_board_id):
        return tmp_path / "graph.grafx"

    assert isinstance(
        CommunityGrafxGraphSchemaManager(lambda _board_id: database, fence),
        GraphSchemaManager,
    )
    assert isinstance(
        CommunityGrafxGraphLifecycle(
            lambda _board_id: database,
            path,
            close,
            fence,
        ),
        GraphLifecycle,
    )
    assert isinstance(
        CommunityGrafxGraphRuntimeStore(
            path,
            close,
            fence,
            board_storage_root_resolver=lambda _board_id: (
                tmp_path / "boards" / "board-1"
            ),
        ),
        GraphRuntimeStore,
    )


def test_bootstrap_commit_fences_every_mutation_and_commit() -> None:
    events: list[str] = []

    class Transaction:
        active = True

        def execute(self, text, parameters) -> None:
            events.append(f"execute:{text}:{parameters['value']}")

        def commit(self):
            self.active = False
            events.append("commit")
            return SimpleNamespace(durable=True, wrote=True)

        def rollback(self) -> None:
            self.active = False
            events.append("rollback")

    database = SimpleNamespace(begin=lambda mode: Transaction())
    _commit_statements(
        database,
        (("one", {"value": 1}), ("two", {"value": 2})),
        revalidate_fence=lambda phase: events.append(f"fence:{phase}"),
    )

    assert events == [
        "fence:bootstrap",
        "execute:one:1",
        "fence:bootstrap",
        "execute:two:2",
        "fence:commit",
        "commit",
    ]


async def test_schema_manager_covers_bootstrap_version_and_validation(
    monkeypatch,
) -> None:
    database = _Database()
    resolutions: list[str] = []
    fences: list[tuple[str, str]] = []
    target = PULSE_GRAFX_SCHEMA_MANIFEST.schema_version

    def resolve(board_id: str):
        resolutions.append(board_id)
        return database

    def ensure(_database, **kwargs):
        kwargs["revalidate_fence"]("bootstrap")
        kwargs["revalidate_fence"]("commit")
        return SimpleNamespace(
            changed=True,
            logical_fingerprint="fingerprint",
        )

    monkeypatch.setattr(schema_module, "ensure_current_grafx_board_schema", ensure)
    monkeypatch.setattr(
        schema_module,
        "read_current_grafx_schema_version",
        lambda _database: target,
    )
    monkeypatch.setattr(
        schema_module,
        "validate_current_grafx_schema",
        lambda _database: "fingerprint",
    )
    manager = CommunityGrafxGraphSchemaManager(
        resolve,
        lambda board_id, phase: fences.append((board_id, phase)),
    )

    await manager.ensure_bootstrapped("board-1")
    assert await manager.current_version("board-1") == target
    validation = await manager.validate("board-1")
    migration = await manager.migrate("board-1")

    assert validation.valid is True
    assert validation.current_version == target
    assert migration["activated"] is False
    assert resolutions == ["board-1", "board-1", "board-1", "board-1"]
    assert ("board-1", "commit") in fences


async def test_schema_migrate_certifies_then_activates_candidate(monkeypatch) -> None:
    database = _Database()
    events: list[str] = []
    candidate_path = Path("candidate-generation")
    receipt = _candidate()
    monkeypatch.setattr(
        schema_module,
        "read_current_grafx_schema_version",
        lambda _database: "0.3.12",
    )

    def rebuild(source, path, *, batch_size):
        assert source is database
        assert path == candidate_path
        assert batch_size == 128
        events.append("candidate")
        return receipt

    monkeypatch.setattr(schema_module, "rebuild_grafx_schema_candidate", rebuild)
    manager = CommunityGrafxGraphSchemaManager(
        lambda _board_id: database,
        lambda _board_id, phase: events.append(f"fence:{phase}"),
        candidate_path_resolver=lambda _board_id: candidate_path,
        candidate_activator=lambda board_id, path, result: events.append(
            f"activate:{board_id}:{path}:{result.target_schema_version}"
        ),
        rebuild_batch_size=128,
    )

    summary = await manager.migrate("board-2")

    assert summary["activated"] is True
    assert summary["candidate_database_uuid"] == b"candidate-uuid".hex()
    assert events == [
        "fence:schema_migrate",
        "fence:schema_migrate_candidate",
        "candidate",
        "fence:schema_migrate_cutover",
        f"activate:board-2:{candidate_path}:{receipt.target_schema_version}",
    ]


async def test_schema_migrate_refuses_missing_activator_before_candidate(
    monkeypatch,
) -> None:
    database = _Database()
    built = False
    monkeypatch.setattr(
        schema_module,
        "read_current_grafx_schema_version",
        lambda _database: "0.3.12",
    )

    def rebuild(*_args, **_kwargs):
        nonlocal built
        built = True

    monkeypatch.setattr(schema_module, "rebuild_grafx_schema_candidate", rebuild)
    manager = CommunityGrafxGraphSchemaManager(
        lambda _board_id: database,
        lambda _board_id, _phase: None,
        candidate_path_resolver=lambda _board_id: Path("candidate"),
    )

    with pytest.raises(GraphCapabilityUnavailable):
        await manager.migrate("board-3")
    assert built is False


async def test_schema_migrate_failure_never_activates_or_touches_primary(
    tmp_path,
    monkeypatch,
) -> None:
    database = _Database()
    primary = tmp_path / "active" / "grafx.meta"
    primary.parent.mkdir()
    primary.write_bytes(b"active-generation")
    activated = False
    monkeypatch.setattr(
        schema_module,
        "read_current_grafx_schema_version",
        lambda _database: "0.3.12",
    )

    def rebuild(*_args, **_kwargs):
        raise GraphCapabilityUnavailable("candidate refused")

    def activate(*_args) -> None:
        nonlocal activated
        activated = True

    monkeypatch.setattr(schema_module, "rebuild_grafx_schema_candidate", rebuild)
    manager = CommunityGrafxGraphSchemaManager(
        lambda _board_id: database,
        lambda _board_id, _phase: None,
        candidate_path_resolver=lambda _board_id: tmp_path / "candidate",
        candidate_activator=activate,
    )

    with pytest.raises(GraphCapabilityUnavailable):
        await manager.migrate("board-failed")
    assert activated is False
    assert primary.read_bytes() == b"active-generation"


async def test_schema_admission_refuses_small_persisted_page_before_bootstrap(
    monkeypatch,
) -> None:
    called = False

    def ensure(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(schema_module, "ensure_current_grafx_board_schema", ensure)
    manager = CommunityGrafxGraphSchemaManager(
        lambda _board_id: _Database(page_size=2048),
        lambda _board_id, _phase: None,
    )

    with pytest.raises(GraphCapabilityUnavailable) as caught:
        await manager.ensure_bootstrapped("board-small")
    assert caught.value.details["minimum_page_size"] == 4096
    assert called is False


async def test_lifecycle_rebuild_refuses_small_page_without_changing_primary(
    tmp_path,
) -> None:
    path = tmp_path / "graph.grafx"
    path.mkdir()
    identity = path / "grafx.meta"
    identity.write_bytes(b"active-generation")
    lifecycle = CommunityGrafxGraphLifecycle(
        lambda _board_id: _Database(page_size=2048),
        lambda _board_id: path,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
    )

    result = await lifecycle.rebuild("board-small")

    assert result.status == "failed"
    assert result.reason == "graph_capability_unavailable"
    assert identity.read_bytes() == b"active-generation"


async def test_lifecycle_covers_open_close_rebuild_and_purge(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "graph.grafx"
    path.mkdir()
    (path / "grafx.meta").write_bytes(b"identity")
    database = _Database()
    closes: list[str | None] = []
    fences: list[str] = []
    monkeypatch.setattr(
        lifecycle_module,
        "ensure_current_grafx_board_schema",
        lambda _database, **_kwargs: SimpleNamespace(changed=False),
    )
    monkeypatch.setattr(
        lifecycle_module,
        "validate_current_grafx_schema",
        lambda _database: "fingerprint",
    )

    def quarantine(_board_id, target, *, reason):
        assert reason == "operator_manual"
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
        return 1, "quarantine-1"

    monkeypatch.setattr(
        lifecycle_module,
        "quarantine_grafx_board_storage",
        quarantine,
    )
    lifecycle = CommunityGrafxGraphLifecycle(
        lambda _board_id: database,
        lambda _board_id: path,
        lambda board_id: closes.append(board_id),
        lambda _board_id, phase: fences.append(phase),
    )

    handle = await lifecycle.open("board-1")
    await lifecycle.close("board-1")
    rebuilt = await lifecycle.rebuild("board-1")
    purged = await lifecycle.purge("board-1", reason="operator_manual")

    assert handle.opened is True
    assert rebuilt.status == "rebuilt"
    assert rebuilt.steps == ("close", "open", "admission", "validate")
    assert purged.status == "purged"
    assert purged.quarantine_ref == "quarantine-1"
    assert closes == ["board-1", "board-1", "board-1"]
    assert "bootstrap" in fences
    assert fences.count("purge") == 2


def test_lifecycle_apply_step_uses_only_public_grafx_operations(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "graph.grafx"
    path.mkdir()
    (path / "grafx.meta").write_bytes(b"identity")
    database = _Database()
    monkeypatch.setattr(
        lifecycle_module,
        "validate_current_grafx_schema",
        lambda _database: "fingerprint",
    )
    lifecycle = CommunityGrafxGraphLifecycle(
        lambda _board_id: database,
        lambda _board_id: path,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
    )

    for step in (STEP_CHECKPOINT, STEP_FLUSH, STEP_FSYNC, STEP_CLOSE_REOPEN_PROBE):
        assert lifecycle.apply_step("board-1", "board_graph", step).ok is True

    assert database.events == ["checkpoint", "flush", "flush", "checkpoint"]
    assert lifecycle.apply_step("board-1", "global_discovery", STEP_FLUSH).ok is False
    assert lifecycle.apply_step("board-1", "board_graph", "unknown").ok is False


def test_runtime_graph_state_covers_all_four_non_opening_states(tmp_path) -> None:
    unavailable = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: (_ for _ in ()).throw(RuntimeError("no provider")),
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: (_ for _ in ()).throw(
            RuntimeError("no storage root")
        ),
    ).graph_state("board-1")
    assert (
        unavailable.normalized_state
        is GraphRuntimeObservationState.PROVIDER_UNAVAILABLE
    )

    path = tmp_path / "graph.grafx"
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: path,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: tmp_path / "boards" / "board-1",
    )
    absent = store.graph_state("board-1", generation="g1")
    assert absent.normalized_state is GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert absent.generation == "g1"

    path.mkdir()
    unreadable = store.graph_state("board-1")
    assert (
        unreadable.normalized_state
        is GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    )
    assert unreadable.reason_code == "board_graph_identity_missing"

    (path / "grafx.meta").write_bytes(b"identity")
    readable = store.graph_state("board-1")
    assert (
        readable.normalized_state
        is GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )
    assert store.exists("board-1") is True


def test_runtime_purge_erase_footprint_and_budget(tmp_path, monkeypatch) -> None:
    board_root = tmp_path / "boards" / "board-1"
    path = board_root / "grafx" / "generation-2"
    path.mkdir(parents=True)
    (path / "grafx.meta").write_bytes(b"meta")
    (path / "heap.dat").write_bytes(b"123456")
    previous = path.parent / "generation-1"
    previous.mkdir()
    (previous / "grafx.meta").write_bytes(b"previous")
    binding = _write_foundation_binding(board_root, generation="generation-2")
    closes: list[str | None] = []
    fences: list[str] = []
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: path,
        lambda board_id: closes.append(board_id),
        lambda _board_id, phase: fences.append(phase),
        board_storage_root_resolver=lambda _board_id: board_root,
        configured_max_bytes=lambda: 100,
    )

    footprint = store.footprint("board-1")
    budget = store.budget_snapshot()
    assert footprint.total_bytes == 10
    assert footprint.primary_bytes == 10
    assert footprint.percentage == 10.0
    assert budget.effective["database_max_bytes"] == 100

    def quarantine(_board_id, target, *, reason):
        assert reason == "manual"
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
        return 1, "quarantine-1"

    monkeypatch.setattr(
        runtime_module,
        "quarantine_grafx_board_storage",
        quarantine,
    )
    purged = store.purge_board_graph("board-1", reason="manual")
    assert purged.status == "purged"
    assert previous.exists()
    assert binding.exists()

    path.mkdir()
    (path / "grafx.meta").write_bytes(b"private")
    residue = path.with_name(f"{path.name}.candidate")
    residue.mkdir()
    (residue / "heap.dat").write_bytes(b"candidate")
    binding.with_name(f"{binding.name}.lock").write_bytes(b"lock")
    binding.with_name(f".{binding.name}.stale.tmp").write_bytes(b"temp")
    unrelated = board_root / "keep.txt"
    unrelated.write_bytes(b"not Grafx storage")
    erased = store.erase_board_graph("board-1", reason="right_to_erasure")
    assert erased.status == "erased"
    assert not path.parent.exists()
    assert not binding.exists()
    assert not binding.with_name(f"{binding.name}.lock").exists()
    assert not binding.with_name(f".{binding.name}.stale.tmp").exists()
    assert unrelated.read_bytes() == b"not Grafx storage"
    assert closes == ["board-1", "board-1"]
    assert fences.count("purge") == 2
    assert fences.count("privacy_erase") >= 10


def test_privacy_erase_removes_all_foundation_generations_and_reacquires(
    tmp_path,
) -> None:
    board_root = tmp_path / "boards" / "board-1"
    generation_1 = board_root / "grafx" / "generation-1"
    generation_2 = board_root / "grafx" / "generation-2"
    generation_1.mkdir(parents=True)
    generation_2.mkdir()
    (generation_1 / "grafx.meta").write_bytes(b"old-private-data")
    (generation_2 / "grafx.meta").write_bytes(b"active-private-data")
    _write_foundation_binding(board_root, generation="generation-2")

    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: _foundation_bound_path(board_root),
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: board_root,
    )

    result = store.erase_board_graph("board-1", reason="right_to_erasure")

    assert result.status == "erased"
    assert not generation_1.exists()
    assert not generation_2.exists()
    assert not (board_root / "graph_backend_binding.json").exists()
    absent = store.graph_state("board-1")
    assert absent.normalized_state is GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert absent.reason_code == "board_graph_canonical_storage_absent"

    generation_3 = board_root / "grafx" / "generation-3"
    generation_3.mkdir(parents=True)
    (generation_3 / "grafx.meta").write_bytes(b"new-board-data")
    _write_foundation_binding(board_root, generation="generation-3")
    reacquired = store.graph_state("board-1")
    assert (
        reacquired.normalized_state
        is GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )


def test_privacy_erase_retry_does_not_depend_on_the_deleted_active_generation(
    tmp_path,
) -> None:
    board_root = tmp_path / "boards" / "board-1"
    active = board_root / "grafx" / "generation-2"
    active.mkdir(parents=True)
    (active / "grafx.meta").write_bytes(b"private")
    binding = _write_foundation_binding(board_root, generation="generation-2")

    def expire_after_graph_bytes_are_gone(_board_id: str, _phase: str) -> None:
        if not active.parent.exists() and binding.exists():
            raise RuntimeError("writer_fence_expired")

    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: _foundation_bound_path(board_root),
        lambda _board_id: None,
        expire_after_graph_bytes_are_gone,
        board_storage_root_resolver=lambda _board_id: board_root,
    )
    interrupted = store.erase_board_graph("board-1", reason="right_to_erasure")
    assert interrupted.status == "failed"
    assert not active.parent.exists()
    assert binding.exists()
    unresolved = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: active,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: board_root,
    ).graph_state("board-1")
    assert (
        unresolved.normalized_state
        is GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR
    )

    retry = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: _foundation_bound_path(board_root),
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: board_root,
    ).erase_board_graph("board-1", reason="right_to_erasure_retry")
    assert retry.status == "erased"
    assert not binding.exists()


def test_privacy_erase_never_reacquires_the_terminally_deleted_binding(
    tmp_path,
) -> None:
    board_root = tmp_path / "boards" / "board-1"
    active = board_root / "grafx" / "generation-1"
    active.mkdir(parents=True)
    (active / "grafx.meta").write_bytes(b"private")
    binding = _write_foundation_binding(board_root, generation="generation-1")
    fence_calls: list[tuple[str, str]] = []

    def binding_backed_fence(board_id: str, phase: str) -> None:
        if not binding.exists():
            raise RuntimeError("binding_reacquired_after_terminal_erasure")
        fence_calls.append((board_id, phase))

    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: _foundation_bound_path(board_root),
        lambda _board_id: None,
        binding_backed_fence,
        board_storage_root_resolver=lambda _board_id: board_root,
    )

    erased = store.erase_board_graph("board-1", reason="right_to_erasure")

    assert erased.status == "erased"
    assert erased.removed is True
    assert not binding.exists()
    assert not active.parent.exists()
    assert fence_calls
    calls_after_erasure = tuple(fence_calls)

    retry = store.erase_board_graph("board-1", reason="right_to_erasure_retry")

    assert retry.status == "not_found"
    assert retry.not_found is True
    assert retry.removed is False
    assert tuple(fence_calls) == calls_after_erasure


def test_privacy_erase_receipt_fails_closed_when_absence_is_unverified(
    tmp_path,
    monkeypatch,
) -> None:
    board_root = tmp_path / "boards" / "board-1"
    active = board_root / "grafx" / "generation-1"
    active.mkdir(parents=True)
    (active / "grafx.meta").write_bytes(b"private")
    _write_foundation_binding(board_root, generation="generation-1")
    real_present = runtime_module.grafx_board_privacy_storage_present
    observations = 0

    def absence_unverified(scope) -> bool:
        nonlocal observations
        observations += 1
        if observations == 1:
            return real_present(scope)
        return True

    monkeypatch.setattr(
        runtime_module,
        "grafx_board_privacy_storage_present",
        absence_unverified,
    )
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: active,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: board_root,
    )

    result = store.erase_board_graph("board-1", reason="right_to_erasure")

    assert result.status == "failed"
    assert result.removed is False
    assert result.not_found is False
    assert result.error_code == "physical_erasure_absence_unverified"
    assert observations == 2


def test_privacy_erase_revalidates_fence_before_each_filesystem_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    board_root = tmp_path / "boards" / "board-1"
    active = board_root / "grafx" / "generation-1"
    active.mkdir(parents=True)
    (active / "grafx.meta").write_bytes(b"private")
    _write_foundation_binding(board_root, generation="generation-1")
    events: list[str] = []
    real_unlink = Path.unlink
    real_rmdir = Path.rmdir

    def observed_unlink(path: Path, *args, **kwargs):
        assert events[-1] == "fence"
        events.append("unlink")
        return real_unlink(path, *args, **kwargs)

    def observed_rmdir(path: Path, *args, **kwargs):
        assert events[-1] == "fence"
        events.append("rmdir")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", observed_unlink)
    monkeypatch.setattr(Path, "rmdir", observed_rmdir)
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: active,
        lambda _board_id: None,
        lambda _board_id, _phase: events.append("fence"),
        board_storage_root_resolver=lambda _board_id: board_root,
    )

    result = store.erase_board_graph("board-1", reason="right_to_erasure")

    assert result.status == "erased"
    assert "unlink" in events
    assert "rmdir" in events


def test_privacy_erase_unlinks_aliases_without_traversing_them(tmp_path) -> None:
    board_root = tmp_path / "boards" / "board-1"
    active = board_root / "grafx" / "generation-1"
    active.mkdir(parents=True)
    (active / "grafx.meta").write_bytes(b"private")
    external = tmp_path / "outside"
    external.mkdir()
    sentinel = external / "must-remain.txt"
    sentinel.write_bytes(b"outside")
    try:
        (active / "linked-copy").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    _write_foundation_binding(board_root, generation="generation-1")
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: active,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: board_root,
    )

    result = store.erase_board_graph("board-1", reason="right_to_erasure")

    assert result.status == "erased"
    assert sentinel.read_bytes() == b"outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction probe requires nt")
def test_privacy_erase_unlinks_junction_without_traversing_it(
    tmp_path,
    monkeypatch,
) -> None:
    board_root = tmp_path / "boards" / "board-1"
    active = board_root / "grafx" / "generation-1"
    active.mkdir(parents=True)
    (active / "grafx.meta").write_bytes(b"private")
    external = tmp_path / "junction-target"
    external.mkdir()
    sentinel = external / "must-remain.txt"
    sentinel.write_bytes(b"outside")
    junction = active / "linked-copy"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if made.returncode != 0 or not junction.exists():
        pytest.skip(f"junction unavailable: {made.stderr.strip()!r}")
    junction_metadata = junction.lstat()
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    assert int(getattr(junction_metadata, "st_file_attributes", 0)) & reparse_flag
    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    assert is_filesystem_alias(junction)
    _write_foundation_binding(board_root, generation="generation-1")
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: active,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: board_root,
    )

    result = store.erase_board_graph("board-1", reason="right_to_erasure")

    assert result.status == "erased"
    with pytest.raises(FileNotFoundError):
        junction.lstat()
    assert external.is_dir()
    assert sentinel.read_bytes() == b"outside"


def test_runtime_purge_failure_preserves_primary_storage(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "graph.grafx"
    path.mkdir()
    identity = path / "grafx.meta"
    identity.write_bytes(b"active-generation")
    monkeypatch.setattr(
        runtime_module,
        "quarantine_grafx_board_storage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: path,
        lambda _board_id: None,
        lambda _board_id, _phase: None,
        board_storage_root_resolver=lambda _board_id: tmp_path / "boards" / "board-1",
    )

    result = store.purge_board_graph("board-1", reason="manual")

    assert result.status == "failed"
    assert identity.read_bytes() == b"active-generation"
