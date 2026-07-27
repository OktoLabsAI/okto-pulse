"""Repository-layout helpers shared by cross-repository acceptance tests."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_core_repo(community_repo: Path) -> Path:
    """Locate the paired Core checkout across local worktrees and CI layouts."""

    configured = os.environ.get("OKTO_PULSE_CORE_REPO")
    workspace_root = community_repo.parent.parent
    candidates = (
        Path(configured).expanduser() if configured else None,
        community_repo.parent / "okto-pulse-core",
        community_repo.parent / "okto_labs_pulse_core",
        workspace_root / "okto-pulse-core",
        workspace_root / "okto_labs_pulse_core",
    )
    checked: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = candidate.resolve()
        if str(resolved) in checked:
            continue
        checked.append(str(resolved))
        if (resolved / "pyproject.toml").is_file() and (
            resolved / "src" / "okto_pulse" / "core"
        ).is_dir():
            return resolved
    raise RuntimeError(
        "Unable to locate the paired Core repository. "
        f"Checked: {checked}. Set OKTO_PULSE_CORE_REPO explicitly."
    )
