from __future__ import annotations

import sys
from pathlib import Path

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
