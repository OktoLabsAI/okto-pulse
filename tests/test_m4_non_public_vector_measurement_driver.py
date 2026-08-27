"""The M-PULSE-4 evidence driver resolves only explicit source trees."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import okto_grafx
from okto_pulse.core.kg import schema_contract


def test_driver_dependency_check_is_independent_of_cwd_and_pythonpath(
    tmp_path: Path,
) -> None:
    pulse_repo = Path(__file__).resolve().parents[1]
    grafx_repo = Path(okto_grafx.__file__).resolve().parents[2]
    core_repo = Path(schema_contract.__file__).resolve().parents[4]
    script = pulse_repo / "scripts" / "measure_m4_non_public_vector_indexes.py"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--grafx-repo",
            str(grafx_repo),
            "--core-repo",
            str(core_repo),
            "--check-only",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    resolved = {
        name: Path(path).resolve()
        for name, path in json.loads(completed.stdout).items()
    }
    assert (grafx_repo / "src").resolve() in resolved["okto_grafx"].parents
    assert grafx_repo.resolve() in resolved["recall_corpus"].parents
    assert (core_repo / "src").resolve() in resolved["pulse_core"].parents
    assert (pulse_repo / "src").resolve() in resolved["pulse_community"].parents
