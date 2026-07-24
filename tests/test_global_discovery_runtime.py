from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import ladybug
import pytest

from okto_pulse.community.adapters.board_graph_runtime import (
    CommunityBoardGraphRuntime,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
    _DIGEST_REPAIR_MAX_PRIMARY_DRAINS,
    _LifecycleReadWriteGate,
    _statement_requires_vector_extension,
)
from okto_pulse.community.adapters.ladybug_writer import writer_lease_is_active
from okto_pulse.community.adapters.ladybug_writer import ladybug_writer_scope
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
from okto_pulse.core.kg.global_discovery_writer import (
    GlobalDiscoveryWriterFenceLost,
    GlobalDiscoveryWriterLease,
)


_EMBEDDING = [0.0] * 384


class _AlwaysOwnedWriterLock:
    def is_owner(self, _board_id: str, _owner_token: str) -> bool:
        return True

    def release(self, *, board_id: str, owner_token: str) -> bool:
        del board_id, owner_token
        return True


@contextmanager
def under_global_safe_write(owner_token: str, operation: str):
    """Activate the same lease+barrier shape without filesystem test locks."""

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


def _bootstrap(runtime: CommunityGlobalDiscoveryRuntime, owner: str) -> None:
    with under_global_safe_write(owner, "test_global_discovery_bootstrap"):
        runtime.bootstrap()


def _seed_board(runtime: CommunityGlobalDiscoveryRuntime, board_id: str) -> None:
    with under_global_safe_write(
        f"seed-{board_id}", "test_global_discovery_seed_board"
    ):
        runtime.upsert_board_summary(
            board_id=board_id,
            name=board_id,
            summary="",
            summary_embedding=_EMBEDDING,
            decision_count=0,
            synced_at="2026-07-15T12:00:00",
        )


def _digest_values(board_id: str, *, title: str) -> dict[str, object]:
    return {
        "digest_id": f"dd_{board_id[:8]}_decision-a",
        "board_id": board_id,
        "original_node_id": "decision-a",
        "title": title,
        "summary": title,
        "node_type": "Decision",
        "graph_layer": "canonical",
        "embedding": _EMBEDDING,
        "created_at": "2026-07-15T12:00:00",
    }


def test_privacy_erasure_rewrites_global_and_removes_old_generations(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    _bootstrap(runtime, "privacy-bootstrap")
    _seed_board(runtime, "board-privacy")
    runtime.close()
    inactive = (
        graph_path.parent / "discovery.generations" / "gdr_inactive" / "discovery.lbug"
    )
    inactive.parent.mkdir(parents=True)
    inactive.write_bytes(b"historical-board-content")
    recovery_copy = (
        graph_path.parent
        / "quarantine"
        / "global-discovery"
        / "attempt-1"
        / "original"
        / "discovery.lbug"
    )
    recovery_copy.parent.mkdir(parents=True)
    recovery_copy.write_bytes(b"quarantined-board-content")

    with under_global_safe_write(
        "privacy-erasure",
        "test_global_discovery_privacy_erasure",
    ):
        runtime.execute(
            "MATCH (b:Board {board_id: $board_id}) DETACH DELETE b",
            {"board_id": "board-privacy"},
        )
        result = runtime.erase_storage_for_privacy(
            board_id="board-privacy",
            reason="right_to_erasure",
        )

    assert result["verified_absent"] is True
    assert result["status"] == "purged"
    assert result["survivors_restored"]["boards"] == 0
    assert graph_path.exists()
    assert not inactive.exists()
    assert not recovery_copy.exists()
    assert runtime.state().exists is True
    assert not list(tmp_path.glob(".global-privacy-survivors-*.json"))


def test_privacy_erasure_preserves_other_board_discovery_and_relations(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    target_board = "board-delete"
    survivor_board = "board-survivor"
    target_digest = _digest_values(target_board, title="delete me")
    survivor_digest = _digest_values(survivor_board, title="keep me")
    try:
        _bootstrap(runtime, "privacy-survivor-bootstrap")
        _seed_board(runtime, target_board)
        _seed_board(runtime, survivor_board)
        with under_global_safe_write(
            "privacy-survivor-erasure",
            "test_global_discovery_privacy_survivor",
        ):
            runtime.upsert_decision_digest(**target_digest)
            runtime.upsert_decision_digest(**survivor_digest)
            runtime.link_board_digest(
                board_id=target_board,
                digest_id=str(target_digest["digest_id"]),
            )
            runtime.link_board_digest(
                board_id=survivor_board,
                digest_id=str(survivor_digest["digest_id"]),
            )
            runtime.execute(
                "CREATE (e:Entity {id: 'survivor-entity', "
                "canonical_name: 'Survivor Entity', aliases: '', "
                "embedding: $embedding, mention_count: 1})",
                {"embedding": _EMBEDDING},
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: 'survivor-entity'}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e)",
                {"digest_id": survivor_digest["digest_id"]},
            )
            # The Core cascade runs before physical privacy erasure.
            runtime.execute(
                "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id DETACH DELETE d",
                {"board_id": target_board},
            )
            runtime.execute(
                "MATCH (b:Board {board_id: $board_id}) DETACH DELETE b",
                {"board_id": target_board},
            )

            result = runtime.erase_storage_for_privacy(
                board_id=target_board,
                reason="right_to_erasure",
            )
            hits = runtime.search_decision_digests(
                _EMBEDDING,
                board_ids=(survivor_board,),
                graph_layer="canonical",
                top_k=10,
                min_similarity=0.0,
                exhaustive=True,
            )
            mentions = runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id})-"
                "[r:DECISION_MENTIONS_ENTITY]->"
                "(e:Entity {id: 'survivor-entity'}) RETURN count(r)",
                {"digest_id": survivor_digest["digest_id"]},
            ).rows
            deleted = runtime.execute(
                "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id RETURN count(d)",
                {"board_id": target_board},
            ).rows

        assert result["verified_absent"] is True
        assert result["survivors_restored"]["boards"] == 1
        assert result["survivors_restored"]["digests"] == 1
        assert [row["board_id"] for row in hits] == [survivor_board]
        assert mentions == ((1,),)
        assert deleted == ((0,),)
    finally:
        runtime.close()


def test_privacy_erasure_stale_journal_cannot_resurrect_deleted_board(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    try:
        _bootstrap(runtime, "privacy-stale-journal-bootstrap")
        _seed_board(runtime, "board-b")
        _seed_board(runtime, "board-c")
        with under_global_safe_write(
            "privacy-stale-journal",
            "test_privacy_stale_journal",
        ):
            snapshot, journal_path = runtime._load_or_create_privacy_survivor_snapshot(
                board_id="board-a",
                storage_root=tmp_path,
                survivor_board_ids=("board-b", "board-c"),
            )
            assert snapshot["survivor_board_ids"] == ["board-b", "board-c"]
            runtime.execute(
                "MATCH (b:Board {board_id: $board_id}) DETACH DELETE b",
                {"board_id": "board-b"},
            )

            result = runtime.erase_storage_for_privacy(
                board_id="board-a",
                reason="retry_after_board_b_erasure",
                survivor_board_ids=("board-c",),
            )
            rows = runtime.execute(
                "MATCH (b:Board) RETURN b.board_id ORDER BY b.board_id"
            ).rows

        assert rows == (("board-c",),)
        assert result["survivors_restored"]["boards"] == 1
        assert not journal_path.exists()
    finally:
        runtime.close()


def test_privacy_erasure_redacts_shared_aggregate_properties(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    survivor = "board-survivor-redacted"
    digest = _digest_values(survivor, title="safe survivor digest")
    secret = "target-only-secret-alias"
    try:
        _bootstrap(runtime, "privacy-redaction-bootstrap")
        _seed_board(runtime, survivor)
        with under_global_safe_write(
            "privacy-redaction",
            "test_privacy_redaction",
        ):
            runtime.upsert_decision_digest(**digest)
            runtime.link_board_digest(
                board_id=survivor,
                digest_id=str(digest["digest_id"]),
            )
            runtime.execute(
                "CREATE (e:Entity {id: 'shared-entity', "
                "canonical_name: 'Shared Entity', aliases: $aliases, "
                "embedding: $embedding, mention_count: 9})",
                {"aliases": secret, "embedding": _EMBEDDING},
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: 'shared-entity'}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e)",
                {"digest_id": digest["digest_id"]},
            )

            runtime.erase_storage_for_privacy(
                board_id="board-deleted",
                reason="redact_cross_board_aggregates",
                survivor_board_ids=(survivor,),
            )
            entity = runtime.execute(
                "MATCH (e:Entity {id: 'shared-entity'}) "
                "RETURN e.canonical_name, e.aliases, e.embedding, "
                "e.mention_count"
            ).rows
            mentions = runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id})-"
                "[r:DECISION_MENTIONS_ENTITY]->"
                "(:Entity {id: 'shared-entity'}) RETURN count(r)",
                {"digest_id": digest["digest_id"]},
            ).rows

        assert entity == ((None, None, None, None),)
        assert mentions == ((1,),)
        runtime.close()
        assert all(
            secret.encode("utf-8") not in path.read_bytes()
            for path in graph_path.parent.rglob("*")
            if path.is_file()
        )
    finally:
        runtime.close()


def test_privacy_erasure_rejects_corrupt_survivor_journal_without_rewrite(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    try:
        _bootstrap(runtime, "privacy-corrupt-journal-bootstrap")
        _seed_board(runtime, "board-safe")
        journal_path = runtime._privacy_snapshot_path(
            tmp_path,
            "board-deleted",
        )
        journal_path.write_text('{"version":3,"rows":', encoding="utf-8")

        with under_global_safe_write(
            "privacy-corrupt-journal",
            "test_privacy_corrupt_journal",
        ):
            with pytest.raises(
                RuntimeError,
                match="privacy_survivor_snapshot_invalid",
            ):
                runtime.erase_storage_for_privacy(
                    board_id="board-deleted",
                    reason="corrupt_journal",
                    survivor_board_ids=("board-safe",),
                )
            rows = runtime.execute("MATCH (b:Board) RETURN b.board_id").rows

        assert rows == (("board-safe",),)
        assert journal_path.exists()
        assert graph_path.exists()
    finally:
        runtime.close()


def test_privacy_erasure_refuses_a_noncanonical_global_root(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "must-survive.txt"
    sentinel.write_text("safe", encoding="utf-8")
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: tmp_path / "discovery.lbug",
    )

    with under_global_safe_write(
        "privacy-invalid-root",
        "test_global_discovery_privacy_invalid_root",
    ):
        with pytest.raises(
            RuntimeError,
            match="global_discovery_privacy_erasure_root_invalid",
        ):
            runtime.erase_storage_for_privacy(
                board_id="board-privacy",
                reason="right_to_erasure",
            )

    assert sentinel.read_text(encoding="utf-8") == "safe"


def _create_same_primary_key_rows_with_multi_writes(
    *,
    graph_path: Path,
    first: dict[str, object],
    second: dict[str, object],
) -> tuple[tuple[tuple[object, ...], ...], tuple[tuple[object, ...], ...]]:
    """Commit the historical race shape with two real Ladybug writers."""

    database = ladybug.Database(graph_path, enable_multi_writes=True)
    first_connection = ladybug.Connection(database)
    second_connection = ladybug.Connection(database)
    create = (
        "CREATE (d:DecisionDigest {"
        "id: $digest_id, board_id: $board_id, "
        "original_node_id: $original_node_id, title: $title, "
        "one_line_summary: $summary, node_type: $node_type, "
        "graph_layer: $graph_layer, embedding: $embedding, "
        "created_at: timestamp($created_at)})"
    )

    def _rows(connection, statement: str, params: dict[str, object]):
        result = connection.execute(statement, params)
        materialized: list[tuple[object, ...]] = []
        while result.has_next():
            materialized.append(tuple(result.get_next()))
        return tuple(materialized)

    try:
        for connection in (first_connection, second_connection):
            connection.execute("LOAD VECTOR")
            connection.execute("BEGIN TRANSACTION")
        first_connection.execute(create, first)
        second_connection.execute(create, second)
        first_connection.execute("COMMIT")
        second_connection.execute("COMMIT")

        lookup = _rows(
            first_connection,
            "MATCH (d:DecisionDigest {id: $digest_id}) RETURN d.id, d.title",
            {"digest_id": first["digest_id"]},
        )
        scan = _rows(
            first_connection,
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id RETURN d.id, d.title",
            {"digest_id": first["digest_id"]},
        )
        return lookup, scan
    finally:
        first_connection.close()
        second_connection.close()
        database.close()


class _SyntheticLiteralPrimaryRuntime(CommunityGlobalDiscoveryRuntime):
    """Model a PK-index row hidden from semantic and coalesce scans."""

    def __init__(
        self,
        *,
        graph_path: Path,
        literal_identity: tuple[object, object, object],
        relationship_kind: str | None = None,
    ) -> None:
        super().__init__(graph_path_provider=lambda: graph_path)
        self.literal_identity = literal_identity
        self.relationship_kind = relationship_kind
        self.literal_lookups = 0
        self.literal_deletes = 0
        self.relationship_preflights: list[str] = []
        self.staging_creates = 0

    @staticmethod
    def _literal_relationship_kind(statement: str) -> str | None:
        if "DECISION_MENTIONS_ENTITY" in statement:
            return "mentions"
        if "DECISION_DERIVES_FROM" not in statement:
            return None
        if statement.startswith("MATCH (:DecisionDigest)"):
            return "incoming"
        return "outgoing"

    def execute(self, statement, params=None):
        if statement.startswith("CREATE (d:DecisionDigest"):
            raise RuntimeError("Found duplicated primary key value dd_literal")
        if "CREATE (staging:DecisionDigest" in statement:
            self.staging_creates += 1
        if (
            "MATCH (d:DecisionDigest {id: $digest_id}) "
            "RETURN d.id, d.board_id, d.original_node_id" in statement
        ):
            self.literal_lookups += 1
            return GraphStatementResult(rows=(self.literal_identity,))
        relationship_kind = self._literal_relationship_kind(statement)
        if relationship_kind is not None and "{id: $digest_id}" in statement:
            self.relationship_preflights.append(relationship_kind)
            count = int(relationship_kind == self.relationship_kind)
            return GraphStatementResult(rows=((count,),))
        if statement.startswith(
            "MATCH (d:DecisionDigest {id: $digest_id}) DETACH DELETE d"
        ):
            self.literal_deletes += 1
            raise AssertionError("literal PK ghosts require rebuild, not DELETE")
        return super().execute(statement, params)


class _RecordingBoardGraphRuntime(CommunityBoardGraphRuntime):
    def __init__(self) -> None:
        self.install_flags: list[bool] = []

    def load_vector_extension(self, conn, *, install: bool = True) -> None:
        self.install_flags.append(install)
        super().load_vector_extension(conn, install=install)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("MATCH (d:DecisionDigest) RETURN d.id", False),
        ("MATCH (d:DecisionDigest) RETURN d.embedding", True),
        (
            "CALL QUERY_VECTOR_INDEX('DecisionDigest', 'idx', $v, 1) RETURN node.id",
            True,
        ),
        (
            "MATCH (d:DecisionDigest) WHERE d.title = 'embedding' RETURN d.id",
            False,
        ),
        (
            'MATCH (d:DecisionDigest) RETURN d.id, "VECTOR_INDEX"',
            False,
        ),
        (
            "MATCH (d:DecisionDigest) RETURN d.id // QUERY_VECTOR_INDEX embedding",
            False,
        ),
        (
            "MATCH (d:DecisionDigest) /* embedding */ RETURN d.id",
            False,
        ),
        (
            "CREATE (:Board {board_id: 'embedding'})",
            True,
        ),
    ],
)
def test_global_statement_vector_classification(
    statement: str,
    expected: bool,
) -> None:
    assert _statement_requires_vector_extension(statement) is expected


def test_vector_read_holds_writer_lease_for_mutating_load() -> None:
    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.lease_states: list[bool] = []

        def _execute_with_writer_lease(self, statement, params):
            del statement, params
            self.lease_states.append(writer_lease_is_active())
            return GraphStatementResult()

        def _database_is_open(self) -> bool:
            return True

        def _ensure_database_open_with_writer_lease(self) -> None:
            return None

    runtime = _ObservedRuntime()
    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        runtime.execute("MATCH (d:DecisionDigest) RETURN d.embedding")
    with under_global_safe_write("vector-read", "test-vector-load"):
        runtime.execute("MATCH (d:DecisionDigest) RETURN d.embedding")
    runtime.execute("MATCH (d:DecisionDigest) RETURN d.id")
    with pytest.raises(GlobalDiscoveryWriterFenceLost):
        runtime.upsert_board_summary(
            board_id="unguarded-board",
            name="unguarded",
            summary="must fail before CREATE",
            summary_embedding=[0.0],
            decision_count=0,
            synced_at="2026-07-17T12:00:00",
        )

    assert runtime.lease_states == [True, False, True]


def test_open_native_loads_vector_without_install_after_bootstrap(
    tmp_path: Path,
) -> None:
    graph_runtime = _RecordingBoardGraphRuntime()
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_runtime=graph_runtime,
        graph_path_provider=lambda: graph_path,
    )
    try:
        _bootstrap(runtime, "hot-open-install-flag")
        runtime.execute("CALL SHOW_TABLES() RETURN name")

        assert graph_runtime.install_flags == [True]

        with under_global_safe_write(
            "vector-load-after-bootstrap",
            "test-vector-load-after-bootstrap",
        ):
            runtime.execute("MATCH (d:DecisionDigest) RETURN d.embedding LIMIT 1")
        assert graph_runtime.install_flags == [True, False]
    finally:
        runtime.close()


def test_bootstrap_is_idempotent_after_runtime_handle_is_warm(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    try:
        _bootstrap(runtime, "warm-bootstrap-initial")
        runtime.execute("CALL SHOW_TABLES() RETURN name")
        assert runtime._database_is_open()

        _bootstrap(runtime, "warm-bootstrap-repeat")
        assert not runtime._database_is_open()
        assert "DecisionDigest" in {
            str(row[0])
            for row in runtime.execute("CALL SHOW_TABLES() RETURN name").rows
        }
    finally:
        runtime.close()


def test_non_vector_fresh_reads_do_not_grow_real_ladybug_wal(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    wal_path = graph_path.with_name(f"{graph_path.name}.wal")
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    try:
        _bootstrap(runtime, "non-vector-wal-bootstrap")
        runtime.close()

        # Exercise the public cold-open path; _open_native intentionally no
        # longer bootstraps/reopens outside writer -> lifecycle-exclusive.
        runtime.execute("CALL SHOW_TABLES() RETURN name")
        before = wal_path.stat().st_size if wal_path.exists() else 0

        statements = (
            "CALL SHOW_TABLES() RETURN name",
            "CALL SHOW_TABLES() RETURN name, 'embedding' AS marker",
            "CALL SHOW_TABLES() RETURN name // QUERY_VECTOR_INDEX embedding",
        )
        for statement in statements:
            runtime.execute(statement)

        after = wal_path.stat().st_size if wal_path.exists() else 0
        assert after == before
    finally:
        runtime.close()


def test_upsert_decision_digest_serializes_check_create_with_real_ladybug(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"

    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        active = 0
        max_active = 0
        observation_lock = threading.Lock()

        def _upsert_decision_digest_with_writer_lease(self, **values):
            with self.observation_lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                # Widen the historical check-create race window.  The outer
                # Ladybug lease must keep the second caller out of this method.
                time.sleep(0.05)
                return super()._upsert_decision_digest_with_writer_lease(**values)
            finally:
                with self.observation_lock:
                    self.active -= 1

    runtime = _ObservedRuntime(graph_path_provider=lambda: graph_path)
    board_id = "board-concurrent-upsert"
    start = threading.Barrier(2)

    def _upsert(index: int) -> str:
        start.wait(timeout=5)
        with under_global_safe_write(
            f"concurrent-upsert-{index}", "test_concurrent_digest_upsert"
        ):
            return runtime.upsert_decision_digest(
                **_digest_values(board_id, title=f"title-{index}")
            )

    try:
        _bootstrap(runtime, "concurrent-upsert-bootstrap")
        _seed_board(runtime, board_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(_upsert, (1, 2)))

        rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "RETURN d.id, d.graph_layer",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows

        assert outcomes == ["created", "updated"]
        assert runtime.max_active == 1
        assert rows == ((f"dd_{board_id[:8]}_decision-a", "canonical"),)
    finally:
        runtime.close()


def test_upsert_recovers_real_same_pk_cross_identity_set_collision(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"

    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__(graph_path_provider=lambda: graph_path)
            self.replacement_calls = 0

        def replace_decision_digest_identity(self, **values):
            self.replacement_calls += 1
            return super().replace_decision_digest_identity(**values)

    runtime = _ObservedRuntime()
    board_id = "board-upsert-set-collision"
    foreign_board_id = "board-upsert-set-foreign"
    values = _digest_values(board_id, title="owned-before")
    foreign = {
        **values,
        "board_id": foreign_board_id,
        "original_node_id": "foreign-decision",
        "title": "foreign-before",
        "summary": "foreign-before",
    }
    try:
        _bootstrap(runtime, "upsert-set-collision-bootstrap")
        _seed_board(runtime, board_id)
        _seed_board(runtime, foreign_board_id)
        runtime.close()

        primary_lookup, full_scan = _create_same_primary_key_rows_with_multi_writes(
            graph_path=graph_path,
            first=foreign,
            second=values,
        )
        assert len(primary_lookup) == 1
        assert len(full_scan) == 2

        with under_global_safe_write(
            "upsert-set-collision-repair", "test_upsert_set_collision"
        ):
            runtime.execute(
                "MATCH (d:DecisionDigest) "
                "WHERE d.board_id = $board_id "
                "AND d.original_node_id = $original_node_id "
                "SET d.title = $title",
                {
                    "board_id": board_id,
                    "original_node_id": "decision-a",
                    "title": "silent-set-left-duplicate",
                },
            )
            silent_rows = runtime.execute(
                "MATCH (d:DecisionDigest) "
                "WHERE coalesce(d.id, '') = $digest_id "
                "RETURN d.board_id, d.title",
                {"digest_id": values["digest_id"]},
            ).rows
            assert len(silent_rows) == 2
            assert (board_id, "silent-set-left-duplicate") in silent_rows
            with pytest.raises(
                RuntimeError,
                match="digest_upsert_verification_failed: .*canonical_rows=2",
            ):
                runtime._verify_decision_digest_identity(
                    digest_id=str(values["digest_id"]),
                    board_id=board_id,
                    original_node_id="decision-a",
                    graph_layer="canonical",
                )

            outcome = runtime.upsert_decision_digest(
                **{**values, "title": "owned-after", "summary": "owned-after"}
            )

        rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id "
            "RETURN d.board_id, d.original_node_id, d.title",
            {"digest_id": values["digest_id"]},
        ).rows
        assert outcome == "updated"
        assert runtime.replacement_calls == 1
        assert rows == ((board_id, "decision-a", "owned-after"),)
    finally:
        runtime.close()


def test_upsert_recovers_semantic_miss_with_existing_cross_identity_pk(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"

    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__(graph_path_provider=lambda: graph_path)
            self.replacement_calls = 0

        def replace_decision_digest_identity(self, **values):
            self.replacement_calls += 1
            return super().replace_decision_digest_identity(**values)

    runtime = _ObservedRuntime()
    board_id = "board-upsert-create-collision"
    foreign_board_id = "board-upsert-create-foreign"
    values = _digest_values(board_id, title="owned-after")
    foreign = {
        **values,
        "board_id": foreign_board_id,
        "original_node_id": "foreign-decision",
        "title": "foreign-before",
        "summary": "foreign-before",
    }
    try:
        _bootstrap(runtime, "upsert-create-collision-bootstrap")
        _seed_board(runtime, board_id)
        _seed_board(runtime, foreign_board_id)
        with under_global_safe_write(
            "upsert-create-collision-seed", "test_upsert_create_collision"
        ):
            assert runtime.upsert_decision_digest(**foreign) == "created"
            with pytest.raises(Exception, match="duplicated primary key"):
                runtime.execute(
                    "CREATE (d:DecisionDigest {"
                    "id: $digest_id, board_id: $board_id, "
                    "original_node_id: $original_node_id, title: $title, "
                    "one_line_summary: $summary, node_type: $node_type, "
                    "graph_layer: $graph_layer, embedding: $embedding, "
                    "created_at: timestamp($created_at)})",
                    values,
                )
            outcome = runtime.upsert_decision_digest(**values)

        rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id "
            "RETURN d.board_id, d.original_node_id, d.title",
            {"digest_id": values["digest_id"]},
        ).rows
        assert outcome == "updated"
        assert runtime.replacement_calls == 1
        assert rows == ((board_id, "decision-a", "owned-after"),)
    finally:
        runtime.close()


def test_replace_missing_identity_remains_fail_closed(tmp_path: Path) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-missing-identity"
    values = _digest_values(board_id, title="missing")
    try:
        _bootstrap(runtime, "missing-identity-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "missing-identity-replace", "test_missing_identity_replace"
        ):
            with pytest.raises(
                RuntimeError,
                match=(
                    "global_discovery.digest_replace_failed: board or digest "
                    "identity was not found"
                ),
            ):
                runtime.replace_decision_digest_identity(**values)

        rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id RETURN d.id",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        assert rows == ()
    finally:
        runtime.close()


def test_upsert_create_duplicate_with_empty_scans_stages_irreparable_ghost(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"

    class _InvisibleGhostPrimaryRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__(graph_path_provider=lambda: graph_path)
            self.direct_create_attempts = 0
            self.staging_creates = 0
            self.swap_attempts = 0

        def execute(self, statement, params=None):
            if statement.startswith("CREATE (d:DecisionDigest"):
                self.direct_create_attempts += 1
                raise RuntimeError("Found duplicated primary key value dd_ghost")
            if "CREATE (staging:DecisionDigest" in statement:
                self.staging_creates += 1
            if (
                "MATCH (staging:DecisionDigest)" in statement
                and "CREATE (replacement:DecisionDigest" in statement
            ):
                self.swap_attempts += 1
                raise RuntimeError("Found duplicated primary key value dd_ghost")
            return super().execute(statement, params)

    runtime = _InvisibleGhostPrimaryRuntime()
    board_id = "board-invisible-ghost"
    values = _digest_values(board_id, title="recover-invisible-ghost")
    try:
        _bootstrap(runtime, "invisible-ghost-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "invisible-ghost-upsert", "test_invisible_ghost_upsert"
        ):
            with pytest.raises(
                RuntimeError,
                match=(
                    "global_discovery.digest_pk_index_irreparable .*"
                    "reason=literal_lookup_empty .*"
                    "staging_preserved=true .*"
                    "recovery=global_discovery_rebuild_then_requeue"
                ),
            ):
                runtime.upsert_decision_digest(**values)

        semantic_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id RETURN d.id",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        target_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id RETURN d.id",
            {"digest_id": values["digest_id"]},
        ).rows
        staging_links = runtime.execute(
            "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
            "WHERE b.board_id = $board_id "
            "AND d.original_node_id = $original_node_id RETURN count(r)",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows

        assert runtime.direct_create_attempts == 1
        assert runtime.staging_creates == 1
        assert runtime.swap_attempts == 0
        assert len(semantic_rows) == 1
        assert str(semantic_rows[0][0]).startswith("dd_repair_")
        assert target_rows == ()
        assert staging_links == ((1,),)
    finally:
        runtime.close()


def test_upsert_literal_pk_ghost_requires_rebuild_without_mutation(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    board_id = "board-literal-convergence"
    values = _digest_values(board_id, title="literal-after")
    runtime = _SyntheticLiteralPrimaryRuntime(
        graph_path=graph_path,
        literal_identity=(
            values["digest_id"],
            board_id,
            values["original_node_id"],
        ),
    )
    try:
        _bootstrap(runtime, "literal-convergence-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "literal-convergence-upsert", "test_literal_convergence_upsert"
        ):
            with pytest.raises(
                RuntimeError,
                match=(
                    "global_discovery.digest_pk_index_irreparable .*"
                    "reason=literal_rebuild_required .*"
                    "staging_preserved=true .*"
                    "recovery=global_discovery_rebuild_then_requeue"
                ),
            ):
                runtime.upsert_decision_digest(**values)

        rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "RETURN d.id",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        target_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id RETURN d.id",
            {"digest_id": values["digest_id"]},
        ).rows
        links = runtime.execute(
            "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
            "WHERE b.board_id = $board_id "
            "AND d.original_node_id = $original_node_id RETURN count(r)",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows

        assert runtime.staging_creates == 1
        assert runtime.literal_lookups == 1
        assert runtime.literal_deletes == 0
        assert runtime.relationship_preflights == [
            "mentions",
            "outgoing",
            "incoming",
        ]
        assert len(rows) == 1
        assert str(rows[0][0]).startswith("dd_repair_")
        assert target_rows == ()
        assert links == ((1,),)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "relationship_kind",
    ["mentions", "outgoing", "incoming"],
)
def test_literal_pk_recovery_blocks_derived_edges_before_delete(
    tmp_path: Path,
    relationship_kind: str,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    board_id = f"board-literal-edge-{relationship_kind}"
    values = _digest_values(board_id, title="must-preserve")
    runtime = _SyntheticLiteralPrimaryRuntime(
        graph_path=graph_path,
        literal_identity=(
            values["digest_id"],
            board_id,
            values["original_node_id"],
        ),
        relationship_kind=relationship_kind,
    )
    try:
        _bootstrap(runtime, f"literal-edge-{relationship_kind}-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            f"literal-edge-{relationship_kind}-upsert",
            "test_literal_edge_upsert",
        ):
            with pytest.raises(
                RuntimeError,
                match=(
                    "global_discovery.digest_pk_index_irreparable .*"
                    "reason=literal_relationships_present .*"
                    "staging_preserved=true .*"
                    "recovery=global_discovery_rebuild_then_requeue"
                ),
            ):
                runtime.upsert_decision_digest(**values)

        staging_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id RETURN d.id",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        assert runtime.literal_deletes == 0
        assert set(runtime.relationship_preflights) == {
            "mentions",
            "outgoing",
            "incoming",
        }
        assert len(staging_rows) == 1
        assert str(staging_rows[0][0]).startswith("dd_repair_")
    finally:
        runtime.close()


@pytest.mark.parametrize("identity_mismatch", ["board", "original_node"])
def test_literal_pk_recovery_rejects_cross_identity_before_delete(
    tmp_path: Path,
    identity_mismatch: str,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    board_id = f"board-literal-identity-{identity_mismatch}"
    values = _digest_values(board_id, title="must-preserve")
    observed_board_id = "foreign-board" if identity_mismatch == "board" else board_id
    observed_original_node_id = (
        "foreign-decision"
        if identity_mismatch == "original_node"
        else values["original_node_id"]
    )
    runtime = _SyntheticLiteralPrimaryRuntime(
        graph_path=graph_path,
        literal_identity=(
            values["digest_id"],
            observed_board_id,
            observed_original_node_id,
        ),
    )
    try:
        _bootstrap(runtime, f"literal-identity-{identity_mismatch}-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            f"literal-identity-{identity_mismatch}-upsert",
            "test_literal_identity_upsert",
        ):
            with pytest.raises(
                RuntimeError,
                match=(
                    "global_discovery.digest_pk_index_irreparable .*"
                    "reason=literal_identity_mismatch .*"
                    "staging_preserved=true .*"
                    "recovery=global_discovery_rebuild_then_requeue"
                ),
            ):
                runtime.upsert_decision_digest(**values)

        staging_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id RETURN d.id",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        assert runtime.literal_deletes == 0
        assert runtime.relationship_preflights == []
        assert len(staging_rows) == 1
        assert str(staging_rows[0][0]).startswith("dd_repair_")
    finally:
        runtime.close()


def test_upsert_create_non_duplicate_error_propagates_without_recovery(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    create_error = RuntimeError("simulated storage failure")

    class _CreateFailureRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__(graph_path_provider=lambda: graph_path)
            self.replacement_calls = 0

        def execute(self, statement, params=None):
            if statement.startswith("CREATE (d:DecisionDigest"):
                raise create_error
            return super().execute(statement, params)

        def replace_decision_digest_identity(self, **values):
            del values
            self.replacement_calls += 1
            raise AssertionError("non-PK errors must not enter recovery")

    runtime = _CreateFailureRuntime()
    board_id = "board-create-storage-failure"
    try:
        _bootstrap(runtime, "create-storage-failure-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "create-storage-failure-upsert", "test_create_storage_failure"
        ):
            with pytest.raises(RuntimeError) as exc_info:
                runtime.upsert_decision_digest(
                    **_digest_values(board_id, title="must-fail")
                )

        assert exc_info.value is create_error
        assert runtime.replacement_calls == 0
    finally:
        runtime.close()


def test_board_summary_absolute_count_is_idempotent_across_retry(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-summary-retry"
    try:
        _bootstrap(runtime, "summary-retry-bootstrap")
        with under_global_safe_write(
            "summary-retry-upserts", "test_summary_retry_idempotence"
        ):
            for _retry in range(2):
                runtime.upsert_board_summary(
                    board_id=board_id,
                    name=board_id,
                    summary="",
                    summary_embedding=_EMBEDDING,
                    decision_count=7,
                    synced_at="2026-07-15T12:00:00",
                )

        rows = runtime.execute(
            "MATCH (b:Board {board_id: $board_id}) RETURN b.decision_count",
            {"board_id": board_id},
        ).rows
        assert rows == ((7,),)
    finally:
        runtime.close()


@pytest.mark.parametrize("operation", ["close", "flush", "bootstrap"])
def test_long_read_blocks_database_lifecycle_operation(operation: str) -> None:
    read_entered = threading.Event()
    release_read = threading.Event()
    lifecycle_called = threading.Event()
    lifecycle_entered = threading.Event()

    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        def _execute_with_writer_lease(self, statement, params):
            del statement, params
            read_entered.set()
            assert release_read.wait(timeout=5)
            return GraphStatementResult()

        def _close_with_writer_lease(self) -> None:
            lifecycle_entered.set()

        def _flush_after_write_batch_with_writer_lease(self) -> None:
            lifecycle_entered.set()

        def _bootstrap_with_writer_lease(self):
            lifecycle_entered.set()
            return object()

        def _database_is_open(self) -> bool:
            return True

    runtime = _ObservedRuntime()

    def _run_lifecycle() -> None:
        lifecycle_called.set()
        if operation == "close":
            runtime.close()
        else:
            with under_global_safe_write(
                f"lifecycle-{operation}",
                f"test-lifecycle-{operation}",
            ):
                if operation == "flush":
                    runtime.flush_after_write_batch()
                else:
                    runtime.bootstrap()

    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(
            runtime.execute,
            "MATCH (d:DecisionDigest) RETURN d.id",
        )
        assert read_entered.wait(timeout=5)
        lifecycle_future = pool.submit(_run_lifecycle)
        assert lifecycle_called.wait(timeout=5)

        # The lifecycle implementation is beyond the exclusive gate and cannot
        # run while execute() is still materializing from its borrowed handle.
        assert not lifecycle_entered.wait(timeout=0.1)
        release_read.set()

        read_future.result(timeout=5)
        lifecycle_future.result(timeout=5)

    assert lifecycle_entered.is_set()


def test_first_open_read_uses_writer_then_exclusive_before_shared() -> None:
    ensure_entered = threading.Event()
    release_ensure = threading.Event()
    close_entered = threading.Event()

    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        def _database_is_open(self) -> bool:
            return False

        def _ensure_database_open_with_writer_lease(self) -> None:
            assert writer_lease_is_active()
            ensure_entered.set()
            assert release_ensure.wait(timeout=5)

        def _execute_with_writer_lease(self, statement, params):
            del statement, params
            return GraphStatementResult()

        def _close_with_writer_lease(self) -> None:
            close_entered.set()

    runtime = _ObservedRuntime()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(
            runtime.execute,
            "MATCH (d:DecisionDigest) RETURN d.id",
        )
        assert ensure_entered.wait(timeout=5)
        close_future = pool.submit(runtime.close)

        # close waits for the writer lease; it cannot hold lifecycle-exclusive
        # while the cold reader still owns writer and is opening the Database.
        assert not close_entered.wait(timeout=0.1)
        release_ensure.set()
        read_future.result(timeout=5)
        close_future.result(timeout=5)

    assert close_entered.is_set()


def test_post_write_verification_scope_keeps_fresh_reads_exclusive() -> None:
    scope_entered = threading.Event()
    release_scope = threading.Event()
    competing_writer_entered = threading.Event()
    fresh_read_saw_writer_lease: list[bool] = []

    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        def _database_is_open(self) -> bool:
            return True

        def _execute_with_writer_lease(self, statement, params):
            del statement, params
            fresh_read_saw_writer_lease.append(writer_lease_is_active())
            return GraphStatementResult()

    runtime = _ObservedRuntime()

    def _verification() -> None:
        with runtime.post_write_verification_scope():
            runtime.execute("MATCH (d:DecisionDigest) RETURN d.id")
            scope_entered.set()
            assert release_scope.wait(timeout=5)

    def _competing_writer() -> None:
        with ladybug_writer_scope(scope="test", phase="competing_writer"):
            competing_writer_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        verify_future = pool.submit(_verification)
        assert scope_entered.wait(timeout=5)
        writer_future = pool.submit(_competing_writer)
        assert not competing_writer_entered.wait(timeout=0.1)
        release_scope.set()
        verify_future.result(timeout=5)
        writer_future.result(timeout=5)

    assert fresh_read_saw_writer_lease == [True]
    assert competing_writer_entered.is_set()


def test_stuck_reader_causes_bounded_lifecycle_failure_and_releases_writer() -> None:
    read_entered = threading.Event()
    release_read = threading.Event()
    writer_reacquired = threading.Event()

    class _ObservedRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__()
            self._lifecycle = _LifecycleReadWriteGate(exclusive_timeout_s=0.05)

        def _database_is_open(self) -> bool:
            return True

        def _execute_with_writer_lease(self, statement, params):
            del statement, params
            read_entered.set()
            assert release_read.wait(timeout=5)
            return GraphStatementResult()

    runtime = _ObservedRuntime()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(
            runtime.execute,
            "MATCH (d:DecisionDigest) RETURN d.id",
        )
        assert read_entered.wait(timeout=5)

        started = time.monotonic()
        with pytest.raises(
            GraphLockContention,
            match="lifecycle_exclusive_timeout",
        ) as exc_info:
            runtime.close()
        assert exc_info.value.retryable is True
        assert exc_info.value.details["error_code"] == "graph_lock_contention"
        assert time.monotonic() - started < 1.0

        def _next_writer() -> None:
            with ladybug_writer_scope(scope="test", phase="after_timeout"):
                writer_reacquired.set()

        writer_future = pool.submit(_next_writer)
        assert writer_reacquired.wait(timeout=1)
        writer_future.result(timeout=5)
        release_read.set()
        read_future.result(timeout=5)


def test_replace_decision_digest_identity_republishes_one_real_row(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-identity-replace"
    values = _digest_values(board_id, title="before")
    try:
        _bootstrap(runtime, "identity-replace-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "identity-replace-seed", "test_identity_replace_seed"
        ):
            assert runtime.upsert_decision_digest(**values) == "created"
            runtime.link_board_digest(
                board_id=board_id,
                digest_id=str(values["digest_id"]),
            )
            removed = runtime.replace_decision_digest_identity(
                **{**values, "title": "after", "summary": "after"}
            )

        rows = runtime.execute(
            "MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) "
            "WHERE b.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "RETURN d.id, d.title",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows

        assert removed == 1
        assert rows == ((values["digest_id"], "after"),)
    finally:
        runtime.close()


def test_replace_digest_converges_real_same_primary_key_race(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-real-primary-race"
    values = _digest_values(board_id, title="first-physical-row")
    try:
        _bootstrap(runtime, "real-primary-race-bootstrap")
        _seed_board(runtime, board_id)
        runtime.close()

        primary_lookup, full_scan = _create_same_primary_key_rows_with_multi_writes(
            graph_path=graph_path,
            first=values,
            second={**values, "title": "second-physical-row"},
        )
        assert len(primary_lookup) == 1
        assert len(full_scan) == 2
        assert {str(row[1]) for row in full_scan} == {
            "first-physical-row",
            "second-physical-row",
        }

        with under_global_safe_write(
            "real-primary-race-repair", "test_real_primary_race"
        ):
            removed = runtime.replace_decision_digest_identity(
                **{**values, "title": "canonical-after", "summary": "after"}
            )

        primary_rows = runtime.execute(
            "MATCH (d:DecisionDigest {id: $digest_id}) RETURN d.id, d.title",
            {"digest_id": values["digest_id"]},
        ).rows
        scanned_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id RETURN d.id, d.title",
            {"digest_id": values["digest_id"]},
        ).rows
        links = runtime.execute(
            "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
            "WHERE b.board_id = $board_id "
            "AND coalesce(d.id, '') = $digest_id RETURN count(r)",
            {"board_id": board_id, "digest_id": values["digest_id"]},
        ).rows

        assert removed == 2
        assert primary_rows == ((values["digest_id"], "canonical-after"),)
        assert scanned_rows == primary_rows
        assert links == ((1,),)
    finally:
        runtime.close()


def test_staged_digest_replace_resumes_after_cleanup_crash(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"

    class _CrashAfterCleanupRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__(graph_path_provider=lambda: graph_path)
            self.fail_swap_once = False
            self.staging_creates = 0

        def execute(self, statement, params=None):
            if "CREATE (staging:DecisionDigest" in statement:
                self.staging_creates += 1
            if (
                self.fail_swap_once
                and "MATCH (staging:DecisionDigest)" in statement
                and "CREATE (replacement:DecisionDigest" in statement
            ):
                self.fail_swap_once = False
                raise RuntimeError("simulated crash after committed cleanup")
            return super().execute(statement, params)

    runtime = _CrashAfterCleanupRuntime()
    board_id = "board-staged-recovery"
    values = _digest_values(board_id, title="canonical-before")
    try:
        _bootstrap(runtime, "staged-recovery-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write("staged-recovery-seed", "test_staged_recovery"):
            runtime.upsert_decision_digest(**values)
            runtime.execute(
                "CREATE (d:DecisionDigest {"
                "id: $digest_id, board_id: $board_id, "
                "original_node_id: $original_node_id, title: $title, "
                "one_line_summary: $summary, node_type: $node_type, "
                "graph_layer: $graph_layer, embedding: $embedding, "
                "created_at: timestamp($created_at)})",
                {
                    **values,
                    "digest_id": "legacy-before-crash",
                    "title": "legacy-before-crash",
                },
            )

            runtime.fail_swap_once = True
            with pytest.raises(
                RuntimeError,
                match="simulated crash after committed cleanup",
            ):
                runtime.replace_decision_digest_identity(**values)

            interrupted_rows = runtime.execute(
                "MATCH (d:DecisionDigest) "
                "WHERE d.board_id = $board_id "
                "AND d.original_node_id = $original_node_id "
                "RETURN d.id",
                {"board_id": board_id, "original_node_id": "decision-a"},
            ).rows
            assert len(interrupted_rows) == 1
            assert str(interrupted_rows[0][0]).startswith("dd_repair_")

            runtime.replace_decision_digest_identity(
                **{**values, "title": "canonical-after"}
            )

        final_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "RETURN d.id, d.title",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        assert final_rows == ((values["digest_id"], "canonical-after"),)
        assert runtime.staging_creates == 1
    finally:
        runtime.close()


def test_staged_swap_duplicate_with_empty_scan_fails_irreparable(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"

    class _GhostPrimaryKeyRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__(graph_path_provider=lambda: graph_path)
            self.swap_attempts = 0

        def execute(self, statement, params=None):
            if (
                "MATCH (staging:DecisionDigest)" in statement
                and "CREATE (replacement:DecisionDigest" in statement
            ):
                self.swap_attempts += 1
                raise RuntimeError("Found duplicated primary key value dd_ghost")
            return super().execute(statement, params)

    runtime = _GhostPrimaryKeyRuntime()
    board_id = "board-ghost-primary-key"
    values = _digest_values(board_id, title="canonical-before")
    try:
        _bootstrap(runtime, "ghost-primary-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write("ghost-primary-seed", "test_ghost_primary"):
            runtime.upsert_decision_digest(**values)
            runtime.execute(
                "CREATE (d:DecisionDigest {"
                "id: $digest_id, board_id: $board_id, "
                "original_node_id: $original_node_id, title: $title, "
                "one_line_summary: $summary, node_type: $node_type, "
                "graph_layer: $graph_layer, embedding: $embedding, "
                "created_at: timestamp($created_at)})",
                {
                    **values,
                    "digest_id": "legacy-before-ghost",
                    "title": "legacy-before-ghost",
                },
            )

            with pytest.raises(
                RuntimeError,
                match=(
                    "global_discovery.digest_pk_index_irreparable .*"
                    "staging_preserved=true .*"
                    "recovery=global_discovery_rebuild_then_requeue"
                ),
            ):
                runtime.replace_decision_digest_identity(**values)

        semantic_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "RETURN d.id",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        target_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id RETURN d.id",
            {"digest_id": values["digest_id"]},
        ).rows
        staging_links = runtime.execute(
            "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
            "WHERE b.board_id = $board_id "
            "AND d.original_node_id = $original_node_id RETURN count(r)",
            {"board_id": board_id, "original_node_id": "decision-a"},
        ).rows
        assert runtime.swap_attempts == 1
        assert len(semantic_rows) == 1
        assert str(semantic_rows[0][0]).startswith("dd_repair_")
        assert target_rows == ()
        assert staging_links == ((1,),)
    finally:
        runtime.close()


def test_staged_swap_primary_drains_are_strictly_bounded() -> None:
    class _PersistentPhysicalPrimaryRuntime(CommunityGlobalDiscoveryRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.swap_attempts = 0
            self.primary_drains = 0
            self.awaiting_physical_probe = False

        def execute(self, statement, params=None):
            del params
            if (
                "MATCH (staging:DecisionDigest)" in statement
                and "CREATE (replacement:DecisionDigest" in statement
            ):
                self.swap_attempts += 1
                self.awaiting_physical_probe = True
                raise RuntimeError("Found duplicated primary key value dd_persistent")
            if (
                self.awaiting_physical_probe
                and "coalesce(d.id, '') = $digest_id RETURN d.id" in statement
            ):
                self.awaiting_physical_probe = False
                return GraphStatementResult(rows=(("dd_persistent",),))
            if (
                self.swap_attempts
                and "coalesce(d.id, '') = $digest_id" in statement
                and "DETACH DELETE d" in statement
            ):
                self.primary_drains += 1
                return GraphStatementResult(rows=((1,),))
            if "RETURN d.id" in statement:
                return GraphStatementResult()
            return GraphStatementResult(rows=((0,),))

    runtime = _PersistentPhysicalPrimaryRuntime()
    params = {
        **_digest_values("board-bounded-drain", title="bounded"),
    }
    with pytest.raises(
        RuntimeError,
        match=(
            "global_discovery.digest_pk_index_irreparable .*"
            "reason=primary_drain_limit .*"
            "staging_preserved=true"
        ),
    ):
        runtime._replace_decision_digest_via_staging(
            params=params,
            staging_id="dd_repair_persistent",
            staging_present=True,
        )

    assert runtime.primary_drains == _DIGEST_REPAIR_MAX_PRIMARY_DRAINS
    assert runtime.swap_attempts == _DIGEST_REPAIR_MAX_PRIMARY_DRAINS + 1


def test_replace_decision_digest_identity_fails_before_erasing_derived_edges(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-relationship-safety"
    values = _digest_values(board_id, title="preserve-me")
    try:
        _bootstrap(runtime, "relationship-safety-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "relationship-safety-seed", "test_relationship_safety"
        ):
            runtime.upsert_decision_digest(**values)
            runtime.link_board_digest(
                board_id=board_id,
                digest_id=str(values["digest_id"]),
            )
            runtime.execute(
                "CREATE (e:Entity {id: $id, canonical_name: $name, "
                "aliases: '', embedding: $embedding, mention_count: 1})",
                {"id": "entity-a", "name": "Entity A", "embedding": _EMBEDDING},
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: $entity_id}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e)",
                {"digest_id": values["digest_id"], "entity_id": "entity-a"},
            )

            with pytest.raises(
                RuntimeError,
                match="digest_replace_relationships_present",
            ):
                runtime.replace_decision_digest_identity(
                    **{**values, "title": "must-not-replace"}
                )

        rows = runtime.execute(
            "MATCH (d:DecisionDigest {id: $digest_id}) RETURN d.title",
            {"digest_id": values["digest_id"]},
        ).rows
        assert rows == (("preserve-me",),)
    finally:
        runtime.close()


@pytest.mark.parametrize("direction", ["outgoing", "incoming"])
def test_replace_preflights_cross_board_same_pk_derived_edges(
    tmp_path: Path,
    direction: str,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-owned-identity"
    foreign_board_id = "board-foreign-identity"
    values = _digest_values(board_id, title="owned-physical-row")
    foreign = {
        **values,
        "board_id": foreign_board_id,
        "original_node_id": "foreign-decision",
        "title": "foreign-physical-row",
        "summary": "foreign-physical-row",
    }
    peer = {
        **_digest_values(foreign_board_id, title="foreign-peer"),
        "digest_id": "dd_foreign_peer",
        "original_node_id": "foreign-peer",
    }
    try:
        _bootstrap(runtime, f"cross-board-{direction}-bootstrap")
        _seed_board(runtime, board_id)
        _seed_board(runtime, foreign_board_id)
        with under_global_safe_write(
            f"cross-board-{direction}-peer", "test_cross_board_peer"
        ):
            runtime.upsert_decision_digest(**peer)
        runtime.close()

        primary_lookup, full_scan = _create_same_primary_key_rows_with_multi_writes(
            graph_path=graph_path,
            first=values,
            second=foreign,
        )
        assert len(primary_lookup) == 1
        assert len(full_scan) == 2

        with under_global_safe_write(
            f"cross-board-{direction}-edge", "test_cross_board_edge"
        ):
            endpoints = (
                "(foreign)-[:DECISION_DERIVES_FROM]->(peer)"
                if direction == "outgoing"
                else "(peer)-[:DECISION_DERIVES_FROM]->(foreign)"
            )
            runtime.execute(
                "MATCH (foreign:DecisionDigest), (peer:DecisionDigest) "
                "WHERE foreign.board_id = $foreign_board_id "
                "AND foreign.original_node_id = $foreign_node_id "
                "AND coalesce(peer.id, '') = $peer_id "
                f"CREATE {endpoints}",
                {
                    "foreign_board_id": foreign_board_id,
                    "foreign_node_id": "foreign-decision",
                    "peer_id": peer["digest_id"],
                },
            )

            with pytest.raises(
                RuntimeError,
                match="digest_replace_relationships_present",
            ):
                runtime.replace_decision_digest_identity(**values)

        physical_rows = runtime.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id "
            "RETURN d.board_id ORDER BY d.board_id",
            {"digest_id": values["digest_id"]},
        ).rows
        derived_edges = runtime.execute(
            "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
            "(:DecisionDigest) RETURN count(r)"
        ).rows
        assert physical_rows == ((foreign_board_id,), (board_id,))
        assert derived_edges == ((1,),)
    finally:
        runtime.close()


def test_guarded_prune_preflights_all_targets_before_first_delete(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-guarded-prune"
    protected = _digest_values(board_id, title="protected")
    unprotected = {
        **_digest_values(board_id, title="unprotected"),
        "digest_id": f"dd_{board_id[:8]}_decision-b",
        "original_node_id": "decision-b",
    }
    try:
        _bootstrap(runtime, "guarded-prune-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write("guarded-prune-seed", "test_guarded_prune"):
            runtime.upsert_decision_digest(**protected)
            runtime.upsert_decision_digest(**unprotected)
            runtime.execute(
                "CREATE (e:Entity {id: $id, canonical_name: $name, "
                "aliases: '', embedding: $embedding, mention_count: 1})",
                {"id": "entity-prune", "name": "Entity", "embedding": _EMBEDDING},
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: $entity_id}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e)",
                {
                    "digest_id": protected["digest_id"],
                    "entity_id": "entity-prune",
                },
            )
            runtime.execute(
                "MATCH (a:DecisionDigest {id: $a}), "
                "(b:DecisionDigest {id: $b}) "
                "CREATE (a)-[:DECISION_DERIVES_FROM]->(b) "
                "CREATE (b)-[:DECISION_DERIVES_FROM]->(a)",
                {"a": protected["digest_id"], "b": unprotected["digest_id"]},
            )

            with pytest.raises(
                RuntimeError,
                match="digest_prune_relationships_present",
            ):
                runtime.delete_decision_digests_guarded(
                    board_id=board_id,
                    original_node_ids=("decision-a", "decision-b"),
                )

        rows = runtime.execute(
            "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id "
            "RETURN d.original_node_id ORDER BY d.original_node_id",
            {"board_id": board_id},
        ).rows
        derives = runtime.execute(
            "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
            "(:DecisionDigest) RETURN count(r)"
        ).rows
        assert rows == (("decision-a",), ("decision-b",))
        assert derives == ((2,),)
    finally:
        runtime.close()


def test_absent_source_prune_atomically_detaches_derived_relationships(
    tmp_path: Path,
) -> None:
    """Lifecycle-authorized hard delete removes the digest and every cache edge."""

    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-absent-source-prune"
    removed_values = _digest_values(board_id, title="removed")
    peer_values = {
        **_digest_values(board_id, title="peer"),
        "digest_id": f"dd_{board_id[:8]}_decision-b",
        "original_node_id": "decision-b",
    }
    try:
        _bootstrap(runtime, "absent-source-prune-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "absent-source-prune-seed",
            "test_absent_source_prune",
        ):
            runtime.upsert_decision_digest(**removed_values)
            runtime.upsert_decision_digest(**peer_values)
            runtime.link_board_digest(
                board_id=board_id,
                digest_id=str(removed_values["digest_id"]),
            )
            runtime.link_board_digest(
                board_id=board_id,
                digest_id=str(peer_values["digest_id"]),
            )
            runtime.execute(
                "CREATE (e:Entity {id: $id, canonical_name: $name, "
                "aliases: '', embedding: $embedding, mention_count: 1})",
                {"id": "entity-absent", "name": "Entity", "embedding": _EMBEDDING},
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: $entity_id}), "
                "(peer:DecisionDigest {id: $peer_id}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e) "
                "CREATE (d)-[:DECISION_DERIVES_FROM]->(peer) "
                "CREATE (peer)-[:DECISION_DERIVES_FROM]->(d)",
                {
                    "digest_id": removed_values["digest_id"],
                    "entity_id": "entity-absent",
                    "peer_id": peer_values["digest_id"],
                },
            )

            removed = runtime.delete_decision_digests_for_absent_sources(
                board_id=board_id,
                original_node_ids=("decision-a",),
            )

        rows = runtime.execute(
            "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id "
            "RETURN d.original_node_id ORDER BY d.original_node_id",
            {"board_id": board_id},
        ).rows
        mentions = runtime.execute(
            "MATCH (:DecisionDigest)-[r:DECISION_MENTIONS_ENTITY]->"
            "(:Entity) RETURN count(r)"
        ).rows
        derives = runtime.execute(
            "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
            "(:DecisionDigest) RETURN count(r)"
        ).rows
        assert removed == 1
        assert rows == (("decision-b",),)
        assert mentions == ((0,),)
        assert derives == ((0,),)
    finally:
        runtime.close()


def test_search_hides_revoked_digest_and_restore_republishes(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-search-lifecycle"
    values = _digest_values(board_id, title="reversible search")
    try:
        _bootstrap(runtime, "search-lifecycle-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write(
            "search-lifecycle-seed",
            "test_search_lifecycle",
        ):
            runtime.upsert_decision_digest(**values)
            runtime.link_board_digest(
                board_id=board_id,
                digest_id=str(values["digest_id"]),
            )
            before = runtime.search_decision_digests(
                _EMBEDDING,
                board_ids=(board_id,),
                graph_layer="canonical",
                top_k=10,
                min_similarity=0.0,
                exhaustive=True,
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}) SET d.source_revoked = true",
                {"digest_id": values["digest_id"]},
            )
            hidden = runtime.search_decision_digests(
                _EMBEDDING,
                board_ids=(board_id,),
                graph_layer="canonical",
                top_k=10,
                min_similarity=0.0,
                exhaustive=True,
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}) "
                "SET d.source_revoked = false",
                {"digest_id": values["digest_id"]},
            )
            restored = runtime.search_decision_digests(
                _EMBEDDING,
                board_ids=(board_id,),
                graph_layer="canonical",
                top_k=10,
                min_similarity=0.0,
                exhaustive=True,
            )

        assert [row["id"] for row in before] == ["decision-a"]
        assert hidden == []
        assert [row["id"] for row in restored] == ["decision-a"]
    finally:
        runtime.close()


def test_normalize_duplicate_contains_preserves_derived_relationships(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-normalize-links"
    values = _digest_values(board_id, title="preserve-derived")
    peer = {
        **_digest_values(board_id, title="peer"),
        "digest_id": f"dd_{board_id[:8]}_decision-b",
        "original_node_id": "decision-b",
    }
    try:
        _bootstrap(runtime, "normalize-links-bootstrap")
        _seed_board(runtime, board_id)
        with under_global_safe_write("normalize-links-seed", "test_normalize_links"):
            runtime.upsert_decision_digest(**values)
            runtime.upsert_decision_digest(**peer)
            runtime.execute(
                "MATCH (b:Board {board_id: $board_id}), "
                "(d:DecisionDigest {id: $digest_id}) "
                "CREATE (b)-[:CONTAINS_DECISION]->(d) "
                "CREATE (b)-[:CONTAINS_DECISION]->(d)",
                {"board_id": board_id, "digest_id": values["digest_id"]},
            )
            runtime.execute(
                "CREATE (e:Entity {id: $id, canonical_name: $name, "
                "aliases: '', embedding: $embedding, mention_count: 1})",
                {"id": "entity-links", "name": "Entity", "embedding": _EMBEDDING},
            )
            runtime.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}), "
                "(e:Entity {id: $entity_id}) "
                "CREATE (d)-[:DECISION_MENTIONS_ENTITY]->(e)",
                {
                    "digest_id": values["digest_id"],
                    "entity_id": "entity-links",
                },
            )
            runtime.execute(
                "MATCH (a:DecisionDigest {id: $a}), "
                "(b:DecisionDigest {id: $b}) "
                "CREATE (a)-[:DECISION_DERIVES_FROM]->(b) "
                "CREATE (b)-[:DECISION_DERIVES_FROM]->(a)",
                {"a": values["digest_id"], "b": peer["digest_id"]},
            )
            assert (
                runtime.normalize_board_digest_link(
                    board_id=board_id,
                    digest_id=str(values["digest_id"]),
                )
                == 2
            )

        contains = runtime.execute(
            "MATCH (:Board)-[r:CONTAINS_DECISION]->"
            "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
            {"digest_id": values["digest_id"]},
        ).rows
        mentions = runtime.execute(
            "MATCH (d:DecisionDigest {id: $digest_id})-"
            "[r:DECISION_MENTIONS_ENTITY]->(:Entity) RETURN count(r)",
            {"digest_id": values["digest_id"]},
        ).rows
        derives = runtime.execute(
            "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
            "(:DecisionDigest) RETURN count(r)"
        ).rows
        assert contains == ((1,),)
        assert mentions == ((1,),)
        assert derives == ((2,),)
    finally:
        runtime.close()


def test_delete_invalid_outgoing_board_link_preserves_foreign_digest(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "global" / "discovery.lbug"
    runtime = CommunityGlobalDiscoveryRuntime(
        graph_path_provider=lambda: graph_path,
    )
    board_id = "board-link-owner"
    foreign_board_id = "board-link-foreign"
    foreign = _digest_values(foreign_board_id, title="foreign")
    try:
        _bootstrap(runtime, "invalid-link-bootstrap")
        _seed_board(runtime, board_id)
        _seed_board(runtime, foreign_board_id)
        with under_global_safe_write("invalid-link-seed", "test_invalid_link_cleanup"):
            runtime.upsert_decision_digest(**foreign)
            runtime.execute(
                "MATCH (b:Board {board_id: $board_id}), "
                "(d:DecisionDigest {id: $digest_id}) "
                "CREATE (b)-[:CONTAINS_DECISION]->(d)",
                {"board_id": board_id, "digest_id": foreign["digest_id"]},
            )
            removed = runtime.delete_invalid_board_digest_links(
                board_id=board_id,
                expected_digest_ids=(),
            )

        edge_count = runtime.execute(
            "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
            "WHERE b.board_id = $board_id RETURN count(r)",
            {"board_id": board_id},
        ).rows
        digest_count = runtime.execute(
            "MATCH (d:DecisionDigest {id: $digest_id}) RETURN count(d)",
            {"digest_id": foreign["digest_id"]},
        ).rows
        assert removed == 1
        assert edge_count == ((0,),)
        assert digest_count == ((1,),)
    finally:
        runtime.close()
