"""Community Global Discovery runtime adapter.

The core owns query semantics and schema constants; the Community edition owns
the local LadybugDB path, handle lifecycle and quarantine behavior.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

from okto_pulse.community.adapters.board_graph_runtime import (
    CommunityBoardGraphRuntime,
)
from okto_pulse.community.adapters.graph_error_mapping import map_graph_error
from okto_pulse.community.adapters.kuzu_graph_transaction import _materialize
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.interfaces.graph_lifecycle import GraphHandle
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

logger = logging.getLogger("okto_pulse.community.global_discovery_runtime")

GLOBAL_DISCOVERY_FILENAME = "discovery.lbug"


class CommunityGlobalDiscoveryRuntime:
    """Concrete GlobalDiscoveryRuntime backed by local LadybugDB."""

    def __init__(
        self,
        graph_runtime: CommunityBoardGraphRuntime | None = None,
        *,
        graph_path_provider: Callable[[], Path] | None = None,
    ) -> None:
        self._graph_runtime = graph_runtime or CommunityBoardGraphRuntime()
        self._graph_path_provider = graph_path_provider
        self._lock = threading.Lock()
        self._db: Any | None = None

    def _runtime(self):
        return self._graph_runtime

    def _kg_base_dir(self) -> Path:
        from okto_pulse.core.services.application_kg import (
            get_current_provider_registry,
        )

        raw = get_current_provider_registry().config.kg_base_dir
        return Path(os.path.expanduser(raw)).resolve()

    def _global_graph_path(self) -> Path:
        if self._graph_path_provider is not None:
            return Path(self._graph_path_provider()).resolve()
        return self._kg_base_dir() / "global" / GLOBAL_DISCOVERY_FILENAME

    @staticmethod
    def _storage_ref() -> StorageRef:
        return StorageRef("global-discovery", "community_local_graph")

    def state(self) -> GraphRuntimeState:
        artifact = self._global_graph_path()
        exists = artifact.exists()
        return GraphRuntimeState(
            board_id="_global",
            storage_ref=self._storage_ref(),
            exists=exists,
            status="healthy" if exists else "absent",
            backend="community_local_graph",
            unavailable_reason=None if exists else "graph_absent",
        )

    def require_write_token(self, *, operation: str = "") -> Any:
        from okto_pulse.core.kg.write_barrier import require_global_write_token

        return require_global_write_token()

    def _quarantine_service(self):
        from okto_pulse.community.adapters.local_storage_ref import local_storage_ref
        from okto_pulse.core.kg.quarantine import KGQuarantineService

        graph_dir = self._global_graph_path().parent
        return KGQuarantineService(
            base_storage_ref_hint=local_storage_ref(graph_dir.parent),
            scope_storage_refs=[local_storage_ref(graph_dir)],
        )

    def is_ladybug_corruption_error(self, exc: BaseException) -> bool:
        return self._runtime().is_ladybug_corruption_error(exc)

    def bootstrap(self) -> GraphHandle:
        from okto_pulse.community.adapters.global_discovery_schema import (
            NODE_DDL,
            REL_DDL,
            VECTOR_INDEXES,
        )
        from okto_pulse.community.adapters.global_discovery_schema import (
            ensure_decision_digest_layer_column,
            raise_existing_global_graph_open_failed,
        )

        self.require_write_token(operation="bootstrap")
        path = self._global_graph_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = self._runtime().open_kuzu_db(path)
        except Exception as exc:
            raise_existing_global_graph_open_failed(
                storage_locator=path,
                operation="bootstrap",
                exc=exc,
            )
        runtime = self._runtime()
        conn = runtime.new_connection(db)
        try:
            runtime.load_vector_extension(conn)
            for ddl in NODE_DDL:
                conn.execute(ddl)
            for ddl in REL_DDL:
                conn.execute(ddl)
            ensure_decision_digest_layer_column(conn)
            for table, idx_name, col in VECTOR_INDEXES:
                try:
                    conn.execute(
                        f"CALL CREATE_VECTOR_INDEX("
                        f"'{table}', '{idx_name}', '{col}', "
                        f"metric := 'cosine')"
                    )
                except Exception:
                    pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
            del db
            gc.collect()
        return GraphHandle(
            board_id="_global",
            storage_ref=self._storage_ref(),
            opened=True,
            status="opened",
            locked=False,
            quarantined=False,
        )

    def ensure_layer_schema(self) -> tuple[str, ...]:
        from okto_pulse.community.adapters.global_discovery_schema import (
            ensure_decision_digest_layer_column,
        )

        self.require_write_token(operation="ensure_layer_schema")
        _db, conn = self._open_native()
        try:
            return ensure_decision_digest_layer_column(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _open_native(self) -> tuple[Any, Any]:
        from okto_pulse.community.adapters.global_discovery_schema import (
            raise_existing_global_graph_open_failed,
        )

        path = self._global_graph_path()
        if not path.exists():
            self.bootstrap()

        with self._lock:
            if self._db is None:
                try:
                    self._db = self._runtime().open_kuzu_db(path)
                except Exception as exc:
                    raise_existing_global_graph_open_failed(
                        storage_locator=path,
                        operation="open_connection",
                        exc=exc,
                    )
            conn = self._runtime().new_connection(self._db)
        self._runtime().load_vector_extension(conn)
        return self._db, conn

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        native_scope = None
        try:
            _db, native_scope = self._open_native()
            native_result = (
                native_scope.execute(statement, params)
                if params
                else native_scope.execute(statement)
            )
            return _materialize(native_result)
        except Exception as exc:
            raise map_graph_error(exc, operation="global_graph_statement") from exc
        finally:
            if native_scope is not None:
                try:
                    native_scope.close()
                except Exception:
                    pass

    def search_decision_digests(
        self,
        query_vector: list[float],
        *,
        board_ids: tuple[str, ...],
        graph_layer: str,
        top_k: int,
        min_similarity: float,
        exhaustive: bool = False,
    ) -> list[dict[str, Any]]:
        """Run indexed or exhaustive semantic search behind the graph port."""

        if not board_ids or top_k <= 0:
            return []

        if not exhaustive:
            try:
                result = self.execute(
                    "CALL QUERY_VECTOR_INDEX("
                    "'DecisionDigest', 'digest_embedding_idx', $vec, $search_k) "
                    "WITH node, distance "
                    "MATCH (b:Board)-[:CONTAINS_DECISION]->(node) "
                    "WHERE b.board_id IN $boards "
                    f"AND {tpl.layer_filter_clause('node')} "
                    "RETURN b.board_id, node.id, node.original_node_id, "
                    "node.title, node.one_line_summary, node.node_type, "
                    f"{tpl.layer_label_projection('node')}, distance "
                    "ORDER BY distance ASC LIMIT $search_k",
                    {
                        "vec": query_vector,
                        "search_k": top_k,
                        "boards": list(board_ids),
                        "graph_layer": graph_layer,
                    },
                )
                hits: list[dict[str, Any]] = []
                for row in result.rows:
                    similarity = max(0.0, min(1.0, 1.0 - float(row[7])))
                    if similarity >= min_similarity:
                        hits.append(
                            {
                                "board_id": row[0],
                                "digest_id": row[1],
                                "id": row[2],
                                "title": row[3],
                                "summary": row[4],
                                "node_type": row[5],
                                "graph_layer": row[6],
                                "similarity": similarity,
                            }
                        )
                if hits:
                    return hits[:top_k]
            except Exception as exc:
                logger.debug("global_discovery.index_search_failed err=%s", exc)

        try:
            result = self.execute(
                "MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) "
                "WHERE b.board_id IN $boards AND d.embedding IS NOT NULL "
                f"AND {tpl.layer_filter_clause('d')} "
                "RETURN b.board_id, d.id, d.original_node_id, d.title, "
                "d.one_line_summary, d.node_type, "
                f"{tpl.layer_label_projection('d')}, d.embedding LIMIT 500",
                {"boards": list(board_ids), "graph_layer": graph_layer},
            )
        except Exception as exc:
            logger.debug("global_discovery.exhaustive_search_failed err=%s", exc)
            return []

        query_norm = sum(value * value for value in query_vector) ** 0.5 or 1.0
        scored: list[dict[str, Any]] = []
        for row in result.rows:
            embedding = row[7]
            if not embedding or len(embedding) != len(query_vector):
                continue
            dot = sum(a * b for a, b in zip(query_vector, embedding))
            embedding_norm = sum(value * value for value in embedding) ** 0.5 or 1.0
            similarity = max(0.0, min(1.0, dot / (query_norm * embedding_norm)))
            if similarity < min_similarity:
                continue
            scored.append(
                {
                    "board_id": row[0],
                    "digest_id": row[1],
                    "id": row[2],
                    "title": row[3],
                    "summary": row[4],
                    "node_type": row[5],
                    "graph_layer": row[6],
                    "similarity": similarity,
                }
            )
        scored.sort(key=lambda item: item["similarity"], reverse=True)
        return scored[:top_k]

    def list_schema_objects(self) -> tuple[str, ...]:
        result = self.execute("CALL SHOW_TABLES() RETURN name")
        return tuple(sorted(str(row[0]) for row in result.rows if row and row[0]))

    def upsert_board_summary(
        self,
        *,
        board_id: str,
        name: str,
        summary: str,
        summary_embedding: list[float],
        decision_delta: int,
        synced_at: str,
    ) -> None:
        existing = self.execute(
            "MATCH (b:Board {board_id: $board_id}) RETURN b.board_id",
            {"board_id": board_id},
        )
        if existing.rows:
            self.execute(
                "MATCH (b:Board {board_id: $board_id}) "
                "SET b.decision_count = coalesce(b.decision_count, 0) + $delta, "
                "b.last_sync_at = timestamp($synced_at)",
                {
                    "board_id": board_id,
                    "delta": decision_delta,
                    "synced_at": synced_at,
                },
            )
            return
        self.execute(
            "CREATE (b:Board {"
            "board_id: $board_id, name: $name, summary: $summary, "
            "summary_embedding: $embedding, topic_count: 0, entity_count: 0, "
            "decision_count: $delta, last_sync_at: timestamp($synced_at)})",
            {
                "board_id": board_id,
                "name": name,
                "summary": summary,
                "embedding": summary_embedding,
                "delta": decision_delta,
                "synced_at": synced_at,
            },
        )

    def upsert_decision_digest(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        title: str,
        summary: str,
        node_type: str,
        graph_layer: str,
        embedding: list[float],
        created_at: str,
    ) -> str:
        existing = self.execute(
            "MATCH (d:DecisionDigest {id: $digest_id}) RETURN d.id",
            {"digest_id": digest_id},
        )
        values = {
            "digest_id": digest_id,
            "board_id": board_id,
            "original_node_id": original_node_id,
            "title": title,
            "summary": summary,
            "node_type": node_type,
            "graph_layer": graph_layer,
        }
        if existing.rows:
            self.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}) "
                "SET d.board_id = $board_id, "
                "d.original_node_id = $original_node_id, d.title = $title, "
                "d.one_line_summary = $summary, d.node_type = $node_type, "
                "d.graph_layer = $graph_layer",
                values,
            )
            return "updated"
        self.execute(
            "CREATE (d:DecisionDigest {"
            "id: $digest_id, board_id: $board_id, "
            "original_node_id: $original_node_id, title: $title, "
            "one_line_summary: $summary, node_type: $node_type, "
            "graph_layer: $graph_layer, embedding: $embedding, "
            "created_at: timestamp($created_at)})",
            {**values, "embedding": embedding, "created_at": created_at},
        )
        return "created"

    def link_board_digest(self, *, board_id: str, digest_id: str) -> None:
        self.execute(
            "MATCH (b:Board {board_id: $board_id}), "
            "(d:DecisionDigest {id: $digest_id}) "
            "MERGE (b)-[:CONTAINS_DECISION]->(d)",
            {"board_id": board_id, "digest_id": digest_id},
        )

    @staticmethod
    def _fsync_if_file(path: Path) -> None:
        if not path.is_file():
            return
        # Windows rejects os.fsync on a read-only descriptor. r+b does not
        # truncate and gives a real durability boundary for file contents.
        with path.open("r+b") as fh:
            os.fsync(fh.fileno())

    def _fsync_global_artifacts(self, path: Path) -> None:
        self._fsync_if_file(path)
        if not path.parent.exists():
            return
        for sibling in sorted(path.parent.glob(path.name + ".*")):
            self._fsync_if_file(sibling)

    def flush_after_write_batch(self) -> None:
        """Close, fsync and reopen-probe discovery.lbug after a write batch."""

        path = self._global_graph_path()
        self.close()
        if not path.exists():
            raise RuntimeError(f"global discovery file missing at {path}")

        self._fsync_global_artifacts(path)

        _db, conn = self._open_native()
        try:
            res = conn.execute("CALL SHOW_TABLES() RETURN name")
            try:
                if res.has_next():
                    res.get_next()
            finally:
                if hasattr(res, "close"):
                    res.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self.close()

        self._fsync_global_artifacts(path)

    def close(self) -> None:
        with self._lock:
            db = self._db
            if db is None:
                return
            self._db = None
        if hasattr(db, "close"):
            try:
                db.close()
            except Exception as exc:
                logger.warning(
                    "global_connection.close_failed err=%s",
                    exc,
                    extra={"event": "global_connection.close_failed"},
                )
        del db
        gc.collect()

    def purge(self, *, reason: str = "manual") -> GraphPurgeResult:
        from okto_pulse.core.kg.quarantine import QuarantineError

        self.require_write_token(operation="purge")

        path = self._global_graph_path()
        self.close()
        targets: list[Path] = []
        if path.exists():
            targets.append(path)
        if path.parent.exists():
            targets.extend(sorted(path.parent.glob(path.name + ".*")))

        if not targets:
            return GraphPurgeResult(
                board_id="_global",
                removed=False,
                not_found=True,
                status="not_found",
                reason=reason,
                backend="community_local_graph",
            )

        service = self._quarantine_service()
        try:
            from okto_pulse.community.adapters.local_storage_ref import (
                local_storage_ref,
            )

            response = service.create(
                board_id="_global",
                graph_type="global_discovery",
                affected_storage_refs=[local_storage_ref(t) for t in targets],
                reason=reason,
                correlation_ids=[],
            )
        except QuarantineError as exc:
            logger.error(
                "global_discovery.purge_blocked_quarantine_failed "
                "reason=%s code=%s err=%s",
                reason,
                exc.code.value,
                exc.reason,
                extra={
                    "event": "global_discovery.purge_blocked_quarantine_failed",
                    "reason": reason,
                    "code": exc.code.value,
                },
            )
            return GraphPurgeResult(
                board_id="_global",
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend="community_local_graph",
                error_code=exc.code.value,
            )

        moved_count = response.files_moved
        removed = [str(t) for t in targets[:moved_count]]
        logger.warning(
            "global_discovery.purged reason=%s removed=%d quarantine_id=%s manifest=%s",
            reason,
            moved_count,
            response.quarantine_id,
            response.manifest_ref,
            extra={
                "event": "global_discovery.purged",
                "reason": reason,
                "quarantine_id": response.quarantine_id,
                "manifest_ref": response.manifest_ref,
                "files_moved": moved_count,
            },
        )
        return GraphPurgeResult(
            board_id="_global",
            removed=bool(removed),
            not_found=not bool(removed),
            status="purged" if removed else "not_found",
            reason=reason,
            backend="community_local_graph",
        )

    def reset_for_tests(self) -> None:
        self.close()


__all__ = ["CommunityGlobalDiscoveryRuntime"]
