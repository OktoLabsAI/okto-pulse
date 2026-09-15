from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dev_extra_matches_the_documented_contributor_setup() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    dependencies = project["optional-dependencies"]["dev"]

    for package in ("build", "pytest", "pytest-asyncio", "uv"):
        assert any(item.startswith(package) for item in dependencies)


def test_contributor_links_and_cla_guidance_are_local_and_current() -> None:
    assert (ROOT / "CONTRIBUTING.md").is_file()
    assert (ROOT / "CLA.md").is_file()
    assert (ROOT / "SECURITY.md").is_file()
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".github/workflows/cla.yml" not in claude
    assert "external CLA integration" in claude


def test_readme_documents_every_supported_cli_command_path() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = (
        "okto-pulse init",
        "okto-pulse serve",
        "okto-pulse status",
        "okto-pulse code-traceability requests",
        "okto-pulse code-traceability receipts",
        "okto-pulse code-traceability inspect",
        "okto-pulse code-traceability diagnose",
        "okto-pulse metrics status",
        "okto-pulse metrics enable-beacon",
        "okto-pulse metrics disable",
        "okto-pulse metrics export",
        "okto-pulse metrics purge-local",
        "okto-pulse api-key",
        "okto-pulse reset",
        "okto-pulse verify-pipeline",
        "okto-pulse kg migrate-schema",
        "okto-pulse kg backfill",
        "okto-pulse kg dedup-entities",
        "okto-pulse kg proposals",
        "okto-pulse kg unmerge",
        "okto-pulse kg export",
        "okto-pulse kg subtype declare",
        "okto-pulse kg restore",
    )

    assert not [command for command in commands if f"`{command}" not in readme]


def test_frontend_readme_describes_the_community_build() -> None:
    readme = (ROOT / "frontend" / "README.md").read_text(encoding="utf-8")
    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )

    assert readme.startswith("# Okto Pulse Frontend")
    assert "does not require a Clerk publishable key" in readme
    assert package["name"] == "okto-pulse-frontend"
