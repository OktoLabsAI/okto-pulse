"""F15 gates for the Community Local First relational boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.sqlalchemy_database import build_community_engine
from okto_pulse.community.adapters.sqlite_only_boundary import (
    FORBIDDEN_SERVER_DATABASE_TOKENS,
    GOVERNED_PORTABLE_RELATIONAL_FILES,
    audit_sqlite_only_community,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_f15_community_production_and_metadata_are_sqlite_only() -> None:
    report = audit_sqlite_only_community(REPOSITORY_ROOT)

    assert report["ok"] is True, report["findings"]
    assert report["finding_count"] == 0
    assert report["scanned_files"]
    assert set(report["portable_relational_files"]) == (
        GOVERNED_PORTABLE_RELATIONAL_FILES
    )
    assert GOVERNED_PORTABLE_RELATIONAL_FILES <= set(report["scanned_files"])


def test_f15_audit_blocks_server_database_markers_in_source_and_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "okto_pulse" / "community" / "adapters" / "database.py"
    source.parent.mkdir(parents=True)
    source.write_text('ENGINE = "' + ("post" + "gresql") + '"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        'dependencies = ["' + ("async" + "pg") + '"]\n',
        encoding="utf-8",
    )

    report = audit_sqlite_only_community(tmp_path)

    assert report["ok"] is False
    assert report["finding_count"] >= 2
    assert {finding["file"] for finding in report["findings"]} == {
        "pyproject.toml",
        "src/okto_pulse/community/adapters/database.py",
    }


def test_f15_community_engine_rejects_a_server_database_url() -> None:
    url = ("post" + "gresql") + "+" + ("async" + "pg") + "://local/test"

    with pytest.raises(ValueError, match="community_database_requires_sqlite"):
        build_community_engine(url)


def test_f15_runtime_suites_stay_local_first_while_schema_is_portable() -> None:
    suite_paths = (
        REPOSITORY_ROOT / "tests" / "test_r01b_engine_session_parity.py",
        REPOSITORY_ROOT / "tests" / "test_r01c_imp4_schema_lifecycle_orchestrator.py",
        REPOSITORY_ROOT / "tests" / "test_r16c_data_bootstrapper.py",
    )
    findings = {
        path.name: token
        for path in suite_paths
        for token in FORBIDDEN_SERVER_DATABASE_TOKENS
        if token in path.read_text(encoding="utf-8").casefold()
    }

    assert findings == {}
    portable_schema_suite = (
        REPOSITORY_ROOT / "tests" / "test_r16b_relational_schema_migrator.py"
    ).read_text(encoding="utf-8")
    assert "test_postgresql_policy_materialization_trigger_matches_json_column_type" in (
        portable_schema_suite
    )
    postgres_proof = (
        REPOSITORY_ROOT / "tests" / "test_skb3_postgresql_trigger_proof_v1.py"
    ).read_text(encoding="utf-8")
    assert "OKTO_PULSE_TEST_POSTGRES_DSN" in postgres_proof
    assert "@pytest.mark.skipif" in postgres_proof
