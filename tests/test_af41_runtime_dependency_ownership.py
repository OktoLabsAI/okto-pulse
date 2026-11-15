from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version


COMMUNITY_ROOT = Path(__file__).resolve().parents[1]


def _dependency_name(spec: str) -> str:
    token = spec.strip()
    for sep in ("[", ">", "<", "=", "!", "~", ";", " ", "(", ")", "@"):
        idx = token.find(sep)
        if idx != -1:
            token = token[:idx]
    return token.strip().lower().replace("_", "-")


def test_af41_community_declares_mcp_serving_runtime_dependencies() -> None:
    pyproject = tomllib.loads(
        (COMMUNITY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = {
        _dependency_name(spec)
        for spec in pyproject["project"]["dependencies"]
    }

    assert {"uvicorn", "wsproto"} <= declared


def test_af41_community_lock_tracks_mcp_serving_runtime_as_direct_deps() -> None:
    lock = tomllib.loads((COMMUNITY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(pkg for pkg in lock["package"] if pkg["name"] == "okto-pulse")

    direct = {dep["name"] for dep in package.get("dependencies", [])}
    metadata = {
        dep["name"]
        for dep in package.get("metadata", {}).get("requires-dist", [])
    }

    assert {"uvicorn", "wsproto"} <= direct
    assert {"uvicorn", "wsproto"} <= metadata


def test_fastmcp2_authlib_contract_excludes_vulnerable_versions() -> None:
    pyproject = tomllib.loads(
        (COMMUNITY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    authlib_requirement = next(
        spec
        for spec in pyproject["project"]["dependencies"]
        if _dependency_name(spec) == "authlib"
    )

    allowed = Requirement(authlib_requirement).specifier
    assert Version("1.9.0") not in allowed
    assert Version("1.9.1") in allowed

    lock = tomllib.loads((COMMUNITY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    authlib_package = next(pkg for pkg in lock["package"] if pkg["name"] == "authlib")
    pulse_package = next(pkg for pkg in lock["package"] if pkg["name"] == "okto-pulse")
    direct = {dep["name"] for dep in pulse_package.get("dependencies", [])}

    assert Version(authlib_package["version"]) >= Version("1.9.1")
    assert "authlib" in direct


def test_filelock_contract_excludes_vulnerable_versions() -> None:
    pyproject = tomllib.loads(
        (COMMUNITY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    filelock_requirement = next(
        spec
        for spec in pyproject["project"]["dependencies"]
        if _dependency_name(spec) == "filelock"
    )
    allowed = Requirement(filelock_requirement).specifier
    assert Version("4.2.0") not in allowed
    assert Version("4.2.1") in allowed

    lock = tomllib.loads((COMMUNITY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    filelock_package = next(pkg for pkg in lock["package"] if pkg["name"] == "filelock")
    assert Version(filelock_package["version"]) >= Version("4.2.1")


def test_fastmcp_jwt_import_works_with_patched_authlib() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from fastmcp.server.auth.providers.jwt import JWTVerifier",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
