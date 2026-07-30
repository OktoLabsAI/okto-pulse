#!/usr/bin/env python3
"""Fail-closed release gate for the paired Core and Community wheels.

The gate deliberately runs outside either source tree after building the current
working trees.  It installs both wheels into a fresh virtual environment, proves
that imports and distribution metadata resolve from that environment, exercises
the packaged CLI and About surface, and lists the installed MCP catalog through
a real loopback HTTP listener.

Usage::

    python scripts/release_artifact_gate.py
    python scripts/release_artifact_gate.py --offline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

EXPECTED_VERSION = "0.3.1"
EXPECTED_MCP_TOOL_COUNT = 312
EXPECTED_CANONICAL_TOOL_COUNT = 304
EXPECTED_TOOL_ALIAS_COUNT = 8
EXPECTED_RESOURCE_COUNT = 52
MINIMUM_SUPPORTED_PYTHON = (3, 11)
COMMUNITY_REPO = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT_ENV = "OKTO_PULSE_WORKSPACE_ROOT"
RUNTIME_MATRIX_PROBE = (
    COMMUNITY_REPO / "scripts" / "release_runtime_matrix_probe.py"
)


class ReleaseArtifactGateError(RuntimeError):
    """One release-artifact invariant failed."""


def _is_core_checkout(candidate: Path) -> bool:
    return (
        (candidate / "pyproject.toml").is_file()
        and (candidate / "src" / "okto_pulse" / "core").is_dir()
    )


def _resolve_core_repo() -> Path:
    configured = str(os.environ.get("OKTO_PULSE_CORE_REPO", "")).strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if _is_core_checkout(candidate):
            return candidate
        raise ReleaseArtifactGateError(
            "OKTO_PULSE_CORE_REPO does not point to a valid Core checkout: "
            f"{candidate}"
        )

    configured_workspace = str(os.environ.get(WORKSPACE_ROOT_ENV, "")).strip()
    if configured_workspace:
        workspace = Path(configured_workspace).expanduser().resolve()
        checked: list[str] = []
        for name in ("okto-pulse-core", "okto_labs_pulse_core"):
            candidate = (workspace / name).resolve()
            checked.append(str(candidate))
            if _is_core_checkout(candidate):
                return candidate
        raise ReleaseArtifactGateError(
            f"{WORKSPACE_ROOT_ENV} contains no valid Core checkout; "
            f"checked: {checked}"
        )

    roots = (COMMUNITY_REPO.parent, COMMUNITY_REPO.parent.parent)
    candidates = (
        *(root / "okto-pulse-core" for root in roots),
        *(root / "okto_labs_pulse_core" for root in roots),
    )
    checked: list[str] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if str(resolved) in checked:
            continue
        checked.append(str(resolved))
        if _is_core_checkout(resolved):
            return resolved
    raise ReleaseArtifactGateError(
        "Unable to locate the paired Core repository. "
        f"Checked: {checked}. Set OKTO_PULSE_CORE_REPO explicitly."
    )


CORE_REPO = _resolve_core_repo()


def _forbidden_checkout_roots() -> tuple[Path, ...]:
    """Enumerate current and legacy worktrees that must not leak into installs."""

    workspace_roots = {
        COMMUNITY_REPO.parent.resolve(),
        COMMUNITY_REPO.parent.parent.resolve(),
        CORE_REPO.parent.resolve(),
        CORE_REPO.parent.parent.resolve(),
    }
    configured_workspace = str(os.environ.get(WORKSPACE_ROOT_ENV, "")).strip()
    if configured_workspace:
        workspace_roots.add(Path(configured_workspace).expanduser().resolve())
    candidates = {CORE_REPO.resolve(), COMMUNITY_REPO.resolve()}
    for workspace in workspace_roots:
        for name in (
            "okto-pulse-core",
            "okto-pulse",
            "okto_labs_pulse_core",
            "okto_labs_pulse_community",
        ):
            candidates.add((workspace / name).resolve())
    return tuple(sorted(candidates, key=str))


FORBIDDEN_CHECKOUT_ROOTS = _forbidden_checkout_roots()


def _assert_canonical_core_resolution() -> None:
    """Keep the bootstrap resolver aligned with Core's canonical resolver."""

    core_source = CORE_REPO / "src"
    sys.path.insert(0, str(core_source))
    try:
        from okto_pulse.core.application.boundary.repository_checkout import (
            RepositoryCheckoutNotFound,
            resolve_repository_checkout,
        )

        try:
            canonical = resolve_repository_checkout(
                "core",
                anchor_repo=COMMUNITY_REPO,
                environ=os.environ,
                required=True,
            )
        except RepositoryCheckoutNotFound as exc:
            raise ReleaseArtifactGateError(
                f"canonical Core checkout resolution failed: {exc}"
            ) from exc
        if canonical is None or canonical.repo_root != CORE_REPO:
            raise ReleaseArtifactGateError(
                "bootstrap and canonical Core checkout resolution diverged: "
                f"bootstrap={CORE_REPO}; canonical="
                f"{None if canonical is None else canonical.repo_root}"
            )
    finally:
        if sys.path and sys.path[0] == str(core_source):
            sys.path.pop(0)


def _command_text(command: Sequence[object]) -> str:
    return " ".join(str(part) for part in command)


def _run(
    command: Sequence[object],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        tuple(str(part) for part in command),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseArtifactGateError(
            f"command failed ({completed.returncode}): {_command_text(command)}\n"
            f"stdout tail: {completed.stdout[-2000:]}\n"
            f"stderr tail: {completed.stderr[-2000:]}"
        )
    return completed


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_script(venv: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return venv / ("Scripts" if os.name == "nt" else "bin") / f"{name}{suffix}"


def _isolated_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def _wheel_metadata_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = sorted(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        if len(metadata_names) != 1:
            raise ReleaseArtifactGateError(
                f"expected one METADATA member in {wheel.name}; got {metadata_names!r}"
            )
        metadata = archive.read(metadata_names[0]).decode("utf-8")
    for line in metadata.splitlines():
        if line.startswith("Version: "):
            return line.removeprefix("Version: ").strip()
    raise ReleaseArtifactGateError(f"Version missing from {wheel.name} METADATA")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_provenance(repo: Path) -> dict[str, Any]:
    commit = _run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        timeout=30,
    ).stdout.strip()
    status = _run(
        (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ),
        cwd=repo,
        timeout=60,
    ).stdout.splitlines()
    return {
        "repository_root": str(repo.resolve()),
        "commit": commit,
        "dirty": bool(status),
        "dirty_entry_count": len(status),
    }


def _payload_tree_provenance(
    *,
    distribution: str,
    wheel: Path,
    repo: Path,
    package_prefix: str,
    site_packages: Path,
) -> dict[str, Any]:
    """Assert every packaged payload byte survives source→wheel→install."""

    source_root = repo / "src"
    source_digest = hashlib.sha256()
    wheel_digest = hashlib.sha256()
    installed_digest = hashlib.sha256()
    critical_hashes: dict[str, str] = {}
    mismatches: list[dict[str, str]] = []
    compared = 0

    with zipfile.ZipFile(wheel) as archive:
        members = sorted(
            name
            for name in archive.namelist()
            if name.startswith(package_prefix) and not name.endswith("/")
        )
        if not members:
            raise ReleaseArtifactGateError(
                f"{distribution} wheel has no payload under {package_prefix}"
            )
        for member in members:
            source = source_root / Path(member)
            installed = site_packages / Path(member)
            if not source.is_file() or not installed.is_file():
                mismatches.append(
                    {
                        "member": member,
                        "source": (
                            "present" if source.is_file() else "missing"
                        ),
                        "wheel": "present",
                        "installed": (
                            "present" if installed.is_file() else "missing"
                        ),
                    }
                )
                continue
            source_bytes = source.read_bytes()
            wheel_bytes = archive.read(member)
            installed_bytes = installed.read_bytes()
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            wheel_hash = hashlib.sha256(wheel_bytes).hexdigest()
            installed_hash = hashlib.sha256(installed_bytes).hexdigest()
            if not (
                source_hash == wheel_hash == installed_hash
                and source_bytes == wheel_bytes == installed_bytes
            ):
                mismatches.append(
                    {
                        "member": member,
                        "source": source_hash,
                        "wheel": wheel_hash,
                        "installed": installed_hash,
                    }
                )
                continue
            compared += 1
            path_bytes = member.encode("utf-8") + b"\0"
            for aggregate, digest in (
                (source_digest, source_hash),
                (wheel_digest, wheel_hash),
                (installed_digest, installed_hash),
            ):
                aggregate.update(path_bytes)
                aggregate.update(digest.encode("ascii"))
                aggregate.update(b"\n")
            if member.endswith(
                (
                    "ska_resource_manifest.json",
                    "ska_tool_manifest.json",
                )
            ):
                critical_hashes[member] = source_hash

    if mismatches:
        raise ReleaseArtifactGateError(
            f"{distribution} source/wheel/site-packages payload drift: "
            + json.dumps(mismatches[:20], sort_keys=True)
        )
    aggregates = {
        "source": source_digest.hexdigest(),
        "wheel": wheel_digest.hexdigest(),
        "site_packages": installed_digest.hexdigest(),
    }
    if len(set(aggregates.values())) != 1:
        raise ReleaseArtifactGateError(
            f"{distribution} aggregate provenance hashes diverged: {aggregates}"
        )
    return {
        **_git_provenance(repo),
        "distribution": distribution,
        "version": _wheel_metadata_version(wheel),
        "wheel": {
            "path": str(wheel.resolve()),
            "sha256": _sha256(wheel),
            "size": wheel.stat().st_size,
        },
        "source_root": str(source_root.resolve()),
        "site_packages_root": str(site_packages.resolve()),
        "package_prefix": package_prefix,
        "byte_identical_member_count": compared,
        "byte_identical": True,
        "aggregate_sha256": aggregates["source"],
        "chain_hashes": aggregates,
        "critical_resource_sha256": critical_hashes,
        "mismatches": [],
    }


def _installed_payload_provenance(
    *,
    core_wheel: Path,
    community_wheel: Path,
    origin_evidence: dict[str, Any],
) -> dict[str, Any]:
    origins = dict(origin_evidence.get("origins") or {})
    core_origin = Path(str(origins["okto_pulse.core"])).resolve()
    community_origin = Path(str(origins["okto_pulse.community"])).resolve()
    core_site_packages = core_origin.parents[2]
    community_site_packages = community_origin.parents[2]
    if core_site_packages != community_site_packages:
        raise ReleaseArtifactGateError(
            "Core and Community installed into different site-packages roots: "
            f"{core_site_packages} != {community_site_packages}"
        )
    return {
        "core": _payload_tree_provenance(
            distribution="okto-pulse-core",
            wheel=core_wheel,
            repo=CORE_REPO,
            package_prefix="okto_pulse/core/",
            site_packages=core_site_packages,
        ),
        "community": _payload_tree_provenance(
            distribution="okto-pulse",
            wheel=community_wheel,
            repo=COMMUNITY_REPO,
            package_prefix="okto_pulse/community/",
            site_packages=community_site_packages,
        ),
    }


def _build_wheels(
    uv: str,
    work_dir: Path,
    *,
    offline: bool,
) -> tuple[Path, Path]:
    wheel_dir = work_dir / "wheels"
    wheel_dir.mkdir(parents=True)
    for repo in (CORE_REPO, COMMUNITY_REPO):
        command: list[object] = [uv, "build", "--wheel"]
        if offline:
            command.append("--offline")
        command.extend(("--out-dir", wheel_dir, repo))
        _run(
            command,
            cwd=work_dir,
            timeout=240,
        )

    core = sorted(wheel_dir.glob("okto_pulse_core-*.whl"))
    community = sorted(wheel_dir.glob("okto_pulse-0*.whl"))
    if len(core) != 1 or len(community) != 1:
        raise ReleaseArtifactGateError(
            "fresh build did not produce exactly one Core and one Community wheel: "
            f"core={core!r}, community={community!r}"
        )
    for wheel in (core[0], community[0]):
        observed = _wheel_metadata_version(wheel)
        if observed != EXPECTED_VERSION:
            raise ReleaseArtifactGateError(
                f"{wheel.name} metadata version {observed!r} != {EXPECTED_VERSION!r}"
            )
    return core[0], community[0]


def _audit_core_artifact(
    core_wheel: Path,
    community_wheel: Path,
    work_dir: Path,
) -> dict[str, Any]:
    """Run the canonical Core AST/dependency/path auditors on fresh wheels."""

    core_source = CORE_REPO / "src"
    sys.path.insert(0, str(core_source))
    try:
        from okto_pulse.core.application.boundary.distribution_dependency_ownership import (  # noqa: E501
            CORE_DISTRIBUTION,
            audit_distribution_dependencies,
        )
        from okto_pulse.core.application.boundary.gates import (
            PackageManifestGate,
            PackageManifestGateInput,
        )

        dependency_report = audit_distribution_dependencies(
            core_repo=CORE_REPO,
            community_repo=COMMUNITY_REPO,
            core_wheel=core_wheel,
            community_wheel=community_wheel,
        )
        if not dependency_report.ok:
            raise ReleaseArtifactGateError(
                "Core distribution dependency/AST audit failed: "
                + json.dumps(dependency_report.as_dict(), sort_keys=True)
            )

        with zipfile.ZipFile(core_wheel) as archive:
            record_names = sorted(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/RECORD")
            )
            if len(record_names) != 1:
                raise ReleaseArtifactGateError(
                    f"expected one Core RECORD member; got {record_names!r}"
                )
            record_path = work_dir / "core-wheel.RECORD"
            record_path.write_bytes(archive.read(record_names[0]))

        manifest_report = PackageManifestGate().run(
            PackageManifestGateInput(
                source_root=CORE_REPO / "src",
                pyproject_path=CORE_REPO / "pyproject.toml",
                wheel_record_path=record_path,
                verify_required_resources_in_wheel=True,
            )
        )
        if manifest_report.status != "passed":
            raise ReleaseArtifactGateError(
                "Core package-manifest audit failed: "
                + json.dumps(manifest_report.evidence, sort_keys=True, default=str)
            )
        observed = dependency_report.observed[CORE_DISTRIBUTION]
        return {
            "dependency_ledger": dependency_report.ledger_version,
            "forbidden_wheel_paths": list(observed["wheel_forbidden_paths"]),
            "wheel_import_roots": list(observed["wheel_imports"]),
            "record_file_count": len(manifest_report.evidence["wheel_files"]),
            "required_resources": manifest_report.evidence["required_resources"],
            "missing_required_resources": manifest_report.evidence[
                "missing_required_resources"
            ],
        }
    finally:
        if sys.path and sys.path[0] == str(core_source):
            sys.path.pop(0)


_INSTALLED_ORIGIN_PROBE = r"""
import importlib.metadata as metadata
import importlib.util
import json
import sys
from pathlib import Path

venv = Path(sys.argv[1]).resolve()
forbidden_roots = tuple(Path(value).resolve() for value in sys.argv[2:])

import okto_pulse.core as core
import okto_pulse.community as community
from okto_pulse.core.mcp.ska_resource_manifest import (
    verify_checked_in_manifest as verify_ska_resource_manifest,
)
from okto_pulse.core.mcp.ska_tool_manifest import (
    verify_checked_in_manifest as verify_ska_tool_manifest,
)

def under(path, root):
    path = Path(path).resolve()
    return path == root or root in path.parents

def clean_origin(module_name):
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.origin, module_name
    origin = Path(spec.origin).resolve()
    assert under(origin, venv), (module_name, origin, venv)
    assert not any(
        under(origin, root) and not under(origin, venv)
        for root in forbidden_roots
    ), (module_name, origin)
    return str(origin)

origins = {
    name: clean_origin(name)
    for name in (
        "okto_pulse.core",
        "okto_pulse.core.mcp.server",
        "okto_pulse.community",
        "okto_pulse.community.adapters.mcp_host",
    )
}

core_dist = metadata.distribution("okto-pulse-core")
community_dist = metadata.distribution("okto-pulse")
assert core_dist.version == community_dist.version == "0.3.1"
for distribution in (core_dist, community_dist):
    root = Path(distribution.locate_file("")).resolve()
    assert under(root, venv), (distribution.metadata["Name"], root, venv)
    assert not any(
        under(root, forbidden) and not under(root, venv)
        for forbidden in forbidden_roots
    )

core_files = sorted(str(path).replace("\\", "/") for path in (core_dist.files or ()))
community_files = sorted(
    str(path).replace("\\", "/") for path in (community_dist.files or ())
)
assert any(path.startswith("okto_pulse/core/") for path in core_files)
assert not any(path.startswith("okto_pulse/community/") for path in core_files)
assert any(path.startswith("okto_pulse/community/") for path in community_files)
required_core_resources = (
    "okto_pulse/core/mcp/resources/ska_resource_manifest.json",
    "okto_pulse/core/mcp/resources/ska_tool_manifest.json",
)
missing_core_resources = sorted(set(required_core_resources) - set(core_files))
assert not missing_core_resources, missing_core_resources

tool_manifest_path = verify_ska_tool_manifest()
resource_manifest_path = verify_ska_resource_manifest()
assert under(tool_manifest_path, venv), tool_manifest_path
assert under(resource_manifest_path, venv), resource_manifest_path
tool_manifest = json.loads(tool_manifest_path.read_text(encoding="utf-8"))
resource_manifest = json.loads(resource_manifest_path.read_text(encoding="utf-8"))
assert tool_manifest["tool_count"] == 11
assert resource_manifest["resource_count"] == 18

bad_sys_path = []
for value in sys.path:
    if not value:
        continue
    resolved = Path(value).resolve()
    if any(
        under(resolved, root) and not under(resolved, venv)
        for root in forbidden_roots
    ):
        bad_sys_path.append(str(resolved))
assert not bad_sys_path, bad_sys_path

frontend = Path(community.__file__).resolve().parent / "frontend_dist"
sources = [path.read_text(encoding="utf-8") for path in frontend.rglob("*.js")]
label = "Community Edition \u2014 v"
about = [source for source in sources if label in source]
assert len(about) == 1, len(about)
assert about[0].count(label) == 1
assert "0.3.1" in about[0]
assert not any("Community Edition \u2014 v0.3.0" in source for source in sources)

print("INSTALLED_ORIGIN_PROBE=" + json.dumps({
    "versions": {"core": core_dist.version, "community": community_dist.version},
    "origins": origins,
    "distribution_file_counts": {
        "core": len(core_files), "community": len(community_files),
    },
    "required_core_resources": list(required_core_resources),
    "ska_contract_manifests": {
        "tool_count": tool_manifest["tool_count"],
        "tool_manifest_sha256": tool_manifest["manifest_sha256"],
        "resource_count": resource_manifest["resource_count"],
        "resource_manifest_sha256": resource_manifest["manifest_sha256"],
    },
    "about_version": "0.3.1",
}, sort_keys=True))
"""


_INSTALLED_RUNTIME_VERSION_PROBE = r"""
import importlib.metadata as metadata
import json
import platform
import sys

expected = tuple(int(part) for part in sys.argv[1].split("."))
observed = tuple(sys.version_info[:2])
if observed != expected:
    raise RuntimeError(
        f"installed runtime {observed!r} != required release floor {expected!r}"
    )

print("INSTALLED_RUNTIME_VERSION=" + json.dumps({
    "implementation": platform.python_implementation(),
    "python": platform.python_version(),
    "python_major_minor": list(observed),
    "required_major_minor": list(expected),
    "pydantic": metadata.version("pydantic"),
    "okto_pulse_core": metadata.version("okto-pulse-core"),
    "okto_pulse": metadata.version("okto-pulse"),
}, sort_keys=True))
"""


_MCP_SERVER = r"""
import sys
import uvicorn
from okto_pulse.community.adapters.mcp_host import CommunityMcpHostProvider
from okto_pulse.community.adapters.resources import (
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.composition import RuntimeValueRegistry, runtime_value_scope
from okto_pulse.core.mcp import server

# Establish the explicit active runtime-value registry for this isolated
# inventory server and inject it into the cold-start transaction (the runtime
# catalog and instruction providers resolve against this same active registry).
runtime_values = RuntimeValueRegistry()
with runtime_value_scope(runtime_values):
    transaction = register_and_freeze_community_resource_catalog(runtime_values)
    frozen_resources, projection_identity = transaction.require_frozen_projection()
    host = CommunityMcpHostProvider().materialize_catalog(
        server.mcp,
        resource_catalog=frozen_resources,
        projection_identity=projection_identity,
    )
    app = host.http_app(transport="streamable-http")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(sys.argv[1]),
        access_log=False,
        log_level="warning",
    )
"""


_MCP_CLIENT_PROBE = r"""
import asyncio
import importlib.metadata as metadata
import json
import sys
from fastmcp import Client
from okto_pulse.core.mcp.manifest import tool_inventory_sha256
from okto_pulse.core.mcp.ska_tool_manifest import build_ska_tool_manifest

async def main():
    async with Client(sys.argv[1], timeout=30) as client:
        tools = await client.list_tools()
        listed_resources = await client.list_resources()
        resources = await client.read_resource("okto-pulse://server-manifest")
        initialized = client.initialize_result
    assert len(resources) == 1
    manifest = json.loads(resources[0].text)
    names = sorted(tool.name for tool in tools)
    aliases = manifest["tool_inventory"]["aliases"]
    frozen_ska_tools = {
        entry["name"] for entry in build_ska_tool_manifest()["tools"]
    }
    assert initialized.serverInfo.version == "0.3.1"
    assert metadata.version("okto-pulse-core") == "0.3.1"
    assert metadata.version("okto-pulse") == "0.3.1"
    assert (
        len(names)
        == manifest["tool_inventory"]["count"]
        == __EXPECTED_MCP_TOOL_COUNT__
    )
    assert len(names) - len(aliases) == __EXPECTED_CANONICAL_TOOL_COUNT__
    assert len(aliases) == __EXPECTED_TOOL_ALIAS_COUNT__
    assert len(listed_resources) == __EXPECTED_RESOURCE_COUNT__
    assert manifest["tool_inventory"]["sha256"] == tool_inventory_sha256(
        {"tools": names, "aliases": aliases}
    )
    assert frozen_ska_tools <= set(names)
    assert "okto_pulse_ask" in names
    assert "okto_pulse_remove_spec_entity" in names
    assert aliases["okto_pulse_ask_question"] == "okto_pulse_ask"
    print("MCP_HTTP_PROBE=" + json.dumps({
        "transport": "streamable-http-loopback",
        "server_version": initialized.serverInfo.version,
        "tool_count": len(names),
        "canonical_tool_count": len(names) - len(aliases),
        "tool_alias_count": len(aliases),
        "resource_count": len(listed_resources),
        "ska_tool_count": len(frozen_ska_tools),
        "inventory_sha256": manifest["tool_inventory"]["sha256"],
    }, sort_keys=True))

asyncio.run(main())
"""
_MCP_CLIENT_PROBE = (
    _MCP_CLIENT_PROBE
    .replace("__EXPECTED_MCP_TOOL_COUNT__", str(EXPECTED_MCP_TOOL_COUNT))
    .replace(
        "__EXPECTED_CANONICAL_TOOL_COUNT__",
        str(EXPECTED_CANONICAL_TOOL_COUNT),
    )
    .replace("__EXPECTED_TOOL_ALIAS_COUNT__", str(EXPECTED_TOOL_ALIAS_COUNT))
    .replace("__EXPECTED_RESOURCE_COUNT__", str(EXPECTED_RESOURCE_COUNT))
)


def _parse_probe(stdout: str, prefix: str) -> dict[str, Any]:
    rows = [
        line.removeprefix(prefix)
        for line in stdout.splitlines()
        if line.startswith(prefix)
    ]
    if len(rows) != 1:
        raise ReleaseArtifactGateError(
            f"expected one {prefix!r} result, got {len(rows)}; "
            f"stdout={stdout[-2000:]!r}"
        )
    return json.loads(rows[0])


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_listener(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise ReleaseArtifactGateError(
                f"installed MCP server exited before readiness ({process.returncode}); "
                f"stdout={stdout[-1000:]!r} stderr={stderr[-2000:]!r}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise ReleaseArtifactGateError(
        f"installed MCP server did not listen on 127.0.0.1:{port} within 60s"
    )


def _installed_gate(
    uv: str,
    core_wheel: Path,
    community_wheel: Path,
    work_dir: Path,
    *,
    offline: bool,
) -> dict[str, Any]:
    venv = work_dir / "venv"
    minimum_python = ".".join(str(part) for part in MINIMUM_SUPPORTED_PYTHON)
    _run(
        (uv, "venv", "--python", minimum_python, venv),
        cwd=work_dir,
        timeout=180,
    )
    python = _venv_python(venv)
    install = [uv, "pip", "install"]
    if offline:
        install.append("--offline")
    install.extend(("--python", python, core_wheel, community_wheel))
    _run(install, cwd=work_dir, timeout=900)

    env = _isolated_env()
    runtime_version = _run(
        (
            python,
            "-c",
            _INSTALLED_RUNTIME_VERSION_PROBE,
            minimum_python,
        ),
        cwd=work_dir,
        env=env,
        timeout=120,
    )
    runtime_version_evidence = _parse_probe(
        runtime_version.stdout,
        "INSTALLED_RUNTIME_VERSION=",
    )
    origin = _run(
        (
            python,
            "-c",
            _INSTALLED_ORIGIN_PROBE,
            venv,
            *FORBIDDEN_CHECKOUT_ROOTS,
        ),
        cwd=work_dir,
        env=env,
        timeout=180,
    )
    origin_evidence = _parse_probe(origin.stdout, "INSTALLED_ORIGIN_PROBE=")
    payload_provenance = _installed_payload_provenance(
        core_wheel=core_wheel,
        community_wheel=community_wheel,
        origin_evidence=origin_evidence,
    )

    runtime_matrix = _run(
        (
            python,
            RUNTIME_MATRIX_PROBE,
            "--work-dir",
            work_dir / "installed-runtime-matrix",
        ),
        cwd=work_dir,
        env=env,
        timeout=900,
    )
    runtime_matrix_evidence = _parse_probe(
        runtime_matrix.stdout,
        "INSTALLED_RUNTIME_MATRIX=",
    )
    for module_name, raw_origin in dict(
        runtime_matrix_evidence.get("module_origins") or {}
    ).items():
        module_origin = Path(str(raw_origin)).resolve()
        if not (
            module_origin == venv.resolve()
            or venv.resolve() in module_origin.parents
        ):
            raise ReleaseArtifactGateError(
                "runtime matrix imported outside the isolated environment: "
                f"{module_name}={module_origin}"
            )
        if any(
            (
                module_origin == forbidden
                or forbidden in module_origin.parents
            )
            and not (
                module_origin == venv.resolve()
                or venv.resolve() in module_origin.parents
            )
            for forbidden in FORBIDDEN_CHECKOUT_ROOTS
        ):
            raise ReleaseArtifactGateError(
                "runtime matrix imported a source checkout: "
                f"{module_name}={module_origin}"
            )

    cli = _run(
        (_venv_script(venv, "okto-pulse"), "--version"),
        cwd=work_dir,
        env=env,
        timeout=120,
    )
    expected_cli = "okto-pulse 0.3.1 (okto-pulse-core 0.3.1)"
    if cli.stdout.strip() != expected_cli:
        raise ReleaseArtifactGateError(
            "installed CLI version mismatch: "
            f"{cli.stdout.strip()!r} != {expected_cli!r}"
        )

    port = _free_loopback_port()
    server = subprocess.Popen(
        (str(python), "-c", _MCP_SERVER, str(port)),
        cwd=work_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    server_stdout = ""
    server_stderr = ""
    try:
        _wait_for_listener(server, port)
        client = _run(
            (
                python,
                "-c",
                _MCP_CLIENT_PROBE,
                f"http://127.0.0.1:{port}/mcp",
            ),
            cwd=work_dir,
            env=env,
            timeout=180,
        )
        mcp_evidence = _parse_probe(client.stdout, "MCP_HTTP_PROBE=")
    except BaseException as exc:
        server.terminate()
        try:
            server_stdout, server_stderr = server.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
            server_stdout, server_stderr = server.communicate()
        raise ReleaseArtifactGateError(
            f"{exc}\ninstalled MCP server stdout={server_stdout[-1000:]!r} "
            f"stderr={server_stderr[-2000:]!r}"
        ) from exc
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server_stdout, server_stderr = server.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
                server_stdout, server_stderr = server.communicate()

    return {
        "isolated_venv": str(venv),
        "offline_install": offline,
        "runtime_version": runtime_version_evidence,
        "forbidden_checkout_roots": [
            str(path) for path in FORBIDDEN_CHECKOUT_ROOTS
        ],
        "origin_probe": origin_evidence,
        "payload_provenance": payload_provenance,
        "runtime_matrix": runtime_matrix_evidence,
        "cli_version": cli.stdout.strip(),
        "mcp_http": mcp_evidence,
        "server_stderr_tail": server_stderr[-500:],
    }


def run_gate(work_dir: Path, *, offline: bool) -> dict[str, Any]:
    uv = shutil.which("uv")
    if uv is None:
        raise ReleaseArtifactGateError(
            "uv is required to build and install release wheels"
        )
    if not (CORE_REPO / "pyproject.toml").is_file():
        raise ReleaseArtifactGateError(f"Core sibling repository missing: {CORE_REPO}")
    _assert_canonical_core_resolution()

    work_dir.mkdir(parents=True, exist_ok=False)
    core_wheel, community_wheel = _build_wheels(uv, work_dir, offline=offline)
    audit = _audit_core_artifact(core_wheel, community_wheel, work_dir)
    installed = _installed_gate(
        uv,
        core_wheel,
        community_wheel,
        work_dir,
        offline=offline,
    )
    evidence = {
        "status": "passed",
        "expected_version": EXPECTED_VERSION,
        "wheels": {
            "core": {
                "name": core_wheel.name,
                "sha256": _sha256(core_wheel),
                "size": core_wheel.stat().st_size,
            },
            "community": {
                "name": community_wheel.name,
                "sha256": _sha256(community_wheel),
                "size": community_wheel.stat().st_size,
            },
        },
        "core_artifact_audit": audit,
        "installed": installed,
    }
    (work_dir / "release-artifact-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return evidence


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        help=(
            "Fresh output directory. Defaults to an automatically-cleaned "
            "temp directory."
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require the dependency closure to already exist in the uv cache.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.work_dir is not None:
            evidence = run_gate(args.work_dir.resolve(), offline=args.offline)
        else:
            with tempfile.TemporaryDirectory(
                prefix="okto-pulse-release-artifact-"
            ) as tmp:
                evidence = run_gate(Path(tmp) / "gate", offline=args.offline)
        print("RELEASE_ARTIFACT_GATE=" + json.dumps(evidence, sort_keys=True))
        return 0
    except (ReleaseArtifactGateError, OSError, ValueError) as exc:
        print(
            "RELEASE_ARTIFACT_GATE_FAILED="
            + json.dumps({"status": "blocking", "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
