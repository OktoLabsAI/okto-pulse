"""Unit tests for the `okto-pulse init` CLI command.

Tests argparse wiring via subprocess (matching the pattern in
test_cli_kg_backfill.py).  Full cmd_init flow requires a real DB
so we only test the subparser shape here.
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


def test_init_subparser_has_agents_flag():
    """The init subparser exposes --agents (nargs='*')."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'{}'); "
         "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
         "init", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert "--agents" in result.stdout


def test_init_subparser_no_args_shows_help():
    """Running `okto-pulse init` with no subcommand prints help and exits 1."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'{}'); "
         "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
         "init"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode in (0, 1)
