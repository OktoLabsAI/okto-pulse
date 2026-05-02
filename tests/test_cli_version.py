"""Unit tests for the global `okto-pulse --version` CLI flag."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).parent.parent / "src"


def test_global_version_flag_outputs_version_and_exits_cleanly():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, r'{}'); "
            "from okto_pulse.community.cli import main; main()".format(str(REPO_SRC)),
            "--version",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("okto-pulse ")
    assert "(okto-pulse-core " in result.stdout
    assert "unrecognized arguments" not in result.stderr
    assert "Version " not in result.stderr
