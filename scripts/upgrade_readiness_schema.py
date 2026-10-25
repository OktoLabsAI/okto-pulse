"""Apply the fenced v400/v401/v402→v403 SQLite upgrade while Pulse is stopped."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

WORKSPACE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(WORKSPACE / 'okto-pulse' / 'src'),
                str(WORKSPACE / 'okto-pulse-core' / 'src')]

from okto_pulse.community.adapters.readiness_schema_upgrade import upgrade_readiness_schema


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    backup = upgrade_readiness_schema(args.database)
    print(json.dumps({'upgraded': str(args.database.resolve()),
                      'retained_backup': str(backup)}, sort_keys=True))
