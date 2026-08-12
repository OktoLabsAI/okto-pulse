"""Conformance audit for the Community Local First relational runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

FORBIDDEN_SERVER_DATABASE_TOKENS: tuple[str, ...] = (
    "post" + "gres",
    "post" + "gresql",
    "async" + "pg",
)

# Community's executable database factory remains SQLite-only.  These adapter
# modules intentionally compile portable DDL/queries for the shared schema
# contract, including the opt-in server-dialect conformance proof. They do not
# make a server database selectable by the Community runtime.
GOVERNED_PORTABLE_RELATIONAL_FILES: frozenset[str] = frozenset(
    {
        "src/okto_pulse/community/adapters/relational_schema_steps.py",
        "src/okto_pulse/community/adapters/relational_schema_migrator.py",
        "src/okto_pulse/community/adapters/semantic_assessment_v2_capabilities.py",
        "src/okto_pulse/community/adapters/sqlalchemy_code_traceability.py",
        "src/okto_pulse/community/adapters/sqlalchemy_guideline_policy.py",
        "src/okto_pulse/community/adapters/sqlalchemy_models.py",
        "src/okto_pulse/community/adapters/sqlalchemy_policy_subject_versioning.py",
    }
)


@dataclass(frozen=True)
class SqliteOnlyFinding:
    file: str
    line: int
    token: str


def audit_sqlite_only_community(source_root: Path) -> dict[str, object]:
    root = source_root.resolve()
    production_root = root / "src" / "okto_pulse" / "community"
    targets = list(production_root.rglob("*.py")) if production_root.exists() else []
    targets.extend(path for path in (root / "pyproject.toml", root / "uv.lock") if path.exists())
    findings: list[SqliteOnlyFinding] = []
    scanned: list[str] = []
    for path in sorted(targets):
        rel = path.relative_to(root).as_posix()
        scanned.append(rel)
        if rel in GOVERNED_PORTABLE_RELATIONAL_FILES:
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            lowered = line.casefold()
            for token in FORBIDDEN_SERVER_DATABASE_TOKENS:
                if token in lowered:
                    findings.append(SqliteOnlyFinding(rel, line_number, token))
    return {
        "ok": not findings,
        "scanned_files": scanned,
        "portable_relational_files": sorted(GOVERNED_PORTABLE_RELATIONAL_FILES),
        "findings": [asdict(finding) for finding in findings],
        "finding_count": len(findings),
    }


__all__ = [
    "FORBIDDEN_SERVER_DATABASE_TOKENS",
    "GOVERNED_PORTABLE_RELATIONAL_FILES",
    "SqliteOnlyFinding",
    "audit_sqlite_only_community",
]
