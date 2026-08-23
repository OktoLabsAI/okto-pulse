"""Conformance audit for the Community Local First relational runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

FORBIDDEN_SERVER_DATABASE_TOKENS: tuple[str, ...] = (
    "post" + "gres",
    "post" + "gresql",
    "async" + "pg",
    "psyco" + "pg",
    "pg" + "8000",
)

# Dialect-aware persistence seams may name a server dialect while satisfying
# the shared Core/SaaS contract.  They must never acquire the capability to
# select a server URL, construct an engine, or import a server driver.
FORBIDDEN_SERVER_RUNTIME_ACTIVATION_TOKENS: tuple[str, ...] = (
    ("post" + "gresql") + "://",
    ("post" + "gresql") + "+",
    "async" + "pg",
    "psyco" + "pg",
    "pg" + "8000",
    "create_" + "async_engine",
    "create_" + "engine",
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

# Unlike the DDL/model modules above, these are executable persistence seams.
# Their dialect vocabulary is therefore audited with a narrower but stronger
# capability boundary instead of being excluded wholesale from inspection.
GOVERNED_DIALECT_AWARE_RUNTIME_FILES: frozenset[str] = frozenset(
    {
        "src/okto_pulse/community/adapters/relational_effects.py",
        "src/okto_pulse/community/adapters/sqlalchemy_spec_dependency.py",
        "src/okto_pulse/community/adapters/sqlalchemy_unit_of_work.py",
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
        tokens = (
            FORBIDDEN_SERVER_RUNTIME_ACTIVATION_TOKENS
            if rel in GOVERNED_DIALECT_AWARE_RUNTIME_FILES
            else FORBIDDEN_SERVER_DATABASE_TOKENS
        )
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            lowered = line.casefold()
            for token in tokens:
                if token in lowered:
                    findings.append(SqliteOnlyFinding(rel, line_number, token))
    return {
        "ok": not findings,
        "scanned_files": scanned,
        "portable_relational_files": sorted(GOVERNED_PORTABLE_RELATIONAL_FILES),
        "dialect_aware_runtime_files": sorted(
            GOVERNED_DIALECT_AWARE_RUNTIME_FILES
        ),
        "findings": [asdict(finding) for finding in findings],
        "finding_count": len(findings),
    }


__all__ = [
    "FORBIDDEN_SERVER_DATABASE_TOKENS",
    "FORBIDDEN_SERVER_RUNTIME_ACTIVATION_TOKENS",
    "GOVERNED_DIALECT_AWARE_RUNTIME_FILES",
    "GOVERNED_PORTABLE_RELATIONAL_FILES",
    "SqliteOnlyFinding",
    "audit_sqlite_only_community",
]
