from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
LOCAL_IMPORT_PATHS = (
    REPO_ROOT / "src",
    WORKSPACE_ROOT / "okto_labs_pulse_core" / "src",
)

for path in reversed(LOCAL_IMPORT_PATHS):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)


@pytest.fixture(autouse=True)
def _reset_relational_schema_lifecycle_seam():
    """Isolate the process-global relational schema-lifecycle seam per test.

    R01C REPLAN-IMP4 activated the seam: the Community composition root
    (``create_community_app`` / the CLI boots) registers a
    ``RelationalSchemaLifecycleOrchestrator`` so ``init_db`` delegates the
    lifecycle to the edition. That registration is a process-global. Without
    isolation, ANY test that constructs the app leaks the registration into
    later tests, making their ``init_db`` baseline delegate instead of running
    inline (e.g. R16-B golden replay, R01B/R05D parity). Reset to the fail-open
    default (None) before AND after every test so each test is hermetic; a test
    that needs the orchestrator registers it explicitly within its own body."""
    from okto_pulse.core.infra import schema_lifecycle as _seam

    _seam.reset_relational_schema_lifecycle_orchestrator()
    try:
        yield
    finally:
        _seam.reset_relational_schema_lifecycle_orchestrator()
