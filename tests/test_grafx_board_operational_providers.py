from __future__ import annotations

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
        CommunityGrafxGraphRuntimeStore(path, close, fence),
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
    path = tmp_path / "graph.grafx"
    path.mkdir()
    (path / "grafx.meta").write_bytes(b"meta")
    (path / "heap.dat").write_bytes(b"123456")
    closes: list[str | None] = []
    fences: list[str] = []
    store = CommunityGrafxGraphRuntimeStore(
        lambda _board_id: path,
        lambda board_id: closes.append(board_id),
        lambda _board_id, phase: fences.append(phase),
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

    path.mkdir()
    (path / "grafx.meta").write_bytes(b"private")
    residue = path.with_name(f"{path.name}.candidate")
    residue.mkdir()
    (residue / "heap.dat").write_bytes(b"candidate")
    erased = store.erase_board_graph("board-1", reason="right_to_erasure")
    assert erased.status == "erased"
    assert not path.exists()
    assert not residue.exists()
    assert closes == ["board-1", "board-1"]
    assert fences.count("purge") == 2
    assert fences.count("privacy_erase") >= 3


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
    )

    result = store.purge_board_graph("board-1", reason="manual")

    assert result.status == "failed"
    assert identity.read_bytes() == b"active-generation"
