"""Community implementation of the legacy board graph runtime port."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class CommunityBoardGraphRuntime:
    """Adapter around ``community.adapters.kg_runtime``.

    The core keeps a shim for historical ``okto_pulse.core.kg.schema`` imports,
    but the concrete Ladybug/Kuzu runtime lives in the Community edition.
    """

    def _runtime(self):
        from okto_pulse.community.adapters import kg_runtime

        return kg_runtime

    def board_kuzu_path(self, board_id: str) -> Path:
        return self._runtime().board_kuzu_path(board_id)

    def bootstrap_board_graph(self, board_id: str) -> Any:
        return self._runtime().bootstrap_board_graph(board_id)

    def ensure_board_graph_bootstrapped(self, board_id: str) -> None:
        self._runtime().ensure_board_graph_bootstrapped(board_id)

    def open_board_connection(self, board_id: str) -> Any:
        return self._runtime().open_board_connection(board_id)

    def open_board_connection_raw(self, board_id: str) -> Any:
        return self._runtime().open_board_connection_raw(board_id)

    def close_all_connections(self, board_id: str | None = None) -> None:
        self._runtime().close_all_connections(board_id)

    def close_board_db_cache(self, board_id: str | None = None) -> None:
        self._runtime().close_board_db_cache(board_id=board_id)

    def purge_board_graph_storage(
        self,
        board_id: str,
        *,
        reason: str = "manual",
    ) -> list[str]:
        return self._runtime().purge_board_graph_storage(board_id, reason=reason)

    def migrate_schema_for_board(self, board_id: str) -> bool:
        return self._runtime().migrate_schema_for_board(board_id)

    def reset_bootstrap_cache_for_tests(self) -> None:
        self._runtime().reset_bootstrap_cache_for_tests()

    def apply_ladybug_lifecycle_step(self, *args: Any, **kwargs: Any) -> Any:
        return self._runtime().apply_ladybug_lifecycle_step(*args, **kwargs)

    def load_vector_extension(self, conn: Any) -> None:
        self._runtime().load_vector_extension(conn)

    def open_kuzu_db(self, path: Path) -> Any:
        return self._runtime()._open_kuzu_db(path)

    def new_connection(self, db: Any) -> Any:
        return self._runtime().kuzu.Connection(db)

    def is_ladybug_corruption_error(self, exc: BaseException) -> bool:
        return self._runtime()._is_ladybug_corruption_error(exc)


__all__ = ["CommunityBoardGraphRuntime"]

