from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tomllib


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


def test_fastmcp2_authlib_contract_avoids_startup_deprecation_warning() -> None:
    pyproject = tomllib.loads(
        (COMMUNITY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    authlib_requirement = next(
        spec
        for spec in pyproject["project"]["dependencies"]
        if _dependency_name(spec) == "authlib"
    )

    assert ">=1.6.5" in authlib_requirement
    assert "<1.7.0" in authlib_requirement

    lock = tomllib.loads((COMMUNITY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    authlib_package = next(pkg for pkg in lock["package"] if pkg["name"] == "authlib")
    pulse_package = next(pkg for pkg in lock["package"] if pkg["name"] == "okto-pulse")
    direct = {dep["name"] for dep in pulse_package.get("dependencies", [])}

    assert authlib_package["version"].startswith("1.6.")
    assert "authlib" in direct


def test_fastmcp_jwt_import_emits_no_deprecation_warning() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "from fastmcp.server.auth.providers.jwt import JWTVerifier",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
