from __future__ import annotations

import json
from pathlib import Path

import pytest

from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
    CommunityGlobalDiscoveryRecoveryError,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    GlobalDiscoveryLayoutError,
    active_pointer_path,
    generation_graph_path,
    read_active_generation,
    resolve_active_graph_path,
    switch_active_generation,
    write_generation_manifest,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    bootstrap_marker_present,
    write_bootstrap_marker,
)
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryDigestSeed,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult


_SCHEMA = ("Board", "DecisionDigest", "CONTAINS_DECISION")


class _UnreadableLiveRuntime:
    """Represents the installed live runtime whose open raises MemoryError."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.open_attempts = 0
        self.close_calls = 0
        self.successful_cutovers: list[Path] = []
        self.candidate_states: dict[Path, dict] = {}

    def close(self) -> None:
        self.close_calls += 1

    def list_schema_objects(self):
        self.open_attempts += 1
        raise MemoryError("bad allocation")

    def note_successful_generation_cutover(
        self, *, active_path: Path, fence_check=None
    ) -> bool:
        if fence_check is not None:
            fence_check()
        self.successful_cutovers.append(active_path)
        return True


class _CandidateRuntime:
    def __init__(self, path: Path, state: dict, *, fail_readback: bool = False) -> None:
        self.path = path
        self.state = state
        self.fail_readback = fail_readback

    def bootstrap(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"fresh-candidate")
        self.path.with_name(self.path.name + ".wal").write_bytes(b"candidate-wal")
        self.state.setdefault("boards", {})
        self.state.setdefault("digests", {})
        self.state.setdefault("links", set())

    def list_schema_objects(self):
        if self.fail_readback:
            raise RuntimeError("synthetic readback failure with local path")
        return _SCHEMA

    def close(self) -> None:
        return None

    def flush_after_write_batch(self) -> None:
        return None

    def upsert_board_summary(self, **values) -> None:
        self.state["boards"][values["board_id"]] = dict(values)

    def upsert_decision_digest(self, **values) -> str:
        self.state["digests"][values["digest_id"]] = dict(values)
        return "inserted"

    def link_board_digest(self, *, board_id: str, digest_id: str) -> None:
        self.state["links"].add((board_id, digest_id))

    def execute(self, statement: str, params=None) -> GraphStatementResult:
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


def _build_adapter(
    live: Path,
    *,
    fail_live_readback: bool = False,
):
    composed_live = _UnreadableLiveRuntime(live)
    created: list[Path] = []
    states = composed_live.candidate_states
    creation_counts: dict[Path, int] = {}

    def factory(path: Path):
        created.append(path)
        creation_counts[path] = creation_counts.get(path, 0) + 1
        return _CandidateRuntime(
            path,
            states.setdefault(path, {}),
            fail_readback=(fail_live_readback and creation_counts[path] > 1),
        )

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    return adapter, composed_live, created


def _boards():
    digest = GlobalDiscoveryDigestSeed(
        original_node_id="node-1",
        title="Decision",
        summary="Decision",
        node_type="Decision",
        graph_layer="canonical",
        source_artifact_ref="artifact-1",
        embedding=(0.1, 0.2),
    )
    return (
        GlobalDiscoveryBoardSeed(
            board_id="board-1",
            board_name="Board One",
            summary="Board summary",
            summary_embedding=(0.3, 0.4),
            digests=(digest,),
            source_inventory_hash="source-hash",
        ),
    )


def _two_boards():
    first = _boards()[0]
    second_digest = GlobalDiscoveryDigestSeed(
        original_node_id="node-2",
        title="Second decision",
        summary="Second decision summary",
        node_type="Decision",
        graph_layer="canonical",
        source_artifact_ref="artifact-2",
        embedding=(0.5, 0.6),
    )
    return (
        first,
        GlobalDiscoveryBoardSeed(
            board_id="other-2",
            board_name="Board Two",
            summary="Second board summary",
            summary_embedding=(0.7, 0.8),
            digests=(second_digest,),
            source_inventory_hash="source-hash-2",
        ),
    )


def test_unreadable_memoryerror_live_is_replaced_without_opening_it(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"corrupt-primary")
    live.with_name(live.name + ".wal").write_bytes(b"corrupt-wal")
    adapter, composed_live, created = _build_adapter(live)
    before = adapter.inspect_live_artifact()

    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_memoryerror",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )

    assert result.outcome == "completed"
    assert result.rollback_performed is False
    assert isinstance(result.directory_fsync_supported, bool)
    active = resolve_active_graph_path(live)
    assert active != live
    assert active.read_bytes() == b"fresh-candidate"
    assert active.with_name(active.name + ".wal").read_bytes() == b"candidate-wal"
    assert live.read_bytes() == b"corrupt-primary"
    quarantine = live.parent / "quarantine" / "global-discovery" / "gdr_memoryerror"
    assert (quarantine / "original" / live.name).read_bytes() == b"corrupt-primary"
    assert (quarantine / "original" / (live.name + ".wal")).read_bytes() == (
        b"corrupt-wal"
    )
    assert (quarantine / "recovery_journal.json").exists()
    assert composed_live.open_attempts == 0
    assert composed_live.close_calls >= 1
    assert composed_live.successful_cutovers == [active]
    assert any("discovery.generations" in str(path) for path in created)
    assert created[-1] == active  # post-cutover readback uses a fresh runtime
    assert (
        composed_live.candidate_states[active]["boards"]["board-1"]["summary"]
        == "Board summary"
    )
    # Provenance remains fingerprint/policy input until DecisionDigest gains a
    # schema column; recovery must not silently invent adapter-only storage.
    assert (
        "source_artifact_ref"
        not in composed_live.candidate_states[active]["digests"]["dd_board-1_node-1"]
    )


def test_post_cutover_readback_failure_rolls_back_and_preserves_both(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original-primary")
    live.with_name(live.name + ".wal").write_bytes(b"original-wal")
    adapter, composed_live, _created = _build_adapter(
        live,
        fail_live_readback=True,
    )
    before = adapter.inspect_live_artifact()

    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_rollback",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )

    assert result.outcome == "rolled_back"
    assert result.rollback_performed is True
    assert result.failure_code == "global_discovery_post_cutover_readback_failed"
    assert live.read_bytes() == b"original-primary"
    assert live.with_name(live.name + ".wal").read_bytes() == b"original-wal"
    quarantine = live.parent / "quarantine" / "global-discovery" / "gdr_rollback"
    assert (quarantine / "original" / live.name).read_bytes() == b"original-primary"
    assert (quarantine / "recovery_journal.json").exists()
    assert read_active_generation(live) is None
    assert composed_live.open_attempts == 0


def test_live_hash_drift_fails_before_quarantine_or_cutover(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"original")
    adapter, composed_live, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    live.write_bytes(b"drifted")

    with pytest.raises(Exception) as refused:
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_drift",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert getattr(refused.value, "code", None) == (
        "global_discovery_live_artifact_drift"
    )
    assert live.read_bytes() == b"drifted"
    assert read_active_generation(live) is None
    assert composed_live.open_attempts == 0


def test_unknown_sidecar_is_fingerprinted_and_preserved_without_mixed_cutover(
    tmp_path,
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    unknown = live.with_name(live.name + ".future-engine-sidecar")
    unknown.write_bytes(b"legacy-unknown-sidecar")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()

    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_sidecars",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )

    assert result.outcome == "completed"
    assert live.read_bytes() == b"legacy-primary"
    assert unknown.read_bytes() == b"legacy-unknown-sidecar"
    copied = (
        live.parent
        / "quarantine"
        / "global-discovery"
        / "gdr_sidecars"
        / "original"
        / unknown.name
    )
    assert copied.read_bytes() == b"legacy-unknown-sidecar"
    assert resolve_active_graph_path(live).parent.name == "gdr_sidecars"


def test_crash_after_pointer_replace_recovers_to_one_complete_generation(
    tmp_path, monkeypatch
):
    from okto_pulse.community.adapters import global_discovery_recovery as module

    class SyntheticCrash(BaseException):
        pass

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    live.with_name(live.name + ".wal").write_bytes(b"legacy-wal")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    real_switch = module.switch_active_generation

    def switch_then_crash(*args, **kwargs):
        real_switch(*args, **kwargs)
        raise SyntheticCrash()

    monkeypatch.setattr(module, "switch_active_generation", switch_then_crash)
    with pytest.raises(SyntheticCrash):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_crashpoint",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    active = read_active_generation(live)
    assert active is not None and active.generation_id == "gdr_crashpoint"
    assert active.graph_path.read_bytes() == b"fresh-candidate"

    monkeypatch.setattr(module, "switch_active_generation", real_switch)
    resumed = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_crashpoint",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert resumed.outcome == "rolled_back"
    assert resumed.rollback_performed is True
    assert read_active_generation(live) is None
    assert live.read_bytes() == b"legacy-primary"
    assert live.with_name(live.name + ".wal").read_bytes() == b"legacy-wal"


def test_crash_while_building_discards_partial_generation_and_rebuilds(
    tmp_path, monkeypatch
):
    class SyntheticCrash(BaseException):
        pass

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    real_materialize = adapter._materialize  # noqa: SLF001

    def crash_materialize(_runtime, _boards, *, fence_check=None):
        del fence_check
        raise SyntheticCrash()

    monkeypatch.setattr(adapter, "_materialize", crash_materialize)
    with pytest.raises(SyntheticCrash):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_buildcrash",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert read_active_generation(live) is None
    with pytest.raises(SyntheticCrash):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_buildcrash",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )

    monkeypatch.setattr(adapter, "_materialize", real_materialize)
    resumed = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_buildcrash",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert resumed.outcome == "completed"
    interrupted = (
        live.parent
        / "quarantine"
        / "global-discovery"
        / "gdr_buildcrash"
        / "failed-candidate"
        / "interrupted"
        / live.name
    )
    assert interrupted.read_bytes() == b"fresh-candidate"
    assert (interrupted.parent.parent / "interrupted-1" / live.name).read_bytes() == (
        b"fresh-candidate"
    )


def test_crash_after_prepared_journal_resumes_forward_without_mixed_store(
    tmp_path, monkeypatch
):
    from okto_pulse.community.adapters import global_discovery_recovery as module

    class SyntheticCrash(BaseException):
        pass

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    real_switch = module.switch_active_generation

    monkeypatch.setattr(
        module,
        "switch_active_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SyntheticCrash()),
    )
    with pytest.raises(SyntheticCrash):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_preparedcrash",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert read_active_generation(live) is None
    assert live.read_bytes() == b"legacy-primary"

    monkeypatch.setattr(module, "switch_active_generation", real_switch)
    resumed = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_preparedcrash",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert resumed.outcome == "completed"
    assert resolve_active_graph_path(live).read_bytes() == b"fresh-candidate"


@pytest.mark.parametrize(
    "crash_phase", ["pointer_switched", "readback_validated", "completed"]
)
def test_crash_after_durable_phase_journal_resumes_idempotently(
    tmp_path, monkeypatch, crash_phase
):
    from okto_pulse.community.adapters import global_discovery_recovery as module

    class SyntheticCrash(BaseException):
        pass

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    # Fault the production journal seam.  R5 keeps directory-fsync support as
    # durable outcome truth, so production deliberately uses the writer that
    # returns that capability instead of the legacy bool-only test helper.
    real_write = module._write_journal_with_directory_fsync  # noqa: SLF001
    crashed = False

    def write_then_crash(path, payload, *, fence_check=None):
        nonlocal crashed
        result = real_write(path, payload, fence_check=fence_check)
        if not crashed and payload.get("phase") == crash_phase:
            crashed = True
            raise SyntheticCrash()
        return result

    monkeypatch.setattr(
        module,
        "_write_journal_with_directory_fsync",
        write_then_crash,
    )
    with pytest.raises(SyntheticCrash):
        adapter.rebuild_candidate_and_cutover(
            run_id=f"gdr_{crash_phase}",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    active = read_active_generation(live)
    assert active is not None and active.generation_id == f"gdr_{crash_phase}"

    monkeypatch.setattr(
        module,
        "_write_journal_with_directory_fsync",
        real_write,
    )
    resumed = adapter.rebuild_candidate_and_cutover(
        run_id=f"gdr_{crash_phase}",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert resumed.outcome == "completed"
    assert resolve_active_graph_path(live).read_bytes() == b"fresh-candidate"


def test_crash_after_rollback_pointer_is_resumed_from_rollback_journal(
    tmp_path, monkeypatch
):
    class SyntheticCrash(BaseException):
        pass

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live, fail_live_readback=True)
    before = adapter.inspect_live_artifact()
    real_restore = adapter._restore_previous  # noqa: SLF001

    def restore_then_crash(**kwargs):
        real_restore(**kwargs)
        raise SyntheticCrash()

    monkeypatch.setattr(adapter, "_restore_previous", restore_then_crash)
    with pytest.raises(SyntheticCrash):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_rollbackcrash",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert read_active_generation(live) is None
    assert live.read_bytes() == b"legacy-primary"

    monkeypatch.setattr(adapter, "_restore_previous", real_restore)
    resumed = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_rollbackcrash",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert resumed.outcome == "rolled_back"
    assert resumed.rollback_performed is True
    assert read_active_generation(live) is None


def test_candidate_count_mismatch_never_switches_active_generation(
    tmp_path, monkeypatch
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    real_materialize = adapter._materialize  # noqa: SLF001

    def omit_links(runtime, boards, *, fence_check=None):
        real_materialize(runtime, boards, fence_check=fence_check)
        runtime.state["links"].clear()

    monkeypatch.setattr(adapter, "_materialize", omit_links)
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as refused:
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_badcounts",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert refused.value.code == "global_discovery_candidate_semantic_mismatch"
    assert read_active_generation(live) is None
    assert live.read_bytes() == b"legacy-primary"


def test_active_pointer_rejects_unsafe_generation_id_before_path_resolution(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy")
    active_pointer_path(live).write_text(
        json.dumps(
            {
                "layout_version": 1,
                "generation_id": "gdr_../../escape",
                "manifest_sha256": "0" * 64,
                "pointer_sha256": "invalid",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(GlobalDiscoveryLayoutError):
        resolve_active_graph_path(live)


def test_warm_runtime_detects_pointer_change_before_reusing_cached_handle(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy")
    runtime = CommunityGlobalDiscoveryRuntime(graph_path_provider=lambda: live)
    runtime._db = object()  # noqa: SLF001
    runtime._db_path = live.resolve()  # noqa: SLF001
    assert runtime._database_is_open() is True  # noqa: SLF001

    generation_id = "gdr_runtimepointer"
    candidate = generation_graph_path(live, generation_id)
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"candidate")
    manifest_sha, _supported = write_generation_manifest(
        live,
        generation_id,
        {
            "graph_filename": live.name,
            "artifact_sha256_at_cutover": "0" * 64,
        },
    )
    switch_active_generation(
        live,
        generation_id=generation_id,
        manifest_sha256=manifest_sha,
    )

    assert runtime._global_graph_path() == candidate.resolve()  # noqa: SLF001
    assert runtime._database_is_open() is False  # noqa: SLF001


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        ("board_name", "Mutated board"),
        ("board_summary", "Mutated board summary"),
        ("board_embedding", [9.0, 9.0]),
        ("digest_title", "Mutated decision"),
        ("digest_summary", "Mutated decision summary"),
        ("digest_type", "Risk"),
        ("digest_layer", "historical"),
        ("digest_embedding", [8.0, 8.0]),
    ],
)
def test_same_count_semantic_mutation_never_reaches_cutover(
    tmp_path, monkeypatch, target, replacement
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    materialize = adapter._materialize  # noqa: SLF001

    def materialize_then_mutate(runtime, boards, *, fence_check=None):
        materialize(runtime, boards, fence_check=fence_check)
        board = runtime.state["boards"]["board-1"]
        digest = runtime.state["digests"]["dd_board-1_node-1"]
        if target == "board_name":
            board["name"] = replacement
        elif target == "board_summary":
            board["summary"] = replacement
        elif target == "board_embedding":
            board["summary_embedding"] = replacement
        elif target == "digest_title":
            digest["title"] = replacement
        elif target == "digest_summary":
            digest["summary"] = replacement
        elif target == "digest_type":
            digest["node_type"] = replacement
        elif target == "digest_layer":
            digest["graph_layer"] = replacement
        else:
            digest["embedding"] = replacement

    monkeypatch.setattr(adapter, "_materialize", materialize_then_mutate)
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as refused:
        adapter.rebuild_candidate_and_cutover(
            run_id=f"gdr_semantic_{target}",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )

    assert refused.value.code == "global_discovery_candidate_semantic_mismatch"
    assert read_active_generation(live) is None
    assert live.read_bytes() == b"legacy-primary"


@pytest.mark.parametrize("mutation", ["swapped", "duplicate"])
def test_exact_link_multiset_rejects_swapped_or_duplicate_links(
    tmp_path, monkeypatch, mutation
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    materialize = adapter._materialize  # noqa: SLF001

    def materialize_then_corrupt_links(runtime, boards, *, fence_check=None):
        materialize(runtime, boards, fence_check=fence_check)
        links = list(runtime.state["links"])
        if mutation == "duplicate":
            runtime.state["links"] = [*links, links[0]]
            return
        digest_by_board = {board_id: digest_id for board_id, digest_id in links}
        runtime.state["links"] = [
            ("board-1", digest_by_board["other-2"]),
            ("other-2", digest_by_board["board-1"]),
        ]

    monkeypatch.setattr(adapter, "_materialize", materialize_then_corrupt_links)
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as refused:
        adapter.rebuild_candidate_and_cutover(
            run_id=f"gdr_links_{mutation}",
            expected_live_sha256=before.sha256,
            boards=_two_boards(),
        )

    assert refused.value.code == "global_discovery_candidate_semantic_mismatch"
    assert read_active_generation(live) is None


def test_post_cutover_same_count_mutation_is_detected_and_rolled_back(
    tmp_path, monkeypatch
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, _created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    validate = adapter._validate_runtime  # noqa: SLF001
    calls = 0

    def mutate_fresh_readback(runtime, boards, *, fence_check=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            runtime.state["digests"]["dd_board-1_node-1"]["title"] = "Drifted"
        return validate(runtime, boards, fence_check=fence_check)

    monkeypatch.setattr(adapter, "_validate_runtime", mutate_fresh_readback)
    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_readback_semantic_drift",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )

    assert result.outcome == "rolled_back"
    assert result.rollback_performed is True
    assert result.failure_code == "global_discovery_candidate_semantic_mismatch"
    assert read_active_generation(live) is None
    assert live.read_bytes() == b"legacy-primary"


def test_legacy_digest_id_collision_fails_before_any_candidate_artifact(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"legacy-primary")
    adapter, _composed, created = _build_adapter(live)
    before = adapter.inspect_live_artifact()
    digest = _boards()[0].digests[0]
    colliding = tuple(
        GlobalDiscoveryBoardSeed(
            board_id=board_id,
            board_name=board_id,
            summary="summary",
            summary_embedding=(0.1, 0.2),
            digests=(digest,),
            source_inventory_hash=f"source-{board_id}",
        )
        for board_id in ("12345678-a", "12345678-b")
    )

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as refused:
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_digest_id_collision",
            expected_live_sha256=before.sha256,
            boards=colliding,
        )

    assert refused.value.code == "global_discovery_candidate_digest_id_collision"
    assert created == []
    assert read_active_generation(live) is None
    assert not generation_graph_path(live, "gdr_digest_id_collision").parent.exists()


def test_community_workflow_adds_candidate_before_similarity_and_proposal():
    workflow = (
        Path(__file__).parents[1]
        / "src/okto_pulse/community/resources/operational/workflows/kg.md"
    ).read_text(encoding="utf-8")
    block = workflow.split("**Consolidation workflow:**", 1)[1].split("```", 2)[1]
    assert (
        block.index("okto_pulse_kg_add_node_candidate")
        < block.index("okto_pulse_kg_get_similar_nodes")
        < block.index("okto_pulse_kg_propose_reconciliation")
    )
    assert "candidate_not_found" in workflow


def test_community_artifact_store_persists_global_recovery_status(tmp_path):
    from okto_pulse.community.adapters.rebuild_audit_storage import (
        CommunityFileSystemRebuildAuditArtifactStore,
    )
    from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey

    store = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(
        namespace="global_discovery_recovery",
        board_id="_global",
        artifact_id="status_gdr_test",
    )
    store.write_json_atomic(key, {"state": "completed"})
    assert store.read_json(key) == {"state": "completed"}
    assert "global_discovery_recovery" in store.reference(key)


def test_blocker12_candidate_sha_binds_to_final_post_readback_bytes(
    tmp_path, monkeypatch
):
    """Blocker 21/33: the post-cutover fresh-validation close grows the artifact
    (WAL) after the candidate snapshot.  ``readback_validated`` + ``completed``
    candidate_sha256, the returned result SHA, and the final active snapshot SHA
    must all bind to the FINAL post-growth SHA, while the immutable manifest's
    ``artifact_sha256_at_cutover`` stays truthful to its earlier boundary."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    from okto_pulse.community.adapters.global_discovery_layout import generation_dir

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"corrupt-primary")
    live.with_name(live.name + ".wal").write_bytes(b"corrupt-wal")

    composed_live = _UnreadableLiveRuntime(live)
    states = composed_live.candidate_states
    creation_counts: dict[Path, int] = {}

    class _GrowingReadback(_CandidateRuntime):
        def close(self) -> None:
            # Simulate LOAD VECTOR growth of the real .wal SIDECAR on the
            # post-cutover readback close, AFTER the candidate snapshot was
            # captured at build time (blocker 9).
            wal = self.path.with_name(self.path.name + ".wal")
            with wal.open("ab") as handle:
                handle.write(b"g")
            return None

    def factory(path: Path):
        creation_counts[path] = creation_counts.get(path, 0) + 1
        state = states.setdefault(path, {})
        # 1st creation for a path = candidate build; 2nd = post-cutover readback.
        if creation_counts[path] >= 2:
            return _GrowingReadback(path, state)
        return _CandidateRuntime(path, state)

    recorded: list[dict] = []
    real_write = rec_mod._write_journal_with_directory_fsync

    def _recording_write(path, payload, *, fence_check=None):
        recorded.append(dict(payload))
        return real_write(path, payload, fence_check=fence_check)

    monkeypatch.setattr(
        rec_mod, "_write_journal_with_directory_fsync", _recording_write
    )

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    result = adapter.rebuild_candidate_and_cutover(
        run_id="gdr_walgrow",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"

    active = read_active_generation(live)
    assert active is not None
    final_snapshot = rec_mod._snapshot(active.graph_path)
    final_sha = final_snapshot.sha256

    prepared_payloads = [p for p in recorded if p.get("phase") == "prepared"]
    readback_payloads = [p for p in recorded if p.get("phase") == "readback_validated"]
    completed_payloads = [p for p in recorded if p.get("phase") == "completed"]
    assert prepared_payloads and readback_payloads and completed_payloads

    # Exact pre-growth artifact-set SHA captured at the manifest boundary
    # (prepared phase).
    pre_growth_sha = prepared_payloads[-1]["candidate_sha256"]
    manifest_path = (
        generation_dir(live, active.generation_id) / "generation_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact_sha256_at_cutover"] == pre_growth_sha

    # The deterministic post-cutover readback close grew the real .wal SIDECAR by
    # exactly one byte ('g'); the .lbug primary is untouched, so the artifact-set
    # SHA differs but the sidecar INVENTORY (count/paths) is preserved.
    wal_path = active.graph_path.with_name(active.graph_path.name + ".wal")
    assert active.graph_path.read_bytes() == b"fresh-candidate"  # primary intact
    assert wal_path.read_bytes() == b"candidate-walg"  # sidecar grew by one byte
    assert manifest["artifact_count_at_cutover"] == final_snapshot.artifact_count == 2
    assert pre_growth_sha != final_sha

    # Every terminal binding is the FINAL post-growth SHA.
    assert readback_payloads[-1]["candidate_sha256"] == final_sha
    assert completed_payloads[-1]["candidate_sha256"] == final_sha
    assert result.candidate_sha256 == final_sha


def test_recover_and_cutover_falls_back_when_schema_complete_but_incoherent(tmp_path):
    """Blocker 3/4: a SCHEMA-complete but structurally-incoherent primary (a
    Board with decision_count=1 but no digest/link population) is NOT adopted —
    recovery falls back to rebuilding the active generation from authoritative
    seeds.  This case would pass a mere schema+nonempty-Board check."""

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"partial-primary")
    live.with_name(live.name + ".wal").write_bytes(b"partial-wal")
    # Marker present so adoption is attempted (then rejected as incoherent).
    write_bootstrap_marker(live)

    composed_live = _UnreadableLiveRuntime(live)
    states = composed_live.candidate_states
    factory_calls = {"n": 0}

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
                            "decision_count": 1,  # claims a decision, but...
                            "summary_embedding": [0.0, 0.0],
                        }
                    },
                    "digests": {},  # ...no digests and no links => incoherent
                    "links": set(),
                },
            )

    def factory(path: Path):
        factory_calls["n"] += 1
        if factory_calls["n"] == 1:
            # The very first factory call is the adoption copy open:
            # schema-complete but structurally incoherent -> rejected.
            return _IncoherentAdoptRuntime(path)
        return _CandidateRuntime(path, states.setdefault(path, {}))

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()

    result = adapter.recover_and_cutover(
        run_id="gdr_incoh",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    active = resolve_active_graph_path(live)
    # Published from authoritative seeds (board-1), NOT the incoherent board-x.
    assert states[active]["boards"].get("board-1")
    assert "board-x" not in states[active]["boards"]


def _coherent_adopt_state() -> dict:
    return {
        "boards": {
            "board-a": {
                "board_id": "board-a",
                "name": "A",
                "summary": "s",
                "decision_count": 1,
                "summary_embedding": [0.1, 0.2],
            }
        },
        "digests": {
            "digest-a": {
                "digest_id": "digest-a",
                "board_id": "board-a",
                "original_node_id": "node-a",
                "title": "t",
                "summary": "s",
                "node_type": "Decision",
                "graph_layer": "canonical",
                "embedding": [0.3, 0.4],
            }
        },
        "links": {("board-a", "digest-a")},
    }


def _build_adopting_adapter(live: Path):
    """Adapter whose candidate factory always returns a COHERENT complete primary
    (shared state), so the adoption path succeeds and re-entry windows can be
    fault-injected deterministically."""

    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    # Adoption is gated on the durable product marker (blocker 2).
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)
    shared_state = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared_state)

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    return adapter, composed_live


def test_adopt_reentry_pre_switch_crash_discards_orphan_and_readopts(
    tmp_path, monkeypatch
):
    """Item 5: a crash AFTER mkdir/copy/prepared but BEFORE switch strands an
    unpublished candidate; re-entry discards the orphan and re-adopts cleanly."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    adapter, composed_live = _build_adopting_adapter(live)
    before = adapter.inspect_live_artifact()

    real_switch = rec_mod.switch_active_generation
    fail = {"on": True}

    def flaky_switch(legacy, *, generation_id, manifest_sha256):
        if fail["on"]:
            raise RuntimeError("pre-switch crash")
        return real_switch(legacy, generation_id=generation_id, manifest_sha256=manifest_sha256)

    monkeypatch.setattr(rec_mod, "switch_active_generation", flaky_switch)

    with pytest.raises(RuntimeError, match="pre-switch crash"):
        adapter.recover_and_cutover(
            run_id="gdr_reentrypre",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    # Not published (switch failed): pointer still legacy.
    assert resolve_active_graph_path(live) == live
    # Blocker 7: a DURABLE adoption journal (phase=prepared) exists on disk, and
    # resume is driven by that journal + the pointer, not in-memory fake state.
    journal_path = (
        live.parent / "quarantine" / "global-discovery" / "gdr_reentrypre"
        / "recovery_journal.json"
    )
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert persisted["kind"] == "adopt_complete_primary"
    assert persisted["phase"] == "prepared"

    # Re-entry: orphan discarded, fresh adoption succeeds and publishes.
    fail["on"] = False
    result = adapter.recover_and_cutover(
        run_id="gdr_reentrypre",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert resolve_active_graph_path(live) != live
    assert composed_live.successful_cutovers  # marker cleared via cutover


def test_adopt_reentry_post_switch_crash_finalizes_forward(tmp_path, monkeypatch):
    """Item 5: a crash AFTER switch but BEFORE the completed journal leaves the
    candidate PUBLISHED; re-entry reconciles forward to completed + clear without
    deleting the active candidate."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    adapter, composed_live = _build_adopting_adapter(live)
    before = adapter.inspect_live_artifact()

    real_write = rec_mod._write_journal_with_directory_fsync
    fail = {"on": True}

    def flaky_write(path, payload, *, fence_check=None):
        if (
            fail["on"]
            and payload.get("phase") == "completed"
            and payload.get("kind") == "adopt_complete_primary"
        ):
            raise RuntimeError("post-switch pre-completed crash")
        return real_write(path, payload, fence_check=fence_check)

    monkeypatch.setattr(rec_mod, "_write_journal_with_directory_fsync", flaky_write)

    with pytest.raises(RuntimeError, match="post-switch pre-completed crash"):
        adapter.recover_and_cutover(
            run_id="gdr_reentrypost",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    # Published (pointer switched) but not completed; marker not yet cleared.
    published = resolve_active_graph_path(live)
    assert published != live
    assert not composed_live.successful_cutovers
    # Blocker 7: a DURABLE adoption journal (phase=pointer_switched) exists on
    # disk; resume finalizes forward from that durable journal + the pointer.
    journal_path = (
        live.parent / "quarantine" / "global-discovery" / "gdr_reentrypost"
        / "recovery_journal.json"
    )
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert persisted["kind"] == "adopt_complete_primary"
    assert persisted["phase"] == "pointer_switched"

    # Re-entry: reconcile forward to completed + clear (no candidate deletion).
    fail["on"] = False
    result = adapter.recover_and_cutover(
        run_id="gdr_reentrypost",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert resolve_active_graph_path(live) == published  # same published generation
    assert composed_live.successful_cutovers


def test_adoption_gated_on_marker_returns_none_with_zero_work(tmp_path):
    """Blocker 2: marker-absent recovery must never open/copy/adopt the live
    primary through the adoption bypass — zero adoption runtime/copy/switch work."""

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    # NO marker written.
    factory_calls = {"n": 0}

    def factory(path: Path):
        factory_calls["n"] += 1
        return _CandidateRuntime(path, _coherent_adopt_state())

    composed_live = _UnreadableLiveRuntime(live)
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    result = adapter._adopt_complete_primary(
        run_id="gdr_nomarker",
        epoch=1,
        attempt_id=None,
        expected_live_sha256=before.sha256,
        fence_check=lambda: None,
    )
    assert result is None
    assert factory_calls["n"] == 0  # zero adoption runtime work
    from okto_pulse.community.adapters.global_discovery_layout import generations_root

    gens = generations_root(live)
    assert not gens.exists() or not any(gens.iterdir())  # zero switch/copy work
    assert not composed_live.successful_cutovers


def test_adoption_completed_resume_threads_false_clear_then_noop(tmp_path, monkeypatch):
    """Blocker 3: adoption completed-resume ANDs the clear-directory-fsync result
    into the returned durability (false-clear -> false), and a second resume is a
    zero-clear no-op."""

    from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
        clear_bootstrap_marker,
    )

    live = tmp_path / "global" / "discovery.lbug"
    adapter, composed_live = _build_adopting_adapter(live)
    before = adapter.inspect_live_artifact()

    real_note = composed_live.note_successful_generation_cutover
    hold = {"on": True}
    note_calls = {"n": 0}

    def note_wrapper(*, active_path, fence_check):
        note_calls["n"] += 1
        if hold["on"]:
            raise RuntimeError("hold-clear")
        # Actually unlink the durable marker (the fake would not), then report a
        # FALSE clear-directory fsync.
        clear_bootstrap_marker(live, fence_check=fence_check)
        real_note(active_path=active_path, fence_check=fence_check)
        return False

    monkeypatch.setattr(
        composed_live, "note_successful_generation_cutover", note_wrapper
    )

    with pytest.raises(RuntimeError, match="hold-clear"):
        adapter.recover_and_cutover(
            run_id="gdr_adoptfclr",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True  # completed durable, marker kept

    # First resume: false clear-directory fsync is threaded into the result.
    hold["on"] = False
    note_calls["n"] = 0
    result = adapter.recover_and_cutover(
        run_id="gdr_adoptfclr",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert result.directory_fsync_supported is False
    assert bootstrap_marker_present(live) is False
    assert note_calls["n"] == 1  # cleared exactly once

    # Second resume: marker absent -> zero clear no-op, and the conservative
    # false persisted by the first resume is still reported (blocker 5: cannot
    # report true after a false clear-directory fsync).
    note_calls["n"] = 0
    result2 = adapter.recover_and_cutover(
        run_id="gdr_adoptfclr",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result2.outcome == "completed"
    assert result2.directory_fsync_supported is False
    assert note_calls["n"] == 0


def test_blocker5_post_switch_resume_preserves_false_journal_fsync(
    tmp_path, monkeypatch
):
    """Blocker 5: a prepared/pointer_switched adoption journal with a FALSE
    directory-fsync must NOT become true on post-switch resume."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    adapter, composed_live = _build_adopting_adapter(live)
    before = adapter.inspect_live_artifact()

    # Force every directory fsync FALSE and hold the clear so the run reaches a
    # durable pointer_switched journal (fsync=false), marker present.
    import okto_pulse.community.adapters.global_discovery_layout as layout_mod
    import okto_pulse.community.adapters.global_discovery_bootstrap_marker as marker_mod

    monkeypatch.setattr(layout_mod, "fsync_directory", lambda _p: False)
    monkeypatch.setattr(rec_mod, "fsync_directory", lambda _p: False)
    monkeypatch.setattr(marker_mod, "fsync_directory", lambda _p: False)

    # Crash at the terminal completed-journal write so a durable
    # pointer_switched journal (fsync=false) remains, marker present.
    real_write = rec_mod._write_journal_with_directory_fsync
    fail = {"on": True}

    def flaky_write(path, payload, *, fence_check=None):
        if (
            fail["on"]
            and payload.get("phase") == "completed"
            and payload.get("kind") == "adopt_complete_primary"
        ):
            raise RuntimeError("post-switch crash")
        return real_write(path, payload, fence_check=fence_check)

    monkeypatch.setattr(rec_mod, "_write_journal_with_directory_fsync", flaky_write)
    with pytest.raises(RuntimeError, match="post-switch crash"):
        adapter.recover_and_cutover(
            run_id="gdr_b5postswitch",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True
    journal_path = (
        live.parent / "quarantine" / "global-discovery" / "gdr_b5postswitch"
        / "recovery_journal.json"
    )
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert persisted["phase"] == "pointer_switched"
    assert persisted["directory_fsync_supported"] is False

    # Post-switch resume must carry that conservative false into the result
    # (never hardcode True).
    fail["on"] = False
    result = adapter.recover_and_cutover(
        run_id="gdr_b5postswitch",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert result.directory_fsync_supported is False


def test_adoption_malformed_terminal_journal_preserves_marker(tmp_path):
    """Blocker 5: a malformed terminal adoption journal fails closed BEFORE any
    marker clear or runtime mutation."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=lambda p: _CandidateRuntime(p, _coherent_adopt_state()),  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    run_id = "gdr_malformed"
    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation_id = rec_mod._physical_generation_id(
        run_id=run_id, attempt_id=attempt_id, epoch=epoch
    )
    journal_dir = live.parent / "quarantine" / "global-discovery" / attempt_id
    journal_dir.mkdir(parents=True)
    bad_journal = {
        "run_id": run_id,
        "epoch": epoch,
        "attempt_id": attempt_id,
        "generation_id": generation_id,
        "kind": "adopt_complete_primary",
        "phase": "completed",
        "outcome": "completed",
        "rollback_performed": False,
        "candidate_sha256": "not-a-valid-sha",  # malformed
        "generation_manifest_sha256": "a" * 64,
        "schema_object_count": 3,
        "counts_by_board": {"board-a": {"boards": 1, "digests": 1, "links": 1}},
        "semantic_fingerprint": "b" * 64,
    }
    rec_mod._write_journal_with_directory_fsync(
        journal_dir / "recovery_journal.json", bad_journal, fence_check=lambda: None
    )

    with pytest.raises(
        CommunityGlobalDiscoveryRecoveryError,
        match="adoption_terminal_journal_invalid",
    ):
        adapter.recover_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256="c" * 64,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True  # preserved
    assert not composed_live.successful_cutovers  # zero clear


def test_adoption_corrupt_open_falls_back_to_rebuild(tmp_path):
    """Blocker 6: a corrupt/unreadable adoption copy open falls back to seed
    rebuild (narrow expected error)."""

    from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)
    states = composed_live.candidate_states
    calls = {"n": 0}

    class _CorruptRuntime(_CandidateRuntime):
        def list_schema_objects(self):
            raise GraphCorruption("synthetic corrupt open")

    def factory(path: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            return _CorruptRuntime(path, _coherent_adopt_state())
        return _CandidateRuntime(path, states.setdefault(path, {}))

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    result = adapter.recover_and_cutover(
        run_id="gdr_corruptopen",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"  # fell back to rebuild
    active = resolve_active_graph_path(live)
    assert states[active]["boards"].get("board-1")


def test_adoption_fence_loss_propagates_no_fallback(tmp_path):
    """Blocker 6: fence/authority loss during the adoption open must PROPAGATE
    (never converted to fallback); marker preserved."""

    from okto_pulse.core.kg.global_discovery_writer import (
        GlobalDiscoveryWriterFenceLost,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)

    class _FenceLostRuntime(_CandidateRuntime):
        def list_schema_objects(self):
            raise GlobalDiscoveryWriterFenceLost()

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=lambda p: _FenceLostRuntime(p, _coherent_adopt_state()),  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        adapter.recover_and_cutover(
            run_id="gdr_fenceloss",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True  # preserved, no fallback


# --- Stable-audit rejection regressions (blockers 3/4/5/6) ----------------


def _write_adoption_journal(live: Path, run_id: str, phase: str, *, fsync=True):
    """Write a nonterminal adoption journal to disk for the given run/phase."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )

    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation_id = rec_mod._physical_generation_id(
        run_id=run_id, attempt_id=attempt_id, epoch=epoch
    )
    journal_dir = live.parent / "quarantine" / "global-discovery" / attempt_id
    journal_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "epoch": epoch,
        "attempt_id": attempt_id,
        "generation_id": generation_id,
        "kind": "adopt_complete_primary",
        "phase": phase,
        "candidate_sha256": "a" * 64,
        "generation_manifest_sha256": "b" * 64,
        "schema_object_count": 3,
        "counts_by_board": {"board-a": {"boards": 1, "digests": 1, "links": 1}},
        "semantic_fingerprint": "c" * 64,
        "directory_fsync_supported": fsync,
    }
    rec_mod._write_journal_with_directory_fsync(
        journal_dir / "recovery_journal.json", payload, fence_check=lambda: None
    )
    return attempt_id, epoch


@pytest.mark.parametrize("phase", ["prepared", "pointer_switched"])
def test_blocker3_nonterminal_adoption_journal_absent_marker_fails_closed(
    tmp_path, phase
):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    # NONTERMINAL adoption journal on disk, but the marker is ABSENT.
    attempt_id, epoch = _write_adoption_journal(live, "gdr_b3nonterm", phase)

    composed_live = _UnreadableLiveRuntime(live)
    factory_calls = {"n": 0}

    def factory(path: Path):
        factory_calls["n"] += 1
        return _CandidateRuntime(path, _coherent_adopt_state())

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    with pytest.raises(
        CommunityGlobalDiscoveryRecoveryError,
        match="adoption_marker_missing_for_nonterminal",
    ):
        adapter.recover_and_cutover(
            run_id="gdr_b3nonterm",
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256="d" * 64,
            boards=_boards(),
        )
    # Zero runtime factory/open/copy/switch/delete/clear.
    assert factory_calls["n"] == 0
    assert not composed_live.successful_cutovers


def test_blocker4_factory_open_failure_falls_back_to_rebuild(tmp_path):
    from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)
    states = composed_live.candidate_states
    calls = {"n": 0}

    def factory(path: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            # The FACTORY itself fails to open the corrupt copy (blocker 4).
            raise GraphCorruption("synthetic factory open failure")
        return _CandidateRuntime(path, states.setdefault(path, {}))

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    result = adapter.recover_and_cutover(
        run_id="gdr_b4factory",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"  # fell back to seed rebuild
    active = resolve_active_graph_path(live)
    assert states[active]["boards"].get("board-1")


def test_blocker4_factory_fence_loss_propagates_no_fallback(tmp_path):
    from okto_pulse.core.kg.global_discovery_writer import (
        GlobalDiscoveryWriterFenceLost,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)

    def factory(path: Path):
        # Authority/fence loss during factory creation must PROPAGATE.
        raise GlobalDiscoveryWriterFenceLost()

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        adapter.recover_and_cutover(
            run_id="gdr_b4fence",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True


# --- R3: fence every mutating close; never swallow authority close errors -----


class _CloseErrorAdoptRuntime:
    """Adoption-copy runtime whose validation raises an expected-corrupt error
    and whose (potentially mutating) close raises a configurable error."""

    def __init__(self, path: Path, *, close_error: BaseException | None) -> None:
        self.path = path
        self.close_error = close_error
        self.close_calls = 0

    def list_schema_objects(self):
        from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption

        raise GraphCorruption("synthetic corrupt adoption copy")

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _adoption_adapter(live: Path, factory):
    composed_live = _UnreadableLiveRuntime(live)
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    return adapter, composed_live


def test_r3_corrupt_open_fallback_preserves_original_on_benign_close(tmp_path):
    """R3: an expected-corrupt adoption copy whose close raises a BENIGN error
    still falls back to seed rebuild — the benign close is suppressed so it
    cannot mask the corrupt-open cause — and the close WAS attempted."""

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    adopt_runtimes: list[_CloseErrorAdoptRuntime] = []
    states: dict = {}
    calls = {"n": 0}

    def factory(path: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            rt = _CloseErrorAdoptRuntime(
                path, close_error=RuntimeError("benign close cleanup io")
            )
            adopt_runtimes.append(rt)
            return rt
        return _CandidateRuntime(path, states.setdefault(path, {}))

    adapter, _composed = _adoption_adapter(live, factory)
    before = adapter.inspect_live_artifact()
    result = adapter.recover_and_cutover(
        run_id="gdr_r3benign",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"  # fell back to seed rebuild
    assert adopt_runtimes[0].close_calls == 1  # the close was attempted
    active = resolve_active_graph_path(live)
    assert states[active]["boards"].get("board-1")


@pytest.mark.parametrize("close_error_name", ["lock_contention", "fence_lost"])
def test_r3_corrupt_open_fallback_authority_close_error_propagates(
    tmp_path, close_error_name
):
    """R3: a close that signals LOST AUTHORITY (GraphLockContention / fence loss)
    during the corrupt-open fallback MUST propagate — never swallowed, no
    fallback, marker preserved, zero cutover."""

    from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
    from okto_pulse.core.kg.global_discovery_writer import (
        GlobalDiscoveryWriterFenceLost,
    )

    if close_error_name == "lock_contention":
        close_error: BaseException = GraphLockContention("synthetic close lock")
        expected_exc: type[BaseException] = GraphLockContention
    else:
        close_error = GlobalDiscoveryWriterFenceLost()
        expected_exc = GlobalDiscoveryWriterFenceLost

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    calls = {"n": 0}

    def factory(path: Path):
        calls["n"] += 1
        assert calls["n"] == 1, "no seed-rebuild fallback may occur"
        return _CloseErrorAdoptRuntime(path, close_error=close_error)

    adapter, composed_live = _adoption_adapter(live, factory)
    before = adapter.inspect_live_artifact()
    with pytest.raises(expected_exc):
        adapter.recover_and_cutover(
            run_id="gdr_r3authclose",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True
    assert calls["n"] == 1  # never fell back
    assert not composed_live.successful_cutovers


def _patch_fsync_directory_true(monkeypatch):
    """Pin every directory-fsync boundary True so an injected/inherited false is
    isolated (Windows dir fsync is naturally False)."""

    import okto_pulse.community.adapters.global_discovery_layout as layout_mod
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    monkeypatch.setattr(layout_mod, "fsync_directory", lambda _p: True)
    monkeypatch.setattr(rec_mod, "fsync_directory", lambda _p: True)


def test_r4_adoption_crash_after_switch_before_pointer_journal_resume_false(
    tmp_path, monkeypatch
):
    """R4 issue #3: the pointer is switched but the switch's own directory fsync
    was NOT yet journaled (crash in the prepared -> pointer_switched window).  A
    resume with EVERY current fsync true must still report false — the optimistic
    ``prepared`` durability must NOT be inherited on post-switch resume."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    _patch_fsync_directory_true(monkeypatch)

    real_write = rec_mod._write_journal_with_directory_fsync

    def fault_pointer_switched(path, payload, *, fence_check=None):
        # The `prepared` journal (dfs=True) is written first; the switch runs;
        # then fault the `pointer_switched` write -> a durable prepared journal +
        # an already-switched pointer.
        if payload.get("phase") == "pointer_switched":
            raise RuntimeError("R4 crash after switch before pointer journal")
        return real_write(path, payload, fence_check=fence_check)

    monkeypatch.setattr(
        rec_mod, "_write_journal_with_directory_fsync", fault_pointer_switched
    )
    with pytest.raises(
        RuntimeError, match="after switch before pointer journal"
    ):
        adapter.recover_and_cutover(
            run_id="gdr_r4switch",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )

    # Resume with EVERY current fsync true (writes restored, dir fsync pinned).
    monkeypatch.setattr(rec_mod, "_write_journal_with_directory_fsync", real_write)
    result = adapter.recover_and_cutover(
        run_id="gdr_r4switch",
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    # The prior `prepared` durability (True) must NOT be inherited: the switch's
    # own directory fsync was unconfirmed, so the resume forces false.
    assert result.directory_fsync_supported is False


def test_r4_pointer_switched_replace_false_then_crash_resume_all_true_stays_false(
    tmp_path, monkeypatch
):
    """R4 pointer boundary: the pointer_switched journal replace COMPLETES (its
    own directory fsync unsupported), then a crash occurs BEFORE the next
    persistence (the finalize completed write).  The durable pointer_switched
    journal persisted a conservative false; an active pointer exists; a resume
    with EVERY current fsync true must still report false (no rebound)."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    import json as _json

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    _patch_fsync_directory_true(monkeypatch)

    real_write = rec_mod._write_journal_with_directory_fsync
    run_id = "gdr_r4ptrfalse"
    attempt_id = None
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )

    # Fault the FIRST completed-phase write (the finalize pending/completed
    # journal), which happens AFTER the pointer_switched journal was durably
    # written and the pointer switched.
    def fault_after_pointer_switched(path, payload, *, fence_check=None):
        if payload.get("phase") == "completed":
            raise RuntimeError("R4 crash after pointer_switched before completed")
        return real_write(path, payload, fence_check=fence_check)

    monkeypatch.setattr(
        rec_mod, "_write_journal_with_directory_fsync", fault_after_pointer_switched
    )
    with pytest.raises(RuntimeError, match="before completed"):
        adapter.recover_and_cutover(
            run_id=run_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )

    # The durable pointer_switched journal persisted a conservative false, NOT an
    # optimistic true, even though every fsync boundary was patched true.
    attempt_id = recovery_attempt_id(run_id, 1)
    journal_path = (
        live.parent / "quarantine" / "global-discovery" / run_id
        / "recovery_journal.json"
    )
    if not journal_path.exists():
        journal_path = (
            live.parent / "quarantine" / "global-discovery" / attempt_id
            / "recovery_journal.json"
        )
    switched = _json.loads(journal_path.read_text(encoding="utf-8"))
    assert switched["phase"] == "pointer_switched"
    assert switched["directory_fsync_supported"] is False

    # Resume with EVERY current fsync true; an active pointer already exists.
    monkeypatch.setattr(rec_mod, "_write_journal_with_directory_fsync", real_write)
    result = adapter.recover_and_cutover(
        run_id=run_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
    )
    assert result.outcome == "completed"
    assert result.directory_fsync_supported is False


class _CloseSpyRuntime:
    def __init__(self, err: BaseException | None = None) -> None:
        self.err = err
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.err is not None:
            raise self.err


def test_r3_fenced_readback_close_unit_matrix():
    """R3 unit proof of the potentially-mutating readback close helper: fence is
    revalidated before the close (a lost fence skips the mutating close);
    authority close errors always propagate; benign errors propagate on the
    success path but are suppressed on the exceptional path."""

    from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
    from okto_pulse.core.kg.global_discovery_writer import (
        GlobalDiscoveryWriterFenceLost,
    )
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    close = rec_mod.CommunityGlobalDiscoveryRecovery._fenced_readback_close

    # (a) Fence lost BEFORE close -> mutating close SKIPPED, fence error surfaces.
    def _lost_fence():
        raise RuntimeError("fence gone")

    rb = _CloseSpyRuntime()
    with pytest.raises(rec_mod.CommunityGlobalDiscoveryRecoveryFenceError):
        close(rb, _lost_fence, in_flight_error=None)
    assert rb.close_calls == 0  # never mutated under a lost fence

    # (b) Success path (no in-flight error): a benign close error PROPAGATES.
    rb = _CloseSpyRuntime(err=RuntimeError("benign close"))
    with pytest.raises(RuntimeError, match="benign close"):
        close(rb, lambda: None, in_flight_error=None)
    assert rb.close_calls == 1

    # (c) Exceptional path: a benign close error is SUPPRESSED (original wins).
    rb = _CloseSpyRuntime(err=RuntimeError("benign close"))
    close(rb, lambda: None, in_flight_error=ValueError("original failure"))
    assert rb.close_calls == 1

    # (d) Authority close error propagates even on the exceptional path.
    for err in (GraphLockContention("lock"), GlobalDiscoveryWriterFenceLost()):
        rb = _CloseSpyRuntime(err=err)
        with pytest.raises(type(err)):
            close(rb, lambda: None, in_flight_error=ValueError("orig"))
        assert rb.close_calls == 1


def test_r3_close_adopt_runtime_preserving_unit_matrix():
    """R3 unit proof: a benign close is suppressed (preserves the corrupt-open
    cause); every authority close error propagates; ``None`` is a no-op."""

    from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
    from okto_pulse.core.kg.global_discovery_writer import (
        GlobalDiscoveryWriterFenceLost,
    )
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    preserve = (
        rec_mod.CommunityGlobalDiscoveryRecovery._close_adopt_runtime_preserving
    )

    preserve(None)  # no-op, no raise

    rt = _CloseSpyRuntime(err=RuntimeError("benign cleanup"))
    preserve(rt)  # benign suppressed
    assert rt.close_calls == 1

    for err in (
        GraphLockContention("lock"),
        GlobalDiscoveryWriterFenceLost(),
        rec_mod.CommunityGlobalDiscoveryRecoveryFenceError(RuntimeError("x")),
    ):
        rt = _CloseSpyRuntime(err=err)
        with pytest.raises(type(err)):
            preserve(rt)
        assert rt.close_calls == 1


class _CloseRaisingCandidate(_CandidateRuntime):
    """A seed-rebuild candidate/readback runtime whose (WAL-checkpointing) close
    raises a configurable error; validation still succeeds."""

    def __init__(self, path: Path, state: dict, *, close_error) -> None:
        super().__init__(path, state)
        self._close_error = close_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


@pytest.mark.parametrize("site", ["candidate", "readback"])
@pytest.mark.parametrize("err_name", ["lock_contention", "fence_lost"])
def test_r3_seed_rebuild_close_authority_error_not_reclassified(
    tmp_path, site, err_name
):
    """R3: an authority error (GraphLockContention / writer-fence loss) from the
    seed-rebuild candidate close (recovery.py:2451) OR the post-switch readback
    close (:2588) is revalidation-fenced and propagates UNCHANGED — the broad
    outer handler must NOT reclassify it into rollback / candidate_build_failed
    (a reclassification would surface CommunityGlobalDiscoveryRecoveryError)."""

    from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
    from okto_pulse.core.kg.global_discovery_writer import (
        GlobalDiscoveryWriterFenceLost,
    )

    if err_name == "lock_contention":
        err: BaseException = GraphLockContention("close lock contention")
        expected: type[BaseException] = GraphLockContention
    else:
        err = GlobalDiscoveryWriterFenceLost()
        expected = GlobalDiscoveryWriterFenceLost

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"live-primary")
    live.with_name(live.name + ".wal").write_bytes(b"live-wal")
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)
    states: dict = {}
    calls = {"n": 0}
    raise_on = 1 if site == "candidate" else 2

    def factory(path: Path):
        calls["n"] += 1
        state = states.setdefault(path, {})
        if calls["n"] == raise_on:
            return _CloseRaisingCandidate(path, state, close_error=err)
        return _CandidateRuntime(path, state)

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    with pytest.raises(expected):
        adapter.rebuild_candidate_and_cutover(
            run_id="gdr_r3seedclose",
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    # Marker preserved; the durable journal never reached a reclassified rollback.
    assert bootstrap_marker_present(live) is True
    journal_path = (
        live.parent / "quarantine" / "global-discovery"
        / "gdr_r3seedclose" / "recovery_journal.json"
    )
    if journal_path.exists():
        j = json.loads(journal_path.read_text(encoding="utf-8"))
        assert j.get("phase") not in ("rollback_pending", "rolled_back")
        assert j.get("outcome") not in ("rolling_back", "rolled_back")


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation_manifest_sha256", "e" * 64),  # valid 64-hex, wrong value
        ("directory_fsync_supported", "false"),  # string, not bool
        ("kind", "seed_rebuild"),
        ("phase", "prepared"),
        ("outcome", "rolling_back"),
        ("rollback_performed", True),
        ("candidate_sha256", "not-a-sha"),
        ("schema_object_count", 0),
        ("semantic_fingerprint", ""),
    ],
)
def test_blocker6_malformed_adoption_terminal_journal_preserves_marker(
    tmp_path, field, value
):
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    write_bootstrap_marker(live)
    composed_live = _UnreadableLiveRuntime(live)
    factory_calls = {"n": 0}

    def factory(path: Path):
        factory_calls["n"] += 1
        return _CandidateRuntime(path, _coherent_adopt_state())

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    run_id = "gdr_b6malformed"
    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation_id = rec_mod._physical_generation_id(
        run_id=run_id, attempt_id=attempt_id, epoch=epoch
    )
    journal_dir = live.parent / "quarantine" / "global-discovery" / attempt_id
    journal_dir.mkdir(parents=True)
    journal = {
        "run_id": run_id,
        "epoch": epoch,
        "attempt_id": attempt_id,
        "generation_id": generation_id,
        "kind": "adopt_complete_primary",
        "phase": "completed",
        "outcome": "completed",
        "rollback_performed": False,
        "candidate_sha256": "a" * 64,
        "generation_manifest_sha256": "b" * 64,
        "schema_object_count": 3,
        "counts_by_board": {"board-a": {"boards": 1, "digests": 1, "links": 1}},
        "semantic_fingerprint": "c" * 64,
        "directory_fsync_supported": True,
        "clear_settled": False,
    }
    journal[field] = value  # inject the single malformation
    rec_mod._write_journal_with_directory_fsync(
        journal_dir / "recovery_journal.json", journal, fence_check=lambda: None
    )

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError):
        adapter.recover_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256="d" * 64,
            boards=_boards(),
        )
    # Marker preserved; zero clear / runtime mutation.
    assert bootstrap_marker_present(live) is True
    assert factory_calls["n"] == 0
    assert not composed_live.successful_cutovers


def test_blocker6_reconcile_attempt_terminal_truth_adoption_manifest_mismatch(
    tmp_path,
):
    """Direct reconcile_attempt_terminal_truth adoption coverage: a completed
    adoption journal whose manifest SHA does not bind to the active pointer is
    rejected, marker preserved."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )

    live = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(live)  # creates live.parent
    composed_live = _UnreadableLiveRuntime(live)
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=lambda p: _CandidateRuntime(p, _coherent_adopt_state()),  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    run_id = "gdr_b6reconcile"
    epoch = 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation_id = rec_mod._physical_generation_id(
        run_id=run_id, attempt_id=attempt_id, epoch=epoch
    )
    journal_dir = live.parent / "quarantine" / "global-discovery" / attempt_id
    journal_dir.mkdir(parents=True)
    # A completed adoption journal, but NO active pointer exists -> generation
    # mismatch (a stricter failure than manifest mismatch; both preserve marker).
    journal = {
        "run_id": run_id,
        "epoch": epoch,
        "attempt_id": attempt_id,
        "generation_id": generation_id,
        "kind": "adopt_complete_primary",
        "phase": "completed",
        "outcome": "completed",
        "rollback_performed": False,
        "candidate_sha256": "a" * 64,
        "generation_manifest_sha256": "b" * 64,
        "schema_object_count": 3,
        "counts_by_board": {"board-a": {"boards": 1, "digests": 1, "links": 1}},
        "semantic_fingerprint": "c" * 64,
        "directory_fsync_supported": True,
        "clear_settled": False,
    }
    rec_mod._write_journal_with_directory_fsync(
        journal_dir / "recovery_journal.json", journal, fence_check=lambda: None
    )
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError):
        adapter.reconcile_attempt_terminal_truth(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256="d" * 64,
            boards=_boards(),
            fence_check=lambda: None,
        )
    assert bootstrap_marker_present(live) is True
    assert not composed_live.successful_cutovers


# --- R5: exact terminal-adoption structure/binding ---------------------------


def _well_formed_terminal_adoption_journal(run_id, epoch, attempt_id, generation_id):
    return {
        "run_id": run_id,
        "epoch": epoch,
        "attempt_id": attempt_id,
        "generation_id": generation_id,
        "kind": "adopt_complete_primary",
        "phase": "completed",
        "outcome": "completed",
        "rollback_performed": False,
        "candidate_sha256": "a" * 64,
        "generation_manifest_sha256": "b" * 64,
        "schema_object_count": 3,
        "counts_by_board": {"board-a": {"boards": 1, "digests": 1, "links": 1}},
        "semantic_fingerprint": "c" * 64,
        "directory_fsync_supported": True,
        "clear_settled": False,
    }


def _bare_adoption_adapter(tmp_path):
    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True, exist_ok=True)
    return CommunityGlobalDiscoveryRecovery(
        global_runtime=_UnreadableLiveRuntime(live),  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=lambda p: _CandidateRuntime(p, _coherent_adopt_state()),  # type: ignore[arg-type]
        fence_check=lambda: None,
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        # identity fields
        ("run_id", "gdr_wrong", "run_id"),
        ("epoch", 2, "epoch"),  # wrong value
        ("epoch", True, "epoch"),  # bool must be rejected (not just != check)
        ("attempt_id", "gdr_r5term/attempt-9", "attempt_id"),
        ("generation_id", "", "generation_id_empty"),
        ("generation_id", "gen-does-not-bind", "generation_id"),
        # discriminant / phase / outcome
        ("kind", "seed_rebuild", "kind"),
        ("phase", "prepared", "phase"),
        ("phase", "pointer_switched", "phase"),
        ("outcome", "rolling_back", "outcome"),
        ("rollback_performed", True, "rollback_performed"),
        ("rollback_performed", None, "rollback_performed"),
        # content-addressed hashes
        ("candidate_sha256", "not-a-sha", "candidate_sha256"),
        ("candidate_sha256", "A" * 64, "candidate_sha256"),  # uppercase invalid
        ("generation_manifest_sha256", "zz", "manifest_sha256"),
        ("semantic_fingerprint", "", "semantic_fingerprint"),
        # schema count (int, non-bool, positive)
        ("schema_object_count", 0, "schema_object_count"),
        ("schema_object_count", -1, "schema_object_count"),
        ("schema_object_count", True, "schema_object_count"),
        ("schema_object_count", "3", "schema_object_count"),
        # counts_by_board shape / keys / value types
        ("counts_by_board", {}, "counts_by_board"),
        ("counts_by_board", "notadict", "counts_by_board"),
        ("counts_by_board", {"": {"boards": 1, "digests": 1, "links": 1}}, "counts_board_id"),
        ("counts_by_board", {"b": [1, 2, 3]}, "counts_shape"),
        ("counts_by_board", {"b": {"boards": -1, "digests": 1, "links": 1}}, "counts_value"),
        ("counts_by_board", {"b": {"boards": True, "digests": 1, "links": 1}}, "counts_value"),
        ("counts_by_board", {"b": {"boards": 1, "digests": 1}}, "counts_value"),  # missing 'links'
        # exact bool for fsync
        ("directory_fsync_supported", "false", "directory_fsync_supported"),
        ("directory_fsync_supported", 1, "directory_fsync_supported"),
        # R5: EXACT JSON types — a 64-DIGIT INTEGER is NOT a SHA string and must
        # not be str-coerced into passing the hex regex.
        ("candidate_sha256", int("1" * 64), "candidate_sha256"),
        ("generation_manifest_sha256", int("2" * 64), "manifest_sha256"),
        ("semantic_fingerprint", int("3" * 64), "semantic_fingerprint"),
        # non-string identity/discriminant fields (JSON integers) rejected.
        ("kind", 123, "kind"),
        ("run_id", 123, "run_id"),
        ("attempt_id", 123, "attempt_id"),
        ("generation_id", 123, "generation_id_empty"),
        ("phase", 123, "phase"),
        ("outcome", 123, "outcome"),
        # epoch must be an EXACT int (a float 1.0 equals 1 but is not an int).
        ("epoch", 1.0, "epoch"),
        # clear_settled EXACT bool: string "false" is not coerced truthy.
        ("clear_settled", "false", "clear_settled"),
        ("clear_settled", 1, "clear_settled"),
        ("clear_settled", None, "clear_settled"),
        # extra per-board key rejected (exact key set).
        ("counts_by_board", {"b": {"boards": 1, "digests": 1, "links": 1, "x": 1}}, "counts_value"),
    ],
)
def test_r5_terminal_adoption_journal_exact_field_rejection(
    tmp_path, field, value, reason
):
    """R5: the terminal-adoption validator rejects EACH malformed field with the
    EXACT typed reason code, and the well-formed journal passes.  Non-tautological:
    every case is a single-field mutation of a fully valid journal."""

    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    adapter = _bare_adoption_adapter(tmp_path)
    run_id, epoch = "gdr_r5term", 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation_id = rec_mod._physical_generation_id(
        run_id=run_id, attempt_id=attempt_id, epoch=epoch
    )
    base = _well_formed_terminal_adoption_journal(
        run_id, epoch, attempt_id, generation_id
    )
    # The well-formed base passes (proves the mutation is the sole cause).
    adapter._assert_adoption_terminal_journal(
        base, run_id=run_id, epoch=epoch, effective_attempt_id=attempt_id
    )

    malformed = {**base, field: value}
    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc_info:
        adapter._assert_adoption_terminal_journal(
            malformed, run_id=run_id, epoch=epoch, effective_attempt_id=attempt_id
        )
    assert exc_info.value.code == (
        f"global_discovery_adoption_terminal_journal_invalid:{reason}"
    )


def _publish_real_generation(live: Path, generation_id: str) -> str:
    """Publish a real, internally-consistent active generation (manifest +
    pointer) and return its true manifest SHA."""

    from okto_pulse.community.adapters.global_discovery_layout import generation_dir

    gdir = generation_dir(live, generation_id)
    gdir.mkdir(parents=True, exist_ok=True)
    generation_graph_path(live, generation_id).write_bytes(b"published-graph")
    manifest_sha, _ = write_generation_manifest(
        live,
        generation_id,
        {
            "graph_filename": live.name,
            "artifact_sha256_at_cutover": "f" * 64,
        },
    )
    switch_active_generation(
        live, generation_id=generation_id, manifest_sha256=manifest_sha
    )
    return manifest_sha


def test_r5_reconcile_wrong_manifest_with_real_active_pointer_rejected(tmp_path):
    """R5: a completed adoption journal whose manifest SHA does NOT bind to a REAL
    active pointer is rejected with the exact ``completed_manifest_mismatch`` code
    — marker preserved, zero runtime construction (raised before the readback),
    zero clear.  Distinct from the no-pointer generation-mismatch case above."""

    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )

    live = tmp_path / "global" / "discovery.lbug"
    write_bootstrap_marker(live)  # creates live.parent + marker
    composed_live = _UnreadableLiveRuntime(live)
    factory_calls = {"n": 0}

    def factory(path: Path):
        factory_calls["n"] += 1
        return _CandidateRuntime(path, _coherent_adopt_state())

    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    run_id, epoch = "gdr_r5manifest", 1
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation_id = rec_mod._physical_generation_id(
        run_id=run_id, attempt_id=attempt_id, epoch=epoch
    )
    real_manifest_sha = _publish_real_generation(live, generation_id)

    # A well-formed completed adoption journal binding to a DIFFERENT (valid-hex)
    # manifest than the real active pointer.
    wrong_manifest = "b" * 64
    assert wrong_manifest != real_manifest_sha
    journal = _well_formed_terminal_adoption_journal(
        run_id, epoch, attempt_id, generation_id
    )
    journal["generation_manifest_sha256"] = wrong_manifest
    journal_dir = live.parent / "quarantine" / "global-discovery" / attempt_id
    journal_dir.mkdir(parents=True)
    rec_mod._write_journal_with_directory_fsync(
        journal_dir / "recovery_journal.json", journal, fence_check=lambda: None
    )

    with pytest.raises(CommunityGlobalDiscoveryRecoveryError) as exc_info:
        adapter.reconcile_attempt_terminal_truth(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256="d" * 64,
            boards=_boards(),
            fence_check=lambda: None,
        )
    assert exc_info.value.code == "global_discovery_completed_manifest_mismatch"
    assert bootstrap_marker_present(live) is True
    assert factory_calls["n"] == 0  # zero runtime construction (raised pre-readback)
    assert not composed_live.successful_cutovers  # zero clear


class _HoldClearLive(_UnreadableLiveRuntime):
    """Live runtime whose marker clear is held (raises) until released."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.hold = True

    def note_successful_generation_cutover(self, *, active_path, fence_check=None):
        if fence_check is not None:
            fence_check()
        if self.hold:
            raise RuntimeError("hold-clear")
        # Mirror the real runtime: physically clear the durable marker so a
        # marker-absent second reconcile is a true idempotent no-op.
        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            clear_bootstrap_marker,
        )

        clear_bootstrap_marker(self.path, fence_check=fence_check or (lambda: None))
        self.successful_cutovers.append(active_path)
        return True


def test_r5_reconcile_wellformed_adoption_clears_then_second_noop(tmp_path):
    """R5: a fully well-formed durable completed adoption journal (produced by a
    real adoption whose clear was held) is reconciled through
    ``reconcile_attempt_terminal_truth`` — the marker is cleared exactly once and
    a SECOND reconcile is an idempotent no-op with no additional clear."""

    from okto_pulse.core.ports.global_discovery_recovery_control import (
        recovery_attempt_id,
    )

    live = tmp_path / "global" / "discovery.lbug"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"complete-primary")
    live.with_name(live.name + ".wal").write_bytes(b"complete-wal")
    write_bootstrap_marker(live)

    shared = _coherent_adopt_state()

    def factory(path: Path):
        return _CandidateRuntime(path, shared)

    composed_live = _HoldClearLive(live)
    adapter = CommunityGlobalDiscoveryRecovery(
        global_runtime=composed_live,  # type: ignore[arg-type]
        graph_path_provider=lambda: live,
        runtime_factory=factory,  # type: ignore[arg-type]
        fence_check=lambda: None,
    )
    before = adapter.inspect_live_artifact()
    run_id, epoch = "gdr_r5wellformed", 1
    attempt_id = recovery_attempt_id(run_id, epoch)

    # Drive a real adoption holding the clear -> durable completed journal +
    # active pointer + marker still present.
    with pytest.raises(RuntimeError, match="hold-clear"):
        adapter.recover_and_cutover(
            run_id=run_id,
            epoch=epoch,
            attempt_id=attempt_id,
            expected_live_sha256=before.sha256,
            boards=_boards(),
        )
    assert bootstrap_marker_present(live) is True

    # Release the clear; reconcile the terminal truth -> clears marker, success.
    composed_live.hold = False
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
    clears_after_first = len(composed_live.successful_cutovers)
    assert clears_after_first == 1

    # Second reconcile: marker absent -> idempotent no-op, no additional clear.
    result2 = adapter.reconcile_attempt_terminal_truth(
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
        expected_live_sha256=before.sha256,
        boards=_boards(),
        fence_check=lambda: None,
    )
    assert result2 is not None and result2.outcome == "completed"
    assert bootstrap_marker_present(live) is False
    assert len(composed_live.successful_cutovers) == clears_after_first
