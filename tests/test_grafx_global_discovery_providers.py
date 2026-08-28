from __future__ import annotations

import json
from pathlib import Path

import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.global_discovery_recovery import (
    GlobalDiscoveryBoardSeed,
    GlobalDiscoveryDigestSeed,
    GlobalDiscoveryRecovery,
)
from okto_pulse.core.kg.interfaces.global_discovery_runtime import (
    GlobalDiscoveryRuntime,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
)

from okto_pulse.community.adapters.global_discovery_layout import (
    GENERATION_MANIFEST_FILENAME,
    read_active_generation,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    CommunityGrafxGlobalDiscoveryRecovery,
    CommunityGrafxGlobalDiscoveryRecoveryError,
)
from okto_pulse.community.adapters.grafx_global_discovery_runtime import (
    CommunityGrafxGlobalDiscoveryRuntime,
)


def _vector(first: float = 1.0, second: float = 0.0) -> list[float]:
    return [first, second, *([0.0] * 382)]


class _DatabaseSlot:
    def __init__(self, legacy: Path) -> None:
        self.legacy = legacy
        self.database = None
        self.opened_path: Path | None = None
        self.resolutions = 0
        self.closes = 0

    def resolve(self):
        from okto_pulse.community.adapters.global_discovery_layout import (
            resolve_active_graph_path,
        )

        path = resolve_active_graph_path(self.legacy)
        self.resolutions += 1
        if self.database is None or self.database.closed or self.opened_path != path:
            if self.database is not None and not self.database.closed:
                self.database.close()
            self.database = connect(path, vector_exact_scan_threshold=4096)
            self.opened_path = path
        return self.database

    def close(self) -> None:
        self.closes += 1
        if self.database is not None and not self.database.closed:
            self.database.close()
        self.database = None
        self.opened_path = None


def _runtime(slot: _DatabaseSlot, fences: list[str] | None = None):
    events = fences if fences is not None else []
    return CommunityGrafxGlobalDiscoveryRuntime(
        slot.resolve,
        lambda: slot.legacy,
        slot.close,
        events.append,
    )


def _seed(runtime, *, board_id: str, digest_id: str, source_id: str) -> None:
    runtime.upsert_board_summary(
        board_id=board_id,
        name=board_id.upper(),
        summary=f"summary-{board_id}",
        summary_embedding=_vector(),
        decision_count=1,
        synced_at="2026-08-28T12:00:00Z",
    )
    runtime.upsert_decision_digest(
        digest_id=digest_id,
        board_id=board_id,
        original_node_id=source_id,
        title=f"title-{source_id}",
        summary=f"summary-{source_id}",
        node_type="Decision",
        graph_layer="canonical",
        embedding=_vector(),
        created_at="2026-08-28T12:00:00Z",
    )
    runtime.link_board_digest(board_id=board_id, digest_id=digest_id)


def _board_seed(board_id: str, source_id: str) -> GlobalDiscoveryBoardSeed:
    return GlobalDiscoveryBoardSeed(
        board_id=board_id,
        board_name=board_id.upper(),
        summary=f"summary-{board_id}",
        summary_embedding=tuple(_vector()),
        digests=(
            GlobalDiscoveryDigestSeed(
                original_node_id=source_id,
                title=f"title-{source_id}",
                summary=f"summary-{source_id}",
                node_type="Decision",
                graph_layer="canonical",
                source_artifact_ref=f"artifact:{source_id}",
                embedding=tuple(_vector()),
            ),
        ),
        source_inventory_hash=f"inventory:{board_id}",
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_grafx_global_providers_cover_exact_core_protocols(tmp_path: Path) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    runtime = _runtime(slot)
    recovery = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        lambda path: connect(path, vector_exact_scan_threshold=4096),
        slot.close,
        lambda _phase: None,
        snapshot_fingerprint_provider=lambda: "source-fingerprint",
    )

    assert isinstance(runtime, GlobalDiscoveryRuntime)
    assert isinstance(recovery, GlobalDiscoveryRecovery)


def test_runtime_all_19_methods_and_exhaustive_search_are_real(
    tmp_path: Path,
    monkeypatch,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    fences: list[str] = []
    runtime = _runtime(slot, fences)

    assert runtime.state().state is GraphRuntimeObservationState.CONFIRMED_ABSENT
    assert runtime.bootstrap().opened is True
    assert (
        runtime.state().state is GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE
    )
    assert runtime.ensure_layer_schema() == ()
    assert {"Board", "DecisionDigest", "CONTAINS_DECISION"}.issubset(
        runtime.list_schema_objects()
    )

    def lose_commit_fence(phase: str) -> None:
        if phase == "commit":
            raise RuntimeError("stale-generation")

    blocked = CommunityGrafxGlobalDiscoveryRuntime(
        slot.resolve,
        lambda: slot.legacy,
        slot.close,
        lose_commit_fence,
    )
    with pytest.raises(GraphError):
        blocked.upsert_board_summary(
            board_id="never-committed",
            name="never-committed",
            summary="never-committed",
            summary_embedding=_vector(),
            decision_count=0,
            synced_at="2026-08-28T12:00:00Z",
        )
    assert runtime.execute(
        "MATCH (b:Board {board_id: 'never-committed'}) RETURN count(b)"
    ).rows == ((0,),)

    _seed(runtime, board_id="board-a", digest_id="digest-a", source_id="source-a")
    _seed(runtime, board_id="board-b", digest_id="digest-b", source_id="source-b")
    hits = runtime.search_decision_digests(
        _vector(),
        board_ids=("board-a",),
        graph_layer="canonical",
        top_k=5,
        min_similarity=1.0,
        exhaustive=True,
    )
    assert [row["digest_id"] for row in hits] == ["digest-a"]
    with pytest.raises(GraphError):
        runtime.search_decision_digests(
            [1.0],
            board_ids=("board-a",),
            graph_layer="canonical",
            top_k=1,
            min_similarity=0.0,
            exhaustive=True,
        )

    statement = runtime.execute(
        "MATCH (d:DecisionDigest {id: $digest_id}) " "RETURN d.embedding, d.created_at",
        {"digest_id": "digest-a"},
    )
    assert isinstance(statement.rows[0][0], list)
    assert str(statement.rows[0][1]).endswith("Z")
    runtime.execute(
        "MATCH (d:DecisionDigest {id: $digest_id}) SET d.title = $title",
        {"digest_id": "digest-a", "title": "changed"},
    )

    assert (
        runtime.replace_decision_digest_identity(
            digest_id="digest-a",
            board_id="board-a",
            original_node_id="source-a",
            title="replaced",
            summary="replaced",
            node_type="Decision",
            graph_layer="canonical",
            embedding=_vector(),
            created_at="2026-08-28T12:00:00Z",
        )
        == 1
    )
    assert (
        runtime.normalize_board_digest_link(board_id="board-a", digest_id="digest-a")
        == 1
    )

    # A wrong but physically possible link is removed without deleting either digest.
    runtime.execute(
        "MATCH (b:Board {board_id: 'board-a'}), "
        "(d:DecisionDigest {id: 'digest-b'}) "
        "CREATE (b)-[:CONTAINS_DECISION]->(d)"
    )
    assert (
        runtime.delete_invalid_board_digest_links(
            board_id="board-a",
            expected_digest_ids=("digest-a",),
        )
        == 1
    )

    assert (
        runtime.delete_decision_digests_guarded(
            board_id="board-b",
            original_node_ids=("source-b",),
        )
        == 1
    )
    _seed(runtime, board_id="board-b", digest_id="digest-c", source_id="source-c")
    assert (
        runtime.delete_decision_digests_for_absent_sources(
            board_id="board-b",
            original_node_ids=("source-c",),
        )
        == 1
    )

    with runtime.post_write_verification_scope():
        runtime.flush_after_write_batch()
    assert slot.closes >= 2
    assert runtime.search_decision_digests(
        _vector(),
        board_ids=("board-a",),
        graph_layer="canonical",
        top_k=1,
        min_similarity=1.0,
        exhaustive=True,
    )
    runtime.close()
    assert "commit" in fences

    # Keep this method real while avoiding dependence on the canonical quarantine store.
    def quarantine(legacy, targets, *, reason):
        from okto_pulse.community.adapters.filesystem_erasure import (
            remove_contained_tree,
        )

        del reason
        for target in targets:
            remove_contained_tree(target, base_dir=legacy.parent)
        return len(targets)

    monkeypatch.setattr(
        CommunityGrafxGlobalDiscoveryRuntime,
        "_quarantine",
        staticmethod(quarantine),
    )
    purged = runtime.purge(reason="test")
    assert purged.removed is True
    assert runtime.state().state is GraphRuntimeObservationState.CONFIRMED_ABSENT


def test_runtime_privacy_rebuild_preserves_survivors_and_erases_target(
    tmp_path: Path,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    runtime = _runtime(slot)
    runtime.bootstrap()
    _seed(runtime, board_id="delete-me", digest_id="delete-d", source_id="delete-s")
    _seed(runtime, board_id="survivor", digest_id="survivor-d", source_id="survivor-s")

    receipt = runtime.erase_storage_for_privacy(
        board_id="delete-me",
        reason="privacy",
        survivor_board_ids=("survivor",),
    )

    assert receipt["verified_absent"] is True
    assert runtime.execute(
        "MATCH (b:Board) RETURN b.board_id ORDER BY b.board_id"
    ).rows == (("survivor",),)
    hits = runtime.search_decision_digests(
        _vector(),
        board_ids=("survivor", "delete-me"),
        graph_layer="canonical",
        top_k=5,
        min_similarity=1.0,
        exhaustive=True,
    )
    assert [row["digest_id"] for row in hits] == ["survivor-d"]
    slot.close()


def test_recovery_builds_authenticated_active_generation_and_is_idempotent(
    tmp_path: Path,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    runtime = _runtime(slot)
    runtime.bootstrap()
    slot.close()
    fences: list[str] = []
    recovery = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        lambda path: connect(path, vector_exact_scan_threshold=4096),
        slot.close,
        fences.append,
        snapshot_fingerprint_provider=lambda: "source-fingerprint",
    )
    before = recovery.inspect_live_artifact()
    boards = (_board_seed("board-a", "source-a"),)

    result = recovery.recover_and_cutover(
        run_id="run-a",
        epoch=1,
        attempt_id="attempt-a",
        expected_live_sha256=before.sha256,
        boards=boards,
        fence_check=lambda: None,
    )

    assert result.outcome == "completed"
    active = read_active_generation(slot.legacy)
    assert active is not None
    manifest = json.loads(
        (active.graph_path.parent / GENERATION_MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["candidate_sha256"] == result.candidate_sha256
    assert manifest["source_fingerprint"]
    assert (
        _runtime(slot).search_decision_digests(
            _vector(),
            board_ids=("board-a",),
            graph_layer="canonical",
            top_k=1,
            min_similarity=1.0,
            exhaustive=True,
        )[0]["id"]
        == "source-a"
    )
    slot.close()

    repeated = recovery.recover_and_cutover(
        run_id="run-a",
        epoch=1,
        attempt_id="attempt-a",
        expected_live_sha256=before.sha256,
        boards=boards,
        fence_check=lambda: None,
    )
    assert repeated.to_dict() == result.to_dict()
    assert "recovery_cutover" in fences


def test_recovery_adopts_a_complete_copy_without_mutating_live_primary(
    tmp_path: Path,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    runtime = _runtime(slot)
    runtime.bootstrap()
    _seed(runtime, board_id="old", digest_id="old-d", source_id="old-s")
    slot.close()
    legacy_bytes = _tree_bytes(slot.legacy)
    recovery = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        lambda path: connect(path, vector_exact_scan_threshold=4096),
        slot.close,
        lambda _phase: None,
        snapshot_fingerprint_provider=lambda: "source-fingerprint",
    )
    before = recovery.inspect_live_artifact()
    replacement = (_board_seed("replacement", "replacement-source"),)

    result = recovery.recover_and_cutover(
        run_id="run-adopt",
        epoch=1,
        attempt_id="attempt-adopt",
        expected_live_sha256=before.sha256,
        boards=replacement,
        fence_check=lambda: None,
    )

    active = read_active_generation(slot.legacy)
    assert active is not None
    assert active.graph_path != slot.legacy
    manifest = json.loads(
        (active.graph_path.parent / GENERATION_MANIFEST_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["kind"] == "grafx_global_discovery_recovery_adoption"
    assert manifest["candidate_sha256"] == result.candidate_sha256
    assert _tree_bytes(slot.legacy) == legacy_bytes
    active_runtime = _runtime(slot)
    assert active_runtime.execute(
        "MATCH (b:Board) RETURN b.board_id ORDER BY b.board_id"
    ).rows == (("old",),)
    slot.close()

    repeated = recovery.recover_and_cutover(
        run_id="run-adopt",
        epoch=1,
        attempt_id="attempt-adopt",
        expected_live_sha256=before.sha256,
        boards=replacement,
        fence_check=lambda: None,
    )
    assert repeated.to_dict() == result.to_dict()
    assert _tree_bytes(slot.legacy) == legacy_bytes


def test_recovery_mismatch_and_invalid_seed_leave_live_pointer_unchanged(
    tmp_path: Path,
) -> None:
    slot = _DatabaseSlot(tmp_path / "global.grafx")
    runtime = _runtime(slot)
    runtime.bootstrap()
    slot.close()
    recovery = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        lambda path: connect(path, vector_exact_scan_threshold=4096),
        slot.close,
        lambda _phase: None,
        snapshot_fingerprint_provider=lambda: "source-fingerprint",
    )
    before = recovery.inspect_live_artifact()

    with pytest.raises(CommunityGrafxGlobalDiscoveryRecoveryError) as mismatch:
        recovery.rebuild_candidate_and_cutover(
            run_id="run-mismatch",
            epoch=1,
            attempt_id="attempt-mismatch",
            expected_live_sha256="f" * 64,
            boards=(_board_seed("board-a", "source-a"),),
            fence_check=lambda: None,
        )
    assert mismatch.value.code == "global_discovery_live_snapshot_changed"
    assert read_active_generation(slot.legacy) is None
    assert recovery.inspect_live_artifact().sha256 == before.sha256

    invalid = _board_seed("board-a", "source-a")
    with pytest.raises(CommunityGrafxGlobalDiscoveryRecoveryError):
        recovery.rebuild_candidate_and_cutover(
            run_id="run-invalid",
            epoch=1,
            attempt_id="attempt-invalid",
            expected_live_sha256=before.sha256,
            boards=(invalid, invalid),
            fence_check=lambda: None,
        )
    assert read_active_generation(slot.legacy) is None
    assert recovery.current_snapshot_fingerprint() == "source-fingerprint"

    def fail_factory(_path: Path):
        raise OSError("candidate-open-refused")

    failed_candidate = CommunityGrafxGlobalDiscoveryRecovery(
        lambda: slot.legacy,
        fail_factory,
        slot.close,
        lambda _phase: None,
        snapshot_fingerprint_provider=lambda: "source-fingerprint",
    )
    with pytest.raises(CommunityGrafxGlobalDiscoveryRecoveryError) as factory_failure:
        failed_candidate.rebuild_candidate_and_cutover(
            run_id="run-factory-failure",
            epoch=1,
            attempt_id="attempt-factory-failure",
            expected_live_sha256=before.sha256,
            boards=(_board_seed("board-a", "source-a"),),
            fence_check=lambda: None,
        )
    assert factory_failure.value.code == "global_discovery_candidate_open_failed"
    assert read_active_generation(slot.legacy) is None
    assert recovery.inspect_live_artifact().sha256 == before.sha256
