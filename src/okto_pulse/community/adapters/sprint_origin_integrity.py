"""Read-only relational integrity diagnostics for sprint hotfix lineage.

This adapter intentionally reports schema/data drift without repairing it.  In
particular, legacy SQLite databases can contain the lineage columns without the
two ``ON DELETE SET NULL`` foreign keys that fresh installations receive.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


_EXPECTED_FOREIGN_KEYS = {
    "origin_sprint_id": ("sprints", "id", "SET NULL"),
    "origin_bug_id": ("cards", "id", "SET NULL"),
}
_SAMPLE_LIMIT = 20


def _probe_failure(
    error: BaseException, *, backend: str | None = None
) -> dict[str, Any]:
    """Return a bounded failure finding; never expose connection details."""

    return {
        "id": "sprint_origin_integrity",
        "status": "critical",
        "severity": "critical",
        "schema": {
            "backend": backend,
            "inspection_supported": False,
            "expected_foreign_key_count": len(_EXPECTED_FOREIGN_KEYS),
            "valid_foreign_key_count": 0,
            "issues": [
                {
                    "column": None,
                    "reason": "probe_failure",
                    "error_class": type(error).__name__,
                }
            ],
        },
        "data": {
            "violation_count": 1,
            "counts": {"probe_failure": 1},
            "sample_sprint_ids_by_violation": {},
        },
        "repair_policy": {
            "direct_sql_supported": False,
            "supported_path": "application_workflows_or_verified_backup_restore",
        },
    }


def _normalize(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _inspect_foreign_keys(sync_connection) -> list[dict[str, Any]]:
    inspector = sa_inspect(sync_connection)
    return list(inspector.get_foreign_keys("sprints"))


def _evaluate_foreign_keys(
    foreign_keys: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    valid_count = 0
    issues: list[dict[str, Any]] = []
    for column, (expected_table, expected_column, expected_on_delete) in (
        _EXPECTED_FOREIGN_KEYS.items()
    ):
        candidates = [
            fk
            for fk in foreign_keys
            if list(fk.get("constrained_columns") or []) == [column]
        ]
        if not candidates:
            issues.append({"column": column, "reason": "missing"})
            continue

        fk = candidates[0]
        referred_columns = list(fk.get("referred_columns") or [])
        if (
            fk.get("referred_table") != expected_table
            or referred_columns != [expected_column]
        ):
            issues.append(
                {
                    "column": column,
                    "reason": "wrong_target",
                    "expected": f"{expected_table}.{expected_column}",
                }
            )
            continue

        options = fk.get("options") or {}
        on_delete = str(options.get("ondelete") or "").upper().replace("_", " ")
        if on_delete != expected_on_delete:
            issues.append(
                {
                    "column": column,
                    "reason": "wrong_on_delete",
                    "expected": expected_on_delete,
                    "actual": on_delete or None,
                }
            )
            continue
        valid_count += 1

    return valid_count, issues


def _evaluate_rows(rows: list[dict[str, Any]]) -> tuple[int, dict, dict]:
    counts: dict[str, int] = defaultdict(int)
    samples: dict[str, list[str]] = defaultdict(list)

    def add(code: str, sprint_id: str) -> None:
        counts[code] += 1
        if len(samples[code]) < _SAMPLE_LIMIT:
            samples[code].append(sprint_id)

    for row in rows:
        sprint_id = str(row["sprint_id"])
        lane_type = _normalize(row["lane_type"])
        origin_sprint_id = row["origin_sprint_id"]
        origin_bug_id = row["origin_bug_id"]

        if row["spec_row_id"] is None:
            add("sprint_spec_orphan", sprint_id)

        if lane_type not in {"normal", "hotfix"}:
            add("invalid_lane_type", sprint_id)
        if lane_type == "normal" and (origin_sprint_id or origin_bug_id):
            add("normal_has_origins", sprint_id)

        origin_sprint_valid = False
        if origin_sprint_id:
            if row["origin_sprint_row_id"] is None:
                add("origin_sprint_orphan", sprint_id)
            else:
                if row["origin_sprint_row_id"] == row["sprint_id"]:
                    add("origin_sprint_self", sprint_id)
                if row["origin_sprint_board_id"] != row["board_id"]:
                    add("origin_sprint_wrong_board", sprint_id)
                if row["origin_sprint_spec_id"] != row["spec_id"]:
                    add("origin_sprint_wrong_spec", sprint_id)
                origin_sprint_valid = bool(
                    row["origin_sprint_row_id"] != row["sprint_id"]
                    and row["origin_sprint_board_id"] == row["board_id"]
                    and row["origin_sprint_spec_id"] == row["spec_id"]
                )

        if origin_bug_id:
            if row["origin_bug_row_id"] is None:
                add("origin_bug_orphan", sprint_id)
            else:
                if row["origin_bug_board_id"] != row["board_id"]:
                    add("origin_bug_wrong_board", sprint_id)
                if row["origin_bug_spec_id"] != row["spec_id"]:
                    add("origin_bug_wrong_spec", sprint_id)
                if _normalize(row["origin_bug_type"]) != "bug":
                    add("origin_bug_wrong_type", sprint_id)
        elif lane_type == "hotfix":
            add("hotfix_missing_origin_bug", sprint_id)

        if lane_type == "hotfix":
            spec_done = _normalize(row["spec_status"]) == "done"
            closed_origin = bool(
                origin_sprint_valid
                and _normalize(row["origin_sprint_status"]) == "closed"
            )
            if not spec_done and not closed_origin:
                add("hotfix_not_eligible", sprint_id)

    ordered_counts = {key: counts[key] for key in sorted(counts)}
    ordered_samples = {
        key: sorted(samples[key]) for key in sorted(samples)
    }
    return sum(ordered_counts.values()), ordered_counts, ordered_samples


async def inspect_sprint_origin_integrity(
    engine_or_factory: AsyncEngine | Callable[[], AsyncEngine],
) -> dict[str, Any]:
    """Inspect sprint lineage constraints and rows without mutating the database."""

    backend: str | None = None
    try:
        engine = engine_or_factory() if callable(engine_or_factory) else engine_or_factory
        backend = str(engine.dialect.name)
        async with engine.connect() as connection:
            foreign_keys = await connection.run_sync(_inspect_foreign_keys)
            result = await connection.execute(
                text(
                    """
                    SELECT
                        s.id AS sprint_id,
                        s.board_id AS board_id,
                        s.spec_id AS spec_id,
                        s.status AS sprint_status,
                        s.lane_type AS lane_type,
                        s.origin_sprint_id AS origin_sprint_id,
                        s.origin_bug_id AS origin_bug_id,
                        sp.id AS spec_row_id,
                        sp.status AS spec_status,
                        os.id AS origin_sprint_row_id,
                        os.board_id AS origin_sprint_board_id,
                        os.spec_id AS origin_sprint_spec_id,
                        os.status AS origin_sprint_status,
                        ob.id AS origin_bug_row_id,
                        ob.board_id AS origin_bug_board_id,
                        ob.spec_id AS origin_bug_spec_id,
                        ob.card_type AS origin_bug_type
                    FROM sprints AS s
                    LEFT JOIN specs AS sp ON sp.id = s.spec_id
                    LEFT JOIN sprints AS os ON os.id = s.origin_sprint_id
                    LEFT JOIN cards AS ob ON ob.id = s.origin_bug_id
                    ORDER BY s.id
                    """
                )
            )
            rows = [dict(row._mapping) for row in result]
    except Exception as error:
        return _probe_failure(error, backend=backend)

    valid_fk_count, schema_issues = _evaluate_foreign_keys(foreign_keys)
    violation_count, counts, samples = _evaluate_rows(rows)
    if violation_count:
        status = "critical"
        severity = "critical"
    elif schema_issues:
        status = "degraded"
        severity = "warning"
    else:
        status = "healthy"
        severity = "info"

    return {
        "id": "sprint_origin_integrity",
        "status": status,
        "severity": severity,
        "schema": {
            "backend": backend,
            "inspection_supported": True,
            "expected_foreign_key_count": len(_EXPECTED_FOREIGN_KEYS),
            "valid_foreign_key_count": valid_fk_count,
            "issues": schema_issues,
        },
        "data": {
            "violation_count": violation_count,
            "counts": counts,
            "sample_sprint_ids_by_violation": samples,
        },
        "repair_policy": {
            "direct_sql_supported": False,
            "supported_path": "application_workflows_or_verified_backup_restore",
        },
    }


__all__ = ["inspect_sprint_origin_integrity"]
