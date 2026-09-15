from __future__ import annotations

import json
from pathlib import Path

import pytest

from okto_pulse.community.adapters.global_discovery_layout import (
    GlobalDiscoveryLayoutError,
    active_pointer_path,
    generation_graph_path,
    resolve_active_graph_path,
    switch_active_generation,
    write_generation_manifest,
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


def _patch_fsync_directory_true(monkeypatch):
    """Pin every directory-fsync boundary True so an injected/inherited false is
    isolated (Windows dir fsync is naturally False)."""

    import okto_pulse.community.adapters.global_discovery_layout as layout_mod
    import okto_pulse.community.adapters.global_discovery_recovery as rec_mod

    monkeypatch.setattr(layout_mod, "fsync_directory", lambda _p: True)
    monkeypatch.setattr(rec_mod, "fsync_directory", lambda _p: True)


class _CloseSpyRuntime:
    def __init__(self, err: BaseException | None = None) -> None:
        self.err = err
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.err is not None:
            raise self.err






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
