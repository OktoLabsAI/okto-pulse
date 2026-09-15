"""Regression tests for the ``okto-pulse kg subtype`` CLI group."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from repo_layout import resolve_core_repo

REPO_SRC = Path(__file__).parent.parent / "src"
CORE_SRC = resolve_core_repo(REPO_SRC.parent) / "src"


def test_kg_subtype_without_declare_prints_help():
    """A missing subtype child command should show help instead of a traceback."""
    command = (
        f"import sys; sys.path[:0] = [r'{REPO_SRC}', r'{CORE_SRC}']; "
        "from okto_pulse.community.cli import main; main()"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            command,
            "kg",
            "subtype",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    assert "usage:" in result.stdout
    assert "declare" in result.stdout
    assert "Traceback" not in result.stderr
