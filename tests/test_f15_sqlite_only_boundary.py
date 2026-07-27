"""F15 gates for the Community Local First relational boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_pulse.community.adapters.sqlalchemy_database import build_community_engine
from okto_pulse.community.adapters.sqlite_only_boundary import (
    FORBIDDEN_SERVER_DATABASE_TOKENS,
    audit_sqlite_only_community,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_f15_community_production_and_metadata_are_sqlite_only() -> None:
    report = audit_sqlite_only_community(REPOSITORY_ROOT)

    assert report["ok"] is True, report["findings"]
    assert report["finding_count"] == 0
    assert report["scanned_files"]


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


def test_f15_engine_and_schema_suites_have_only_local_first_expectations() -> None:
    suite_paths = (
        REPOSITORY_ROOT / "tests" / "test_r01b_engine_session_parity.py",
        REPOSITORY_ROOT / "tests" / "test_r16b_relational_schema_migrator.py",
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
