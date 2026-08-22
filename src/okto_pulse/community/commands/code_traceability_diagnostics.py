"""Read-only operator diagnostics for agent-attested Code Traceability.

The command surface in this module intentionally observes only Pulse-owned
relational state.  It does not acquire source code or invoke an investigation;
those actions belong to the authenticated external agent that submits a
receipt.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ValidationError

from okto_pulse.core.models.schemas import BoardSettings, CodeTraceabilitySettings


DiagnosticKind = Literal["request", "receipt"]

_TRACEABILITY_TABLES = (
    "code_investigation_requests",
    "code_investigation_receipts",
    "code_investigation_receipt_revocations",
    "code_investigation_heads",
    "code_evidence",
    "code_evidence_spec_links",
    "code_evidence_dispositions",
    "implementation_targets",
    "implementation_target_spec_links",
    "implementation_target_evidence_links",
    "implementation_target_resolutions",
    "implementation_target_execution_records",
    "target_overlap_acknowledgements",
    "code_traceability_waivers",
)
_REQUEST_STATUSES = frozenset({"open", "consumed", "expired", "revoked"})
_MAX_PAGE_SIZE = 200


class CodeTraceabilityDiagnosticsError(RuntimeError):
    """Stable operator-facing failure raised without mutating persisted state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def open_read_only_database(database_path: str) -> sqlite3.Connection:
    """Open the Community SQLite database in fail-closed read-only mode."""

    normalized_path = str(database_path).strip().replace("\\", "/")
    if not normalized_path:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_database_not_found",
            "Community database path is not configured",
        )
    try:
        connection = sqlite3.connect(
            f"file:{normalized_path}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as exc:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_database_not_found",
            "Community database was not found or could not be opened read-only",
        ) from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_diagnostics_limit_invalid",
            f"limit must be between 1 and {_MAX_PAGE_SIZE}",
        )
    return limit


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


def list_requests(
    connection: sqlite3.Connection,
    *,
    board_id: str,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List bounded request metadata; challenge material is never returned."""

    bounded_limit = _bounded_limit(limit)
    if status is not None and status not in _REQUEST_STATUSES:
        raise CodeTraceabilityDiagnosticsError(
            "code_investigation_request_status_invalid",
            "status must be open, consumed, expired, or revoked",
        )
    predicate = "board_id = ?"
    parameters: list[Any] = [board_id]
    if status is not None:
        predicate += " AND status = ?"
        parameters.append(status)
    parameters.append(bounded_limit)
    try:
        return _rows(
            connection.execute(
                "SELECT id, board_id, subject_type, subject_id, subject_version, "
                "issued_to_actor_id, source_ref, required_capabilities, "
                "expected_head_generation, expected_predecessor_receipt_id, "
                "canonicalization_profile, limits_profile, challenge_key_id, "
                "single_use, status, expires_at, requested_by, created_at, "
                "consumed_at "
                "FROM code_investigation_requests "
                f"WHERE {predicate} ORDER BY created_at DESC, id DESC LIMIT ?",
                parameters,
            )
        )
    except sqlite3.Error as exc:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_schema_invalid",
            "Code Investigation request storage is unavailable",
        ) from exc


def list_receipts(
    connection: sqlite3.Connection,
    *,
    board_id: str,
    outcome: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List receipt attestations and current/revoked lineage metadata."""

    bounded_limit = _bounded_limit(limit)
    if outcome is not None and outcome not in {"accessible", "partial", "unavailable"}:
        raise CodeTraceabilityDiagnosticsError(
            "code_investigation_receipt_outcome_invalid",
            "outcome must be accessible, partial, or unavailable",
        )
    predicate = "receipt.board_id = ?"
    parameters: list[Any] = [board_id]
    if outcome is not None:
        predicate += " AND receipt.outcome = ?"
        parameters.append(outcome)
    parameters.append(bounded_limit)
    try:
        return _rows(
            connection.execute(
                "SELECT receipt.id, receipt.request_id, receipt.board_id, "
                "receipt.subject_type, receipt.subject_id, receipt.subject_version, "
                "receipt.attestor_actor_id, receipt.generation, "
                "receipt.predecessor_receipt_id, receipt.trust_level, "
                "receipt.acceptance_status, receipt.outcome, receipt.capabilities, "
                "receipt.source_ref, receipt.declared_revision, "
                "receipt.workspace_state_id, receipt.declared_dirty, "
                "receipt.reproducibility_claim, receipt.omission_count, "
                "receipt.observed_at, receipt.received_at, receipt.expires_at, "
                "CASE WHEN revocation.id IS NULL THEN 0 ELSE 1 END AS revoked, "
                "CASE WHEN head.current_receipt_id = receipt.id "
                "THEN 1 ELSE 0 END AS current_head "
                "FROM code_investigation_receipts AS receipt "
                "LEFT JOIN code_investigation_receipt_revocations AS revocation "
                "ON revocation.receipt_id = receipt.id "
                "LEFT JOIN code_investigation_heads AS head "
                "ON head.board_id = receipt.board_id "
                "AND head.source_ref = receipt.source_ref "
                f"WHERE {predicate} "
                "ORDER BY receipt.received_at DESC, receipt.id DESC LIMIT ?",
                parameters,
            )
        )
    except sqlite3.Error as exc:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_schema_invalid",
            "Code Investigation receipt storage is unavailable",
        ) from exc


def inspect_record(
    connection: sqlite3.Connection,
    *,
    board_id: str,
    kind: DiagnosticKind,
    record_id: str,
) -> dict[str, Any]:
    """Inspect one persisted request or receipt without returning secret digests."""

    try:
        if kind == "request":
            row = connection.execute(
                "SELECT id, board_id, subject_type, subject_id, subject_version, "
                "issued_to_actor_id, source_ref, required_capabilities, "
                "expected_head_generation, expected_predecessor_receipt_id, "
                "canonicalization_profile, limits_profile, challenge_key_id, "
                "single_use, status, expires_at, requested_by, created_at, "
                "consumed_at FROM code_investigation_requests "
                "WHERE board_id = ? AND id = ?",
                (board_id, record_id),
            ).fetchone()
        elif kind == "receipt":
            row = connection.execute(
                "SELECT receipt.id, receipt.request_id, receipt.board_id, "
                "receipt.subject_type, receipt.subject_id, receipt.subject_version, "
                "receipt.attestor_actor_id, receipt.generation, "
                "receipt.predecessor_receipt_id, receipt.trust_level, "
                "receipt.acceptance_status, receipt.outcome, receipt.capabilities, "
                "receipt.source_ref, receipt.declared_revision, "
                "receipt.workspace_state_id, receipt.declared_dirty, "
                "receipt.reproducibility_claim, receipt.omission_count, "
                "receipt.observed_at, receipt.received_at, receipt.expires_at, "
                "CASE WHEN revocation.id IS NULL THEN 0 ELSE 1 END AS revoked, "
                "CASE WHEN head.current_receipt_id = receipt.id "
                "THEN 1 ELSE 0 END AS current_head "
                "FROM code_investigation_receipts AS receipt "
                "LEFT JOIN code_investigation_receipt_revocations AS revocation "
                "ON revocation.receipt_id = receipt.id "
                "LEFT JOIN code_investigation_heads AS head "
                "ON head.board_id = receipt.board_id "
                "AND head.source_ref = receipt.source_ref "
                "WHERE receipt.board_id = ? AND receipt.id = ?",
                (board_id, record_id),
            ).fetchone()
        else:
            raise CodeTraceabilityDiagnosticsError(
                "code_traceability_diagnostics_kind_invalid",
                "kind must be request or receipt",
            )
    except sqlite3.Error as exc:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_schema_invalid",
            f"Code Investigation {kind} storage is unavailable",
        ) from exc
    if row is None:
        raise CodeTraceabilityDiagnosticsError(
            f"code_investigation_{kind}_not_found",
            f"{kind.capitalize()} not found for the selected board",
        )
    return dict(row)


def _decode_settings(raw: Any) -> Mapping[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return raw
    try:
        decoded = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_policy_invalid",
            "Board settings are not valid JSON",
        ) from exc
    if not isinstance(decoded, Mapping):
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_policy_invalid",
            "Board settings must be a JSON object",
        )
    return decoded


def validate_policy(
    connection: sqlite3.Connection,
    *,
    board_id: str,
) -> dict[str, Any]:
    """Validate the persisted board policy using the Core-owned schema."""

    try:
        row = connection.execute(
            "SELECT settings FROM boards WHERE id = ?",
            (board_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise CodeTraceabilityDiagnosticsError(
            "code_traceability_schema_invalid",
            "Board policy storage is unavailable",
        ) from exc
    if row is None:
        raise CodeTraceabilityDiagnosticsError(
            "board_not_found",
            "Board not found",
        )
    decoded = dict(_decode_settings(row["settings"]))
    raw_policy = decoded.get("code_traceability")
    legacy_default_applied = (
        "code_traceability" not in decoded
        or raw_policy is None
        or (isinstance(raw_policy, Mapping) and raw_policy.get("mode") == "off")
    )
    try:
        if legacy_default_applied:
            decoded["code_traceability"] = CodeTraceabilitySettings.from_persisted(
                raw_policy
            ).model_dump(mode="json")
        settings = BoardSettings.model_validate(decoded)
    except ValidationError as exc:
        return {
            "valid": False,
            "effective_mode": "invalid",
            "errors": [
                {
                    "path": ".".join(str(part) for part in error["loc"]),
                    "type": error["type"],
                }
                for error in exc.errors(include_input=False, include_url=False)
            ],
        }
    policy = settings.code_traceability
    return {
        "valid": True,
        "effective_mode": policy.mode.value,
        "legacy_default_applied": legacy_default_applied,
        "policy": policy.model_dump(mode="json"),
        "responsibility_boundary": "external_authenticated_agent",
    }


def validate_schema(connection: sqlite3.Connection) -> dict[str, Any]:
    """Validate the Code Traceability table/trigger census and FK integrity."""

    from okto_pulse.community.adapters.relational_schema_steps import (
        code_traceability_sqlite_trigger_manifest,
    )

    available_tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    expected_triggers = set(code_traceability_sqlite_trigger_manifest())
    available_triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'trg_code_traceability_v1_%'"
        ).fetchall()
    }
    missing_tables = sorted(set(_TRACEABILITY_TABLES) - available_tables)
    missing_triggers = sorted(expected_triggers - available_triggers)
    unexpected_triggers = sorted(available_triggers - expected_triggers)
    foreign_key_violations = [
        {
            "table": row[0],
            "rowid": row[1],
            "parent": row[2],
            "constraint": row[3],
        }
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        if row[0] in _TRACEABILITY_TABLES
    ]
    valid = not missing_tables and not missing_triggers and not foreign_key_violations
    return {
        "valid": valid,
        "expected_table_count": len(_TRACEABILITY_TABLES),
        "expected_trigger_count": len(expected_triggers),
        "missing_tables": missing_tables,
        "missing_triggers": missing_triggers,
        "unexpected_triggers": unexpected_triggers,
        "foreign_key_violations": foreign_key_violations,
    }


def diagnose(
    connection: sqlite3.Connection,
    *,
    board_id: str,
) -> dict[str, Any]:
    """Return a bounded, read-only health report for persisted attestations."""

    schema = validate_schema(connection)
    policy = validate_policy(connection, board_id=board_id)
    counts: dict[str, int] = {}
    if not schema["missing_tables"]:
        for table in _TRACEABILITY_TABLES:
            counts[table] = int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE board_id = ?',
                    (board_id,),
                ).fetchone()[0]
            )
    open_request_count = 0
    maximum_actor_open_requests = 0
    open_request_limit_offenders: list[dict[str, Any]] = []
    if not schema["missing_tables"]:
        open_request_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM code_investigation_requests "
                "WHERE board_id = ? AND status = 'open' "
                "AND julianday(expires_at) > julianday('now')",
                (board_id,),
            ).fetchone()[0]
        )
        actor_counts = _rows(
            connection.execute(
                "SELECT issued_to_actor_id, COUNT(*) AS open_request_count "
                "FROM code_investigation_requests "
                "WHERE board_id = ? AND status = 'open' "
                "AND julianday(expires_at) > julianday('now') "
                "GROUP BY issued_to_actor_id "
                "ORDER BY open_request_count DESC, issued_to_actor_id",
                (board_id,),
            )
        )
        maximum_actor_open_requests = max(
            (int(row["open_request_count"]) for row in actor_counts),
            default=0,
        )
        open_request_limit_offenders = [
            row for row in actor_counts if int(row["open_request_count"]) > 8
        ]
    checks = {
        "schema": bool(schema["valid"]),
        "policy": bool(policy["valid"]),
        "open_request_limit": not open_request_limit_offenders,
    }
    return {
        "board_id": board_id,
        "healthy": all(checks.values()),
        "checks": checks,
        "schema": schema,
        "policy": policy,
        "open_request_count": open_request_count,
        "maximum_actor_open_requests": maximum_actor_open_requests,
        "open_request_limit_offenders": open_request_limit_offenders,
        "persisted_counts": counts,
        "source_investigation_performed": False,
    }


__all__ = [
    "CodeTraceabilityDiagnosticsError",
    "diagnose",
    "inspect_record",
    "list_receipts",
    "list_requests",
    "open_read_only_database",
    "validate_policy",
    "validate_schema",
]
