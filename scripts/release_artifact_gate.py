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
from pathlib import Path
from typing import Any, Sequence


EXPECTED_VERSION = "0.3.0"
EXPECTED_MCP_TOOL_COUNT = 276
COMMUNITY_REPO = Path(__file__).resolve().parents[1]
CORE_REPO = COMMUNITY_REPO.parent / "okto_labs_pulse_core"


class ReleaseArtifactGateError(RuntimeError):
    """One release-artifact invariant failed."""


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


def _build_wheels(uv: str, work_dir: Path) -> tuple[Path, Path]:
    wheel_dir = work_dir / "wheels"
    wheel_dir.mkdir(parents=True)
    for repo in (CORE_REPO, COMMUNITY_REPO):
        _run(
            (uv, "build", "--wheel", "--offline", "--out-dir", wheel_dir, repo),
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
        from okto_pulse.core.application.boundary.distribution_dependency_ownership import (
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

def under(path, root):
    path = Path(path).resolve()
    return path == root or root in path.parents

def clean_origin(module_name):
    spec = importlib.util.find_spec(module_name)
    assert spec is not None and spec.origin, module_name
    origin = Path(spec.origin).resolve()
    assert under(origin, venv), (module_name, origin, venv)
    assert not any(under(origin, root) for root in forbidden_roots), (module_name, origin)
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
assert core_dist.version == community_dist.version == "0.3.0"
for distribution in (core_dist, community_dist):
    root = Path(distribution.locate_file("")).resolve()
    assert under(root, venv), (distribution.metadata["Name"], root, venv)
    assert not any(under(root, forbidden) for forbidden in forbidden_roots)

core_files = sorted(str(path).replace("\\", "/") for path in (core_dist.files or ()))
community_files = sorted(
    str(path).replace("\\", "/") for path in (community_dist.files or ())
)
assert any(path.startswith("okto_pulse/core/") for path in core_files)
assert not any(path.startswith("okto_pulse/community/") for path in core_files)
assert any(path.startswith("okto_pulse/community/") for path in community_files)

bad_sys_path = []
for value in sys.path:
    if not value:
        continue
    resolved = Path(value).resolve()
    if any(under(resolved, root) for root in forbidden_roots):
        bad_sys_path.append(str(resolved))
assert not bad_sys_path, bad_sys_path

frontend = Path(community.__file__).resolve().parent / "frontend_dist"
sources = [path.read_text(encoding="utf-8") for path in frontend.rglob("*.js")]
label = "Community Edition \u2014 v"
about = [source for source in sources if label in source]
assert len(about) == 1, len(about)
assert about[0].count(label) == 1
assert "0.3.0" in about[0]
assert not any("Community Edition \u2014 v0.2.5" in source for source in sources)

print("INSTALLED_ORIGIN_PROBE=" + json.dumps({
    "versions": {"core": core_dist.version, "community": community_dist.version},
    "origins": origins,
    "distribution_file_counts": {
        "core": len(core_files), "community": len(community_files),
    },
    "about_version": "0.3.0",
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

async def main():
    async with Client(sys.argv[1], timeout=30) as client:
        tools = await client.list_tools()
        resources = await client.read_resource("okto-pulse://server-manifest")
        initialized = client.initialize_result
    assert len(resources) == 1
    manifest = json.loads(resources[0].text)
    names = sorted(tool.name for tool in tools)
    aliases = manifest["tool_inventory"]["aliases"]
    assert initialized.serverInfo.version == "0.3.0"
    assert metadata.version("okto-pulse-core") == "0.3.0"
    assert metadata.version("okto-pulse") == "0.3.0"
    assert len(names) == manifest["tool_inventory"]["count"] == 276
    assert manifest["tool_inventory"]["sha256"] == tool_inventory_sha256(
        {"tools": names, "aliases": aliases}
    )
    assert "okto_pulse_ask" in names
    assert "okto_pulse_remove_spec_entity" in names
    assert aliases["okto_pulse_ask_question"] == "okto_pulse_ask"
    print("MCP_HTTP_PROBE=" + json.dumps({
        "transport": "streamable-http-loopback",
        "server_version": initialized.serverInfo.version,
        "tool_count": len(names),
        "inventory_sha256": manifest["tool_inventory"]["sha256"],
    }, sort_keys=True))

asyncio.run(main())
"""


def _parse_probe(stdout: str, prefix: str) -> dict[str, Any]:
    rows = [
        line.removeprefix(prefix)
        for line in stdout.splitlines()
        if line.startswith(prefix)
    ]
    if len(rows) != 1:
        raise ReleaseArtifactGateError(
            f"expected one {prefix!r} result, got {len(rows)}; stdout={stdout[-2000:]!r}"
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
    _run((uv, "venv", "--python", sys.executable, venv), cwd=work_dir, timeout=180)
    python = _venv_python(venv)
    install = [uv, "pip", "install"]
    if offline:
        install.append("--offline")
    install.extend(("--python", python, core_wheel, community_wheel))
    _run(install, cwd=work_dir, timeout=900)

    env = _isolated_env()
    origin = _run(
        (
            python,
            "-c",
            _INSTALLED_ORIGIN_PROBE,
            venv,
            CORE_REPO,
            COMMUNITY_REPO,
        ),
        cwd=work_dir,
        env=env,
        timeout=180,
    )
    origin_evidence = _parse_probe(origin.stdout, "INSTALLED_ORIGIN_PROBE=")

    cli = _run(
        (_venv_script(venv, "okto-pulse"), "--version"),
        cwd=work_dir,
        env=env,
        timeout=120,
    )
    expected_cli = "okto-pulse 0.3.0 (okto-pulse-core 0.3.0)"
    if cli.stdout.strip() != expected_cli:
        raise ReleaseArtifactGateError(
            f"installed CLI version mismatch: {cli.stdout.strip()!r} != {expected_cli!r}"
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
        "origin_probe": origin_evidence,
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

    work_dir.mkdir(parents=True, exist_ok=False)
    core_wheel, community_wheel = _build_wheels(uv, work_dir)
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
        help="Fresh output directory. Defaults to an automatically-cleaned temp directory.",
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
