from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from okto_pulse.core.application.boundary.distribution_dependency_ownership import (
    COMMUNITY_DISTRIBUTION,
    audit_distribution_dependencies,
)
from repo_layout import resolve_core_repo


COMMUNITY_REPO = Path(__file__).resolve().parents[1]
CORE_REPO = resolve_core_repo(COMMUNITY_REPO)
CORE_WHEEL = CORE_REPO / "dist" / "okto_pulse_core-0.3.1-py3-none-any.whl"
COMMUNITY_WHEEL = COMMUNITY_REPO / "dist" / "okto_pulse-0.3.1-py3-none-any.whl"


def test_community_declares_every_runtime_dependency_directly() -> None:
    report = audit_distribution_dependencies(
        core_repo=CORE_REPO,
        community_repo=COMMUNITY_REPO,
    )

    assert report.ok, report.as_dict()
    direct = set(report.observed[COMMUNITY_DISTRIBUTION]["manifest"])
    assert {
        "aiosqlite",
        "anyio",
        "fastapi",
        "filelock",
        "httpx",
        "python-multipart",
        "sqlalchemy",
        "starlette",
    } <= direct


def test_build_script_bootstraps_build_frontend_before_wheels() -> None:
    script = (COMMUNITY_REPO / "build.sh").read_text(encoding="utf-8")
    attributes = (COMMUNITY_REPO / ".gitattributes").read_text(encoding="utf-8")
    step_two = script.index("# Step 2: Build okto-pulse-core")
    bootstrap_call = script.index("\nensure_python_build_tool\n", step_two)
    first_wheel_build = script.index("\npython -m build --wheel\n", step_two)

    assert "*.sh text eol=lf" in attributes.splitlines()
    assert "python -c 'import build'" in script[:step_two]
    assert 'python -m pip install --disable-pip-version-check "build>=1.2,<2"' in (
        script[:step_two]
    )
    assert bootstrap_call < first_wheel_build


@pytest.mark.skipif(
    os.environ.get("OKTO_RUN_F14_WHEEL_SMOKE") != "1",
    reason="Set OKTO_RUN_F14_WHEEL_SMOKE=1 for clean-wheel acceptance.",
)
def test_community_wheel_builds_the_local_app_from_declared_metadata(
    tmp_path: Path,
) -> None:
    venv = tmp_path / "community-venv"
    subprocess.run(
        ["uv", "venv", str(venv), "--python", sys.executable],
        check=True,
        cwd=COMMUNITY_REPO,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--find-links",
            str(CORE_REPO / "dist"),
            str(CORE_WHEEL),
            str(COMMUNITY_WHEEL),
        ],
        check=True,
        cwd=COMMUNITY_REPO,
    )
    script = r"""
from importlib.metadata import requires
from okto_pulse.community.main import app

metadata = requires("okto-pulse") or []
for dependency in ("fastapi", "sqlalchemy", "aiosqlite", "anyio", "httpx"):
    assert any(row.lower().startswith(dependency) for row in metadata), dependency
assert app.title
paths = app.openapi()["paths"]
assert "/api/v1/boards" in paths
print(f"community_app={app.title!r} openapi_paths={len(paths)}")
"""
    result = subprocess.run(
        [str(python), "-c", script],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": "",
            "OKTO_DATA_DIR": str(tmp_path / "data"),
        },
    )
    assert "community_app=" in result.stdout
