"""Release-version contract for the compiled Community About surface."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _release_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def test_frontend_manifest_and_python_distribution_share_release_version() -> None:
    expected = _release_version()
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))

    assert package["version"] == expected
    assert lock["version"] == expected
    assert lock["packages"][""]["version"] == expected


def test_about_version_is_injected_from_authoritative_pyproject() -> None:
    vite_config = (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")
    header = (
        FRONTEND / "src" / "components" / "layout" / "Header.tsx"
    ).read_text(encoding="utf-8")

    assert "../pyproject.toml" in vite_config
    assert "__APP_VERSION__: JSON.stringify(communityVersion)" in vite_config
    assert "Community Edition — v{__APP_VERSION__}" in header
    assert "Community Edition — v0." not in header


def test_packaged_frontend_contains_current_about_version_only() -> None:
    label = "Community Edition — v"
    expected_version = _release_version()
    stale = "Community Edition — v0.2.5"
    bundle_root = ROOT / "src" / "okto_pulse" / "community" / "frontend_dist"
    javascript = [path.read_text(encoding="utf-8") for path in bundle_root.rglob("*.js")]
    about_bundles = [source for source in javascript if label in source]

    # Vite may emit adjacent React text children (label + build constant), so
    # validate both values in the same compiled chunk instead of assuming the
    # minifier concatenates them into one string literal.
    assert len(about_bundles) == 1
    assert about_bundles[0].count(label) == 1
    assert expected_version in about_bundles[0]
    assert sum(source.count(stale) for source in javascript) == 0
