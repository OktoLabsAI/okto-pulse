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
from typing import Any

from okto_pulse.community.adapters.board_graph_runtime import (
    CommunityBoardGraphRuntime,
)

logger = logging.getLogger("okto_pulse.community.global_discovery_runtime")

GLOBAL_DISCOVERY_FILENAME = "discovery.lbug"


class CommunityGlobalDiscoveryRuntime:
    """Concrete GlobalDiscoveryRuntime backed by local LadybugDB."""

    def __init__(self, graph_runtime: CommunityBoardGraphRuntime | None = None) -> None:
        self._graph_runtime = graph_runtime or CommunityBoardGraphRuntime()
        self._lock = threading.Lock()
        self._db: Any | None = None

    def _runtime(self):
        try:
            from okto_pulse.core.kg.interfaces import get_kg_registry

            runtime = getattr(get_kg_registry(), "board_graph_runtime", None)
            if runtime is not None:
                return runtime
        except Exception:
            pass
        return self._graph_runtime

    def _kg_base_dir(self) -> Path:
        from okto_pulse.core.kg.interfaces import get_kg_registry

        raw = get_kg_registry().config.kg_base_dir
        return Path(os.path.expanduser(raw)).resolve()

    def global_graph_path(self) -> Path:
        return self._kg_base_dir() / "global" / GLOBAL_DISCOVERY_FILENAME

    def require_write_token(self, *, operation: str = "") -> Any:
        from okto_pulse.core.kg.write_barrier import require_global_write_token

        return require_global_write_token()

    def _quarantine_service(self):
        from okto_pulse.core.kg.quarantine import KGQuarantineService

        graph_dir = self.global_graph_path().parent
        return KGQuarantineService(
            base_dir=graph_dir.parent,
            scope_roots=[graph_dir],
        )

    def is_ladybug_corruption_error(self, exc: BaseException) -> bool:
        return self._runtime().is_ladybug_corruption_error(exc)

    def bootstrap(self) -> Path:
        from okto_pulse.core.kg.global_discovery.schema import (
            NODE_DDL,
            REL_DDL,
            VECTOR_INDEXES,
            _ensure_decision_digest_layer_column,
            _raise_existing_global_graph_open_failed,
        )

        self.require_write_token(operation="bootstrap")
        path = self.global_graph_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = self._runtime().open_kuzu_db(path)
        except Exception as exc:
            _raise_existing_global_graph_open_failed(
                path=path,
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
            _ensure_decision_digest_layer_column(conn)
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
        return path

    def ensure_layer_schema(self) -> list[str]:
        from okto_pulse.core.kg.global_discovery.schema import (
            _ensure_decision_digest_layer_column,
        )

        self.require_write_token(operation="ensure_layer_schema")
        _db, conn = self.open_connection()
        try:
            return _ensure_decision_digest_layer_column(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def open_connection(self) -> tuple[Any, Any]:
        from okto_pulse.core.kg.global_discovery.schema import (
            _raise_existing_global_graph_open_failed,
        )

        path = self.global_graph_path()
        if not path.exists():
            self.bootstrap()

        with self._lock:
            if self._db is None:
                try:
                    self._db = self._runtime().open_kuzu_db(path)
                except Exception as exc:
                    _raise_existing_global_graph_open_failed(
                        path=path,
                        operation="open_connection",
                        exc=exc,
                    )
            conn = self._runtime().new_connection(self._db)
        self._runtime().load_vector_extension(conn)
        return self._db, conn

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
                    "global_connection.close_failed err=%s", exc,
                    extra={"event": "global_connection.close_failed"},
                )
        del db
        gc.collect()

    def purge(self, *, reason: str = "manual") -> list[str]:
        from okto_pulse.core.kg.quarantine import QuarantineError

        self.require_write_token(operation="purge")

        path = self.global_graph_path()
        self.close()
        targets: list[Path] = []
        if path.exists():
            targets.append(path)
        if path.parent.exists():
            targets.extend(sorted(path.parent.glob(path.name + ".*")))

        if not targets:
            return []

        service = self._quarantine_service()
        try:
            response = service.create(
                board_id="_global",
                graph_type="global_discovery",
                affected_paths=[str(t) for t in targets],
                reason=reason,
                correlation_ids=[],
            )
        except QuarantineError as exc:
            logger.error(
                "global_discovery.purge_blocked_quarantine_failed "
                "reason=%s code=%s err=%s",
                reason, exc.code.value, exc.reason,
                extra={
                    "event": "global_discovery.purge_blocked_quarantine_failed",
                    "reason": reason,
                    "code": exc.code.value,
                },
            )
            return []

        moved_count = response.files_moved
        removed = [str(t) for t in targets[:moved_count]]
        logger.warning(
            "global_discovery.purged reason=%s removed=%d "
            "quarantine_id=%s manifest=%s",
            reason, moved_count,
            response.quarantine_id, response.manifest_ref,
            extra={
                "event": "global_discovery.purged",
                "reason": reason,
                "quarantine_id": response.quarantine_id,
                "manifest_ref": response.manifest_ref,
                "files_moved": moved_count,
            },
        )
        return removed

    def reset_for_tests(self) -> None:
        self.close()


__all__ = ["CommunityGlobalDiscoveryRuntime"]
