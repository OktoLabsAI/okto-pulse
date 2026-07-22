"""Community-owned SQLite BoardSourceReader adapter."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from okto_pulse.core.kg.board_source_store import (
    AMENDMENT_CONTENT_COLUMNS,
    CARD_CONTENT_COLUMNS,
    IDEATION_CONTENT_COLUMNS,
    REFINEMENT_CONTENT_COLUMNS,
    SPEC_CONTENT_COLUMNS_V1,
    SPEC_CONTENT_COLUMNS_V2,
    SPEC_SOURCE_MANIFEST_VERSION,
    SPRINT_CONTENT_COLUMNS,
    STORY_CONTENT_COLUMNS,
    bug_has_minimal_evidence,
    canonical_content_hash,
    card_artifact_type,
    decision_sources_from_spec,
    row_status,
    to_iso,
    updated_at,
)
from okto_pulse.core.kg.interfaces.board_source_reader import (
    BoardSourceSnapshot,
    SourceReadFailure,
    SourceUnavailableError,
)

logger = logging.getLogger("okto_pulse.community.board_source_reader")


# SQL table ownership lives in the edition adapter. Core retains only the DTO
# and hash rules consumed above.
ARTIFACT_QUERIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("story", "stories", "status", STORY_CONTENT_COLUMNS),
    ("ideation", "ideations", "status", IDEATION_CONTENT_COLUMNS),
    ("spec", "specs", "status", SPEC_CONTENT_COLUMNS_V2),
    ("refinement", "refinements", "status", REFINEMENT_CONTENT_COLUMNS),
    ("sprint", "sprints", "status", SPRINT_CONTENT_COLUMNS),
)

_REQUIRED_SOURCE_TABLES = frozenset(
    {
        "boards",
        *(table for _, table, _, _ in ARTIFACT_QUERIES),
        "cards",
        "amendment_hotfix_revisions",
    }
)

_REQUIRED_SOURCE_COLUMNS: dict[str, frozenset[str]] = {
    "boards": frozenset({"id"}),
    **{
        table: frozenset(
            {
                "id",
                "board_id",
                "created_at",
                status_col,
                *content_cols,
            }
        )
        for _, table, status_col, content_cols in ARTIFACT_QUERIES
    },
    "cards": frozenset(
        {
            "id",
            "board_id",
            "created_at",
            "status",
            *CARD_CONTENT_COLUMNS,
        }
    ),
    "amendment_hotfix_revisions": frozenset(
        {
            "id",
            "board_id",
            "created_at",
            "status",
            *AMENDMENT_CONTENT_COLUMNS,
        }
    ),
}


def resolve_pulse_db_path() -> Path:
    """Return the SQLite file targeted by the configured SQLAlchemy engine."""

    try:
        from okto_pulse.community.adapters.sqlalchemy_database import (
            CommunityDatabasePathUnavailable,
            resolve_sqlite_database_path,
        )

        return resolve_sqlite_database_path()
    except CommunityDatabasePathUnavailable as exc:
        raise SourceUnavailableError(
            "board source database path could not be resolved",
            cause_type=type(exc).__name__,
        ) from exc


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _board_working_ttl_days(conn: sqlite3.Connection, board_id: str) -> int | None:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='boards'"
    ).fetchone()
    if not exists:
        return None
    columns = _table_columns(conn, "boards")
    if "settings" not in columns:
        return None
    row = conn.execute(
        "SELECT settings FROM boards WHERE id = ?",
        (board_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["settings"]
    return _working_ttl_days_from_settings(raw)


def _working_ttl_days_from_settings(raw: object) -> int | None:
    if not raw:
        return None
    try:
        settings = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    for key in (
        "kg_working_ttl_days",
        "kg_working_source_ttl_days",
        "working_graph_ttl_days",
    ):
        value = settings.get(key)
        if value is None:
            continue
        try:
            ttl = int(value)
        except (TypeError, ValueError):
            continue
        if ttl >= 0:
            return ttl
    return None


def read_realm_source_snapshot(
    connection: sqlite3.Connection,
    *,
    realm_id: str,
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[dict[str, Any], ...]]]:
    """Capture every realm board and rebuild row without per-board SQL.

    The caller owns the surrounding SQLite read transaction.  Each source
    table is scanned exactly once through a realm-filtered join, so preparation
    can prove one coherent census without reopening the database for each board.
    """

    normalized_realm_id = str(realm_id).strip()
    if not normalized_realm_id:
        raise ValueError("realm_id must be non-empty")
    boards = connection.execute(
        "SELECT id, name, description, settings FROM boards "
        "WHERE realm_id = ? ORDER BY id COLLATE BINARY",
        (normalized_realm_id,),
    ).fetchall()
    board_rows = tuple(
        {
            "board_id": str(row["id"]),
            "board_name": str(row["name"]),
            "board_summary": str(row["description"] or ""),
        }
        for row in boards
    )
    ttl_by_board = {
        str(row["id"]): _working_ttl_days_from_settings(row["settings"])
        for row in boards
    }
    captured: dict[str, list[dict[str, Any]]] = {
        str(row["id"]): [] for row in boards
    }

    def realm_rows(table_name: str) -> list[sqlite3.Row]:
        return connection.execute(
            f'SELECT source.* FROM "{table_name}" AS source '
            "INNER JOIN boards AS board ON board.id = source.board_id "
            "WHERE board.realm_id = ? "
            "ORDER BY source.board_id COLLATE BINARY, "
            "source.created_at ASC, source.id COLLATE BINARY",
            (normalized_realm_id,),
        ).fetchall()

    for artifact_type, table, status_col, content_cols in ARTIFACT_QUERIES:
        for row in realm_rows(table):
            board_id = str(row["board_id"])
            row_id = str(row["id"])
            version_raw = row["version"] if "version" in row.keys() else 1
            source_version = str(version_raw if version_raw is not None else 1)
            source_row: dict[str, Any] = {
                "artifact_type": artifact_type,
                "id": row_id,
                "source_ref": f"{artifact_type}:{row_id}",
                "source_version": source_version,
                "content_hash": canonical_content_hash(row, content_cols),
                "created_at": to_iso(row["created_at"]),
                "updated_at": updated_at(row),
                "status": row_status(row, status_col),
                "source_artifact_status": row_status(row, status_col),
                "has_minimal_evidence": True,
            }
            if artifact_type == "spec":
                source_row["content_hash_v1"] = canonical_content_hash(
                    row, SPEC_CONTENT_COLUMNS_V1
                )
                source_row["source_manifest_version"] = SPEC_SOURCE_MANIFEST_VERSION
            working_ttl_days = ttl_by_board[board_id]
            if working_ttl_days is not None:
                source_row["working_ttl_days"] = working_ttl_days
            captured[board_id].append(source_row)
            if artifact_type == "spec":
                captured[board_id].extend(decision_sources_from_spec(row))

    for row in realm_rows("cards"):
        board_id = str(row["board_id"])
        row_id = str(row["id"])
        artifact_type = card_artifact_type(row)
        source_row = {
            "artifact_type": artifact_type,
            "id": row_id,
            "source_ref": f"{artifact_type}:{row_id}",
            "source_version": "1",
            "content_hash": canonical_content_hash(row, CARD_CONTENT_COLUMNS),
            "created_at": to_iso(row["created_at"]),
            "updated_at": updated_at(row),
            "status": row_status(row),
            "source_artifact_status": row_status(row),
            "has_minimal_evidence": bug_has_minimal_evidence(row),
        }
        working_ttl_days = ttl_by_board[board_id]
        if working_ttl_days is not None:
            source_row["working_ttl_days"] = working_ttl_days
        captured[board_id].append(source_row)

    for row in realm_rows("amendment_hotfix_revisions"):
        board_id = str(row["board_id"])
        row_id = str(row["id"])
        lineage_raw = row["lineage_state"] if "lineage_state" in row.keys() else None
        source_row = {
            "artifact_type": "amendment_hotfix_revision",
            "id": row_id,
            "source_ref": f"amendment_hotfix_revision:{row_id}",
            "source_version": "1",
            "content_hash": canonical_content_hash(row, AMENDMENT_CONTENT_COLUMNS),
            "created_at": to_iso(row["created_at"]),
            "updated_at": updated_at(row),
            "status": row_status(row, "status"),
            "source_artifact_status": row_status(row, "status"),
            "lineage_complete": str(lineage_raw or "").strip().lower() == "complete",
        }
        working_ttl_days = ttl_by_board[board_id]
        if working_ttl_days is not None:
            source_row["working_ttl_days"] = working_ttl_days
        captured[board_id].append(source_row)

    return board_rows, {
        board_id: tuple(rows) for board_id, rows in captured.items()
    }


def read_realm_cognitive_source_snapshot(
    connection: sqlite3.Connection,
    *,
    realm_id: str,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Capture durable cognitive rows for the same caller-owned transaction."""

    normalized_realm_id = str(realm_id).strip()
    if not normalized_realm_id:
        raise ValueError("realm_id must be non-empty")
    board_ids = tuple(
        str(row["id"])
        for row in connection.execute(
            "SELECT id FROM boards WHERE realm_id = ? ORDER BY id COLLATE BINARY",
            (normalized_realm_id,),
        ).fetchall()
    )
    captured: dict[str, list[dict[str, Any]]] = {
        board_id: [] for board_id in board_ids
    }
    rows = connection.execute(
        "SELECT source.board_id, source.node_id, source.node_type, "
        "source.generation, source.payload, source.committed_at "
        "FROM kg_cognitive_sources AS source "
        "INNER JOIN boards AS board ON board.id = source.board_id "
        "WHERE board.realm_id = ? "
        "ORDER BY source.board_id COLLATE BINARY, source.committed_at ASC, "
        "source.node_id COLLATE BINARY, source.generation ASC",
        (normalized_realm_id,),
    ).fetchall()
    for row in rows:
        captured[str(row["board_id"])].append(
            {
                "node_id": str(row["node_id"]),
                "node_type": str(row["node_type"]),
                "generation": int(row["generation"]),
                "payload": row["payload"],
                "committed_at": row["committed_at"],
            }
        )
    return {
        board_id: tuple(records) for board_id, records in captured.items()
    }


@dataclass(frozen=True, slots=True)
class CommunityBoardSourceReader:
    """Read SDLC artifacts from the Community-owned SQLite pulse database."""

    db_path: Path | None = None
    db_path_provider: Callable[[], Path] | None = None

    def _path(self) -> Path:
        if self.db_path is not None:
            return Path(self.db_path)
        if self.db_path_provider is not None:
            return Path(self.db_path_provider())
        return resolve_pulse_db_path()

    def fetch(self, board_id: str) -> BoardSourceSnapshot:
        db_path = self._path()
        if not db_path.exists():
            logger.warning(
                "kg.board_source_reader.db_missing path=%s - snapshot incomplete",
                db_path,
            )
            return BoardSourceSnapshot(rows=(), complete=False, cause="db_missing")

        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro&immutable=0",
                uri=True,
                timeout=5.0,
            )
        except sqlite3.Error as exc:
            raise SourceUnavailableError(
                "board source database could not be opened",
                cause_type=type(exc).__name__,
            ) from exc

        conn.row_factory = sqlite3.Row
        try:
            # sqlite3 does not open a transaction for a SELECT by default.  An
            # explicit read transaction keeps schema preflight and row
            # collection pinned to one coherent database snapshot.
            conn.execute("BEGIN")
            return self._fetch_conn(conn, board_id)
        except sqlite3.Error as exc:
            raise SourceReadFailure(
                "board source rows could not be read",
                cause_type=type(exc).__name__,
            ) from exc
        finally:
            conn.close()

    def _fetch_conn(
        self,
        conn: sqlite3.Connection,
        board_id: str,
    ) -> BoardSourceSnapshot:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing_tables = sorted(_REQUIRED_SOURCE_TABLES - tables)
        if missing_tables:
            logger.warning(
                "kg.board_source_reader.table_missing tables=%s - snapshot incomplete",
                ",".join(missing_tables),
            )
            return BoardSourceSnapshot(
                rows=(),
                complete=False,
                cause="table_missing",
            )

        missing_columns: dict[str, list[str]] = {}
        for table, required in _REQUIRED_SOURCE_COLUMNS.items():
            missing = required - _table_columns(conn, table)
            if missing:
                missing_columns[table] = sorted(missing)
        if missing_columns:
            details = ",".join(
                f"{table}:[{'|'.join(columns)}]"
                for table, columns in sorted(missing_columns.items())
            )
            logger.warning(
                "kg.board_source_reader.realm_incomplete board_id=%s "
                "reason=required_columns_missing columns=%s",
                board_id,
                details,
            )
            return BoardSourceSnapshot(
                rows=(),
                complete=False,
                cause="realm_incomplete",
            )

        board = conn.execute(
            "SELECT 1 FROM boards WHERE id = ?",
            (board_id,),
        ).fetchone()
        if board is None:
            logger.warning(
                "kg.board_source_reader.realm_incomplete board_id=%s "
                "reason=board_unproven",
                board_id,
            )
            return BoardSourceSnapshot(
                rows=(),
                complete=False,
                cause="realm_incomplete",
            )

        out: list[dict[str, Any]] = []
        working_ttl_days = _board_working_ttl_days(conn, board_id)
        for artifact_type, table, status_col, content_cols in ARTIFACT_QUERIES:
            rows = conn.execute(
                f"SELECT * FROM {table} "
                f"WHERE board_id = ? "
                f"ORDER BY created_at ASC, id ASC",
                (board_id,),
            ).fetchall()
            for row in rows:
                row_id = str(row["id"])
                version_raw = row["version"] if "version" in row.keys() else 1
                source_version = str(version_raw if version_raw is not None else 1)
                content_hash = canonical_content_hash(row, content_cols)
                source_row = {
                    "artifact_type": artifact_type,
                    "id": row_id,
                    "source_ref": f"{artifact_type}:{row_id}",
                    "source_version": source_version,
                    "content_hash": content_hash,
                    "created_at": to_iso(row["created_at"]),
                    "updated_at": updated_at(row),
                    "status": row_status(row, status_col),
                    "source_artifact_status": row_status(row, status_col),
                    "has_minimal_evidence": True,
                }
                if artifact_type == "spec":
                    source_row["content_hash_v1"] = canonical_content_hash(
                        row, SPEC_CONTENT_COLUMNS_V1
                    )
                    source_row["source_manifest_version"] = SPEC_SOURCE_MANIFEST_VERSION
                if working_ttl_days is not None:
                    source_row["working_ttl_days"] = working_ttl_days
                out.append(source_row)
                if artifact_type == "spec":
                    out.extend(decision_sources_from_spec(row))
        self._append_card_rows(conn, board_id, working_ttl_days, out)
        self._append_amendment_rows(conn, board_id, working_ttl_days, out)
        return BoardSourceSnapshot(rows=tuple(out), complete=True, cause=None)

    def _append_card_rows(
        self,
        conn: sqlite3.Connection,
        board_id: str,
        working_ttl_days: int | None,
        out: list[dict[str, Any]],
    ) -> None:
        rows = conn.execute(
            "SELECT * FROM cards "
            "WHERE board_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (board_id,),
        ).fetchall()
        for row in rows:
            row_id = str(row["id"])
            artifact_type = card_artifact_type(row)
            source_row = {
                "artifact_type": artifact_type,
                "id": row_id,
                "source_ref": f"{artifact_type}:{row_id}",
                "source_version": "1",
                "content_hash": canonical_content_hash(row, CARD_CONTENT_COLUMNS),
                "created_at": to_iso(row["created_at"]),
                "updated_at": updated_at(row),
                "status": row_status(row),
                "source_artifact_status": row_status(row),
                "has_minimal_evidence": bug_has_minimal_evidence(row),
            }
            if working_ttl_days is not None:
                source_row["working_ttl_days"] = working_ttl_days
            out.append(source_row)

    def _append_amendment_rows(
        self,
        conn: sqlite3.Connection,
        board_id: str,
        working_ttl_days: int | None,
        out: list[dict[str, Any]],
    ) -> None:
        rows = conn.execute(
            "SELECT * FROM amendment_hotfix_revisions "
            "WHERE board_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (board_id,),
        ).fetchall()
        for row in rows:
            row_id = str(row["id"])
            lineage_raw = row["lineage_state"] if "lineage_state" in row.keys() else None
            source_row = {
                "artifact_type": "amendment_hotfix_revision",
                "id": row_id,
                "source_ref": f"amendment_hotfix_revision:{row_id}",
                "source_version": "1",
                "content_hash": canonical_content_hash(row, AMENDMENT_CONTENT_COLUMNS),
                "created_at": to_iso(row["created_at"]),
                "updated_at": updated_at(row),
                "status": row_status(row, "status"),
                "source_artifact_status": row_status(row, "status"),
                "lineage_complete": str(lineage_raw or "").strip().lower() == "complete",
            }
            if working_ttl_days is not None:
                source_row["working_ttl_days"] = working_ttl_days
            out.append(source_row)


# Backwards-compatible adapter-local name for tests and older Community imports.
BoardSourceStore = CommunityBoardSourceReader


__all__ = [
    "ARTIFACT_QUERIES",
    "BoardSourceStore",
    "CommunityBoardSourceReader",
    "read_realm_cognitive_source_snapshot",
    "read_realm_source_snapshot",
    "resolve_pulse_db_path",
]
