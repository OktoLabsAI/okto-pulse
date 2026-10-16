from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from okto_pulse.core.application.boundary.distribution_dependency_ownership import (
    COMMUNITY_DISTRIBUTION,
    audit_distribution_dependencies,
)
from repo_layout import resolve_core_repo


COMMUNITY_REPO = Path(__file__).resolve().parents[1]
CORE_REPO = resolve_core_repo(COMMUNITY_REPO)

def _current_wheel(repo: Path, distribution: str, environment: str) -> Path:
    """Resolve the exact current checkout version, never an old release wheel."""
    version = tomllib.loads((repo / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    filename = f'{distribution}-{version}-py3-none-any.whl'
    configured = os.environ.get(environment)
    wheel = Path(configured) if configured else repo / 'dist' / filename
    assert wheel.name == filename, f'wheel must match current checkout: {filename}'
    assert wheel.is_file(), f'build the current paired wheel first: {wheel}'
    return wheel.resolve()


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
    core_wheel = _current_wheel(CORE_REPO, 'okto_pulse_core', 'OKTO_F14_CORE_WHEEL')
    community_wheel = _current_wheel(COMMUNITY_REPO, 'okto_pulse', 'OKTO_F14_COMMUNITY_WHEEL')
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
            str(core_wheel.parent),
            str(core_wheel),
            str(community_wheel),
        ],
        check=True,
        cwd=COMMUNITY_REPO,
    )
    script = r"""
from pathlib import Path
import sys
import sysconfig
from importlib.metadata import requires

# Prove the installed pair before importing any product behavior. A fresh
# process starts only after installation; no old in-memory runtime is reused.
namespace = Path(sysconfig.get_paths()['purelib']) / 'okto_pulse'
for edition, checkout in zip(('core', 'community'), sys.argv[1:]):
    source = Path(checkout) / 'src' / 'okto_pulse' / edition
    installed = namespace / edition
    expected = {p.relative_to(source): p.read_bytes() for p in source.rglob('*.py')}
    actual = {p.relative_to(installed): p.read_bytes() for p in installed.rglob('*.py')}
    assert expected and expected == actual, f'installed {edition} differs from checkout'

# Check storage isolation before application composition can access storage.
from okto_pulse.community.config import CommunitySettings
import os
settings = CommunitySettings()
root = Path(os.environ["DATA_DIR"]).resolve()
assert Path(settings.data_dir).resolve() == root
assert settings.database_url == f"sqlite+aiosqlite:///{root / 'data' / 'pulse.db'}"
for value in (settings.kg_base_dir, settings.upload_dir, settings.metrics_dir):
    assert Path(value).resolve().is_relative_to(root)
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
        [str(python), "-I", "-c", script, str(CORE_REPO), str(COMMUNITY_REPO)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": "",
            "DATA_DIR": str(tmp_path / "data"),
            "DATABASE_URL": "",
            "KG_BASE_DIR": "",
            "UPLOAD_DIR": "",
            "METRICS_DIR": "",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "community_app=" in result.stdout
