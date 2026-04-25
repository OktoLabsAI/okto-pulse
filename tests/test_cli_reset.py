"""Unit tests for the `okto-pulse reset` CLI command.

Tests argparse wiring via subprocess (matching the pattern in
test_cli_kg_backfill.py).  Full cmd_reset flow requires user input
and file system access so we only test the subparser shape here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = Path(__file__).parent.parent.parent / "okto-pulse-core" / "src"

for p in (str(REPO_SRC), str(CORE_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

for mod in list(sys.modules):
    if mod.startswith("okto_pulse.community"):
        del sys.modules[mod]


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


def test_reset_subparser_has_yes_flag():
    """The reset subparser exposes -y/--yes."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'{}'); "
         "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
         "reset", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "-y" in result.stdout
    assert "--yes" in result.stdout
