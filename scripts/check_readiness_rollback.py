"""Assess a stopped v403 SQLite database against its pre-upgrade backup.

Exit 2 refuses an unqualified predecessor restore. This command never restores
or changes either file. A positive result is valid only while writers stay
stopped; follow docs/READINESS_PERSISTED_UPGRADE_PLAN.md before any restore.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / "okto-pulse" / "src"),
                str(WORKSPACE / "okto-pulse-core" / "src")]

from okto_pulse.community.adapters.readiness_rollback_assessment import (  # noqa: E402
    assess_readiness_rollback,
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upgraded", type=Path)
    parser.add_argument("backup", type=Path)
    result = assess_readiness_rollback(**vars(parser.parse_args()))
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["restore_allowed_without_preservation_plan"] else 2)
