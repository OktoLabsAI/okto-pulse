from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from okto_pulse.community.commands.code_traceability_diagnostics import (
    CodeTraceabilityDiagnosticsError,
    diagnose,
    inspect_record,
    list_requests,
    open_read_only_database,
    validate_policy,
)


REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = Path(__file__).parent.parent.parent / "okto_labs_pulse_core" / "src"


def _request_database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE boards (id TEXT PRIMARY KEY, settings TEXT);
        CREATE TABLE code_investigation_requests (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            subject_version INTEGER NOT NULL,
            issued_to_actor_id TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            required_capabilities TEXT NOT NULL,
            expected_head_generation INTEGER NOT NULL,
            expected_predecessor_receipt_id TEXT,
            canonicalization_profile TEXT NOT NULL,
            limits_profile TEXT NOT NULL,
            challenge_key_id TEXT NOT NULL,
            challenge_token_hash TEXT NOT NULL,
            single_use INTEGER NOT NULL,
            status TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT
        );
        """
    )
    connection.execute("INSERT INTO boards VALUES ('board-1', NULL)")
    connection.executemany(
        "INSERT INTO code_investigation_requests VALUES "
        "(?, 'board-1', 'card', 'card-1', 1, 'agent-1', 'source:opaque', "
        "'[\"path_read\"]', 0, NULL, 'canonical-v1', 'limits-v1', "
        "'composed-v1', 'secret-digest', 1, 'open', "
        "'2026-08-09T12:30:00Z', 'agent-1', ?, NULL)",
        [
            (f"request-{index:03}", f"2026-08-09T12:{index % 60:02}:00Z")
            for index in range(205)
        ],
    )
    return connection


def test_request_diagnostics_are_bounded_and_hide_challenge_material() -> None:
    connection = _request_database()

    rows = list_requests(connection, board_id="board-1", status="open", limit=5)

    assert len(rows) == 5
    assert all("challenge_token_hash" not in row for row in rows)
    assert rows[0]["challenge_key_id"] == "composed-v1"
    with pytest.raises(CodeTraceabilityDiagnosticsError) as error:
        list_requests(connection, board_id="board-1", limit=201)
    assert error.value.code == "code_traceability_diagnostics_limit_invalid"


def test_inspect_finds_record_outside_first_diagnostics_page() -> None:
    connection = _request_database()

    record = inspect_record(
        connection,
        board_id="board-1",
        kind="request",
        record_id="request-000",
    )

    assert record["id"] == "request-000"
    assert "challenge_token_hash" not in record


def test_policy_validation_upgrades_legacy_off_and_rejects_invalid_enum() -> None:
    connection = _request_database()

    legacy = validate_policy(connection, board_id="board-1")
    assert legacy["valid"] is True
    assert legacy["effective_mode"] == "advisory"
    assert legacy["legacy_default_applied"] is True
    assert legacy["policy"]["mode"] == "advisory"
    assert legacy["responsibility_boundary"] == "external_authenticated_agent"

    connection.execute(
        "UPDATE boards SET settings = ? WHERE id = 'board-1'",
        ('{"code_traceability":{"mode":"off","minimum_trust":"corroborated"}}',),
    )
    explicit_off = validate_policy(connection, board_id="board-1")
    assert explicit_off["effective_mode"] == "advisory"
    assert explicit_off["legacy_default_applied"] is True
    assert explicit_off["policy"]["minimum_trust"] == "corroborated"

    connection.execute(
        "UPDATE boards SET settings = ? WHERE id = 'board-1'",
        ('{"code_traceability":{"mode":"invalid"}}',),
    )
    invalid = validate_policy(connection, board_id="board-1")
    assert invalid["valid"] is False
    assert invalid["effective_mode"] == "invalid"
    assert invalid["errors"][0]["path"] == "code_traceability.mode"


def test_database_is_opened_read_only(tmp_path: Path) -> None:
    path = tmp_path / "pulse.db"
    writable = sqlite3.connect(path)
    writable.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
    writable.commit()
    writable.close()

    with open_read_only_database(str(path)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO marker DEFAULT VALUES")


def test_diagnose_applies_open_limit_per_actor_and_ignores_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.commands import code_traceability_diagnostics as module

    connection = _request_database()
    connection.execute("UPDATE code_investigation_requests SET status = 'consumed'")
    for table in module._TRACEABILITY_TABLES:  # noqa: SLF001 - schema fixture census
        if table == "code_investigation_requests":
            continue
        connection.execute(f'CREATE TABLE "{table}" (id TEXT, board_id TEXT)')
    template = (
        "INSERT INTO code_investigation_requests VALUES "
        "(?, 'board-1', 'card', 'card-1', 1, ?, 'source:opaque', '[]', "
        "0, NULL, 'canonical-v1', 'limits-v1', 'composed-v1', 'digest', 1, "
        "'open', ?, ?, ?, NULL)"
    )
    connection.executemany(
        template,
        [
            (
                f"active-{index}",
                "agent-a",
                "2099-01-01T00:00:00Z",
                "agent-a",
                "2098-01-01T00:00:00Z",
            )
            for index in range(9)
        ]
        + [
            (
                f"expired-{index}",
                "agent-b",
                "2000-01-01T00:00:00Z",
                "agent-b",
                "1999-01-01T00:00:00Z",
            )
            for index in range(20)
        ],
    )
    monkeypatch.setattr(
        module,
        "validate_schema",
        lambda _connection: {
            "valid": True,
            "missing_tables": [],
            "missing_triggers": [],
            "foreign_key_violations": [],
        },
    )

    report = diagnose(connection, board_id="board-1")

    assert report["open_request_count"] == 9
    assert report["maximum_actor_open_requests"] == 9
    assert report["checks"]["open_request_limit"] is False
    assert report["open_request_limit_offenders"] == [
        {"issued_to_actor_id": "agent-a", "open_request_count": 9}
    ]


def test_cli_exposes_only_persisted_state_diagnostics() -> None:
    command = (
        "import sys; "
        f"sys.path[:0] = [{str(REPO_SRC)!r}, {str(CORE_SRC)!r}]; "
        "from okto_pulse.community.cli import main; main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, "code-traceability", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert "requests" in result.stdout
    assert "receipts" in result.stdout
    assert "inspect" in result.stdout
    assert "diagnose" in result.stdout
    assert "probe" not in result.stdout.lower()
    assert "resolve" not in result.stdout.lower()
    assert "clone" not in result.stdout.lower()
