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
    SPEC_CONTENT_COLUMNS_V1,
    SPEC_CONTENT_COLUMNS_V2,
    SPEC_SOURCE_MANIFEST_VERSION,
    _bug_has_minimal_evidence,
    _canonical_content_hash,
    _card_artifact_type,
    _decision_sources_from_spec,
    _row_status,
    _to_iso,
    _updated_at,
)
from okto_pulse.core.kg.interfaces.board_source_reader import (
    SourceReadFailure,
    SourceUnavailableError,
)

logger = logging.getLogger("okto_pulse.community.board_source_reader")


# SQL table ownership lives in the edition adapter. Core retains only the DTO
# and hash rules consumed above.
ARTIFACT_QUERIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "story",
        "stories",
        "status",
        (
            "title",
            "description",
            "actor",
            "goal",
            "benefit",
            "topic_id",
            "status",
            "labels",
        ),
    ),
    (
        "ideation",
        "ideations",
        "status",
        (
            "title",
            "description",
            "problem_statement",
            "proposed_approach",
            "scope_assessment",
            "complexity",
            "status",
            "version",
            "labels",
        ),
    ),
    ("spec", "specs", "status", SPEC_CONTENT_COLUMNS_V2),
    (
        "refinement",
        "refinements",
        "status",
        (
            "title",
            "description",
            "in_scope",
            "out_of_scope",
            "analysis",
            "decisions",
            "status",
            "version",
            "labels",
        ),
    ),
    (
        "sprint",
        "sprints",
        "status",
        (
            "title",
            "description",
            "spec_id",
            "spec_version",
            "status",
            "lane_type",
            "objective",
            "expected_outcome",
            "test_scenario_ids",
            "business_rule_ids",
            "evaluations",
            "version",
            "labels",
        ),
    ),
)


def resolve_pulse_db_path() -> Path:
    """Return the SQLite file targeted by the configured SQLAlchemy engine."""

    try:
        from okto_pulse.core.infra.database import get_engine

        url = str(get_engine().url)
    except Exception:
        return Path.home() / ".okto-pulse" / "data" / "pulse.db"
    marker = ":///"
    idx = url.rfind(marker)
    if idx < 0:
        return Path.home() / ".okto-pulse" / "data" / "pulse.db"
    return Path(url[idx + len(marker):])


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

    def fetch(self, board_id: str) -> list[dict[str, Any]]:
        db_path = self._path()
        if not db_path.exists():
            logger.warning(
                "kg.board_source_reader.db_missing path=%s - returning empty",
                db_path,
            )
            return []

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
            return self._fetch_conn(conn, board_id)
        except sqlite3.Error as exc:
            raise SourceReadFailure(
                "board source rows could not be read",
                cause_type=type(exc).__name__,
            ) from exc
        finally:
            conn.close()

    def _fetch_conn(self, conn: sqlite3.Connection, board_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        working_ttl_days = _board_working_ttl_days(conn, board_id)
        for artifact_type, table, status_col, content_cols in ARTIFACT_QUERIES:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                logger.warning(
                    "kg.board_source_reader.table_missing table=%s - skipped",
                    table,
                )
                continue
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
                content_hash = _canonical_content_hash(row, content_cols)
                source_row = {
                    "artifact_type": artifact_type,
                    "id": row_id,
                    "source_ref": f"{artifact_type}:{row_id}",
                    "source_version": source_version,
                    "content_hash": content_hash,
                    "created_at": _to_iso(row["created_at"]),
                    "updated_at": _updated_at(row),
                    "status": _row_status(row, status_col),
                    "source_artifact_status": _row_status(row, status_col),
                    "has_minimal_evidence": True,
                }
                if artifact_type == "spec":
                    source_row["content_hash_v1"] = _canonical_content_hash(
                        row, SPEC_CONTENT_COLUMNS_V1
                    )
                    source_row["source_manifest_version"] = SPEC_SOURCE_MANIFEST_VERSION
                if working_ttl_days is not None:
                    source_row["working_ttl_days"] = working_ttl_days
                out.append(source_row)
                if artifact_type == "spec":
                    out.extend(_decision_sources_from_spec(row))
        self._append_card_rows(conn, board_id, working_ttl_days, out)
        self._append_amendment_rows(conn, board_id, working_ttl_days, out)
        return out

    def _append_card_rows(
        self,
        conn: sqlite3.Connection,
        board_id: str,
        working_ttl_days: int | None,
        out: list[dict[str, Any]],
    ) -> None:
        cards_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cards'"
        ).fetchone()
        if not cards_exists:
            logger.warning(
                "kg.board_source_reader.table_missing table=cards - skipped",
            )
            return
        rows = conn.execute(
            "SELECT * FROM cards "
            "WHERE board_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (board_id,),
        ).fetchall()
        for row in rows:
            row_id = str(row["id"])
            artifact_type = _card_artifact_type(row)
            source_row = {
                "artifact_type": artifact_type,
                "id": row_id,
                "source_ref": f"{artifact_type}:{row_id}",
                "source_version": "1",
                "content_hash": _canonical_content_hash(row, CARD_CONTENT_COLUMNS),
                "created_at": _to_iso(row["created_at"]),
                "updated_at": _updated_at(row),
                "status": _row_status(row),
                "source_artifact_status": _row_status(row),
                "has_minimal_evidence": _bug_has_minimal_evidence(row),
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
        amendments_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='amendment_hotfix_revisions'"
        ).fetchone()
        if not amendments_exists:
            logger.debug(
                "kg.board_source_reader.table_missing "
                "table=amendment_hotfix_revisions - skipped",
            )
            return
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
                "content_hash": _canonical_content_hash(row, AMENDMENT_CONTENT_COLUMNS),
                "created_at": _to_iso(row["created_at"]),
                "updated_at": _updated_at(row),
                "status": _row_status(row, "status"),
                "source_artifact_status": _row_status(row, "status"),
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
    "resolve_pulse_db_path",
]
