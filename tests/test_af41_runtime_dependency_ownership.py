from __future__ import annotations

from pathlib import Path
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
