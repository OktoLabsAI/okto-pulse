"""Installed-wheel, real-HTTP acceptance for recovery and Global Outbox DLQ.

This is intentionally artifact-first.  It builds the current Pulse worktrees plus
an explicit Grafx 0.0.1 candidate, installs only those wheels into isolated virtual
environments, starts the installed CLI's dual API/MCP server on loopback ports, and
drives the public Streamable HTTP MCP surface.  Controlled fixture injections are
declared in the sibling JSON manifest; there are no direct FastMCP ``.fn`` calls in
this harness.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
import pytest
from fastmcp import Client

from okto_pulse.core.application.boundary.repository_checkout import (
    resolve_repository_checkout,
)

COMMUNITY_REPO = Path(__file__).resolve().parents[1]
_CORE_CHECKOUT = resolve_repository_checkout(
    "core",
    anchor_repo=COMMUNITY_REPO,
)
assert _CORE_CHECKOUT is not None
CORE_REPO = _CORE_CHECKOUT.repo_root
WORKSPACE_ROOT = COMMUNITY_REPO.parent.resolve()
MANIFEST_PATH = Path(__file__).with_name(
    "global_discovery_recovery_installed_e2e_manifest.json"
)
OPT_OUT_ENV = "OKTO_SKIP_GLOBAL_DISCOVERY_INSTALLED_E2E"
GRAFX_WHEEL_ENV = "OKTO_E2E_GRAFX_WHEEL"
GRAFX_REPO_ENV = "OKTO_E2E_GRAFX_REPO"
FINAL_WHEEL_DIR_ENV = "OKTO_E2E_FINAL_WHEEL_DIR"
FINAL_CORE_WHEEL_SHA256_ENV = "OKTO_E2E_FINAL_CORE_WHEEL_SHA256"
FINAL_COMMUNITY_WHEEL_SHA256_ENV = "OKTO_E2E_FINAL_COMMUNITY_WHEEL_SHA256"
FINAL_GRAFX_WHEEL_SHA256_ENV = "OKTO_E2E_FINAL_GRAFX_WHEEL_SHA256"
EXPECTED_GRAFX_VERSION = "0.0.1"
BOARD_CENSUS_SIZE = 1_500
EXPECTED_TOOL_COUNT = 338
EXPECTED_CANONICAL_TOOL_COUNT = 330
EXPECTED_TOOL_INVENTORY_SHA256 = (
    "0e0afb57b9b0e5fd12522a98c3d3516373aa145dbb112ba2522673c1cc96c16d"
)
EXPECTED_TOOL_ALIASES = {
    "okto_pulse_ask_ideation_question": "okto_pulse_ask",
    "okto_pulse_ask_question": "okto_pulse_ask",
    "okto_pulse_ask_refinement_question": "okto_pulse_ask",
    "okto_pulse_ask_spec_question": "okto_pulse_ask",
    "okto_pulse_ask_sprint_question": "okto_pulse_ask",
    "okto_pulse_remove_api_contract": "okto_pulse_remove_spec_entity",
    "okto_pulse_remove_business_rule": "okto_pulse_remove_spec_entity",
    "okto_pulse_remove_decision": "okto_pulse_remove_spec_entity",
}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get(OPT_OUT_ENV) == "1",
        reason=f"explicitly opted out via {OPT_OUT_ENV}=1",
    ),
]


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {command!r}\n"
        f"stdout:\n{completed.stdout[-8000:]}\n"
        f"stderr:\n{completed.stderr[-8000:]}"
    )
    return completed


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_script(venv: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    directory = "Scripts" if os.name == "nt" else "bin"
    return venv / directory / f"{name}{suffix}"


def _single_wheel(directory: Path, distribution_prefix: str) -> Path:
    wheels = sorted(directory.glob(f"{distribution_prefix}-*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_sha256(environment_name: str, *, required: bool = False) -> str | None:
    raw = os.environ.get(environment_name, "").strip().lower()
    if not raw:
        assert not required, f"{environment_name} is required in FINAL-pair mode"
        return None
    assert re.fullmatch(r"[0-9a-f]{64}", raw), (
        f"{environment_name} must be exactly 64 hexadecimal characters"
    )
    return raw


def _resolve_pulse_wheel_pair(
    *,
    root: Path,
    uv: str,
    build_env: dict[str, str],
) -> tuple[Path, Path]:
    """Reuse the governed FINAL pair or build the two current Pulse worktrees."""

    final_wheel_dir_raw = os.environ.get(FINAL_WHEEL_DIR_ENV, "").strip()
    if final_wheel_dir_raw:
        final_wheel_dir = Path(final_wheel_dir_raw).expanduser().resolve()
        assert final_wheel_dir.is_dir(), final_wheel_dir
        all_wheels = sorted(final_wheel_dir.glob("*.whl"))
        core_candidates = [
            wheel for wheel in all_wheels if wheel.name.startswith("okto_pulse_core-")
        ]
        community_candidates = [
            wheel for wheel in all_wheels if wheel.name.startswith("okto_pulse-")
        ]
        assert len(all_wheels) == 2, all_wheels
        assert len(core_candidates) == 1, all_wheels
        assert len(community_candidates) == 1, all_wheels
        core_wheel = core_candidates[0]
        community_wheel = community_candidates[0]
        assert "-0.3.3-" in core_wheel.name, core_wheel.name
        assert "-0.3.3-" in community_wheel.name, community_wheel.name
        expected_core_sha = _expected_sha256(FINAL_CORE_WHEEL_SHA256_ENV)
        expected_community_sha = _expected_sha256(FINAL_COMMUNITY_WHEEL_SHA256_ENV)
        if expected_core_sha is not None:
            assert _sha256(core_wheel) == expected_core_sha
        if expected_community_sha is not None:
            assert _sha256(community_wheel) == expected_community_sha
        return core_wheel, community_wheel

    core_dist = root / "dist" / "core"
    community_dist = root / "dist" / "community"
    core_dist.mkdir(parents=True)
    community_dist.mkdir(parents=True)
    _run_checked(
        [uv, "build", "--wheel", "--out-dir", str(core_dist), str(CORE_REPO)],
        cwd=root,
        env=build_env,
    )
    _run_checked(
        [
            uv,
            "build",
            "--wheel",
            "--out-dir",
            str(community_dist),
            str(COMMUNITY_REPO),
        ],
        cwd=root,
        env=build_env,
    )
    return (
        _single_wheel(core_dist, "okto_pulse_core"),
        _single_wheel(community_dist, "okto_pulse"),
    )


def _assert_wheel_resource_ownership(
    core_wheel: Path,
    community_wheel: Path,
) -> None:
    """Prove concrete Community resource bodies/imports never enter Core."""

    with zipfile.ZipFile(core_wheel) as archive:
        core_names = tuple(archive.namelist())
        core_python = {
            name: archive.read(name).decode("utf-8")
            for name in core_names
            if name.endswith(".py")
        }
        core_payload = b"\n".join(
            archive.read(name) for name in core_names if not name.endswith("/")
        )
    assert not any(name.startswith("okto_pulse/community/") for name in core_names)
    import_pattern = re.compile(
        r"(?m)^\s*(?:from\s+okto_pulse\.community\b|import\s+okto_pulse\.community\b)"
    )
    assert not {
        name: line
        for name, source in core_python.items()
        for line in source.splitlines()
        if import_pattern.search(line)
    }

    with zipfile.ZipFile(community_wheel) as archive:
        operational = {
            name: archive.read(name)
            for name in archive.namelist()
            if name.startswith("okto_pulse/community/resources/operational/")
            and name.endswith(".md")
        }
    assert len(operational) == 4, sorted(operational)
    assert all(body and body not in core_payload for body in operational.values())


def _assert_grafx_candidate_wheel(wheel: Path) -> Path:
    """Authenticate the exact unpublished Grafx candidate used by installed tests."""

    resolved = wheel.expanduser().resolve()
    assert resolved.is_file(), resolved
    assert resolved.suffix == ".whl", resolved
    with zipfile.ZipFile(resolved) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1, metadata_names
        metadata = archive.read(metadata_names[0]).decode("utf-8")
    assert re.search(r"(?m)^Name: okto-grafx\s*$", metadata), metadata[:2000]
    assert re.search(
        rf"(?m)^Version: {re.escape(EXPECTED_GRAFX_VERSION)}\s*$", metadata
    ), metadata[:2000]
    assert re.search(r"(?m)^Provides-Extra: accel\s*$", metadata), metadata[:2000]
    assert re.search(
        r"(?mi)^Requires-Dist: numpy>=1\.24; extra == ['\"]accel['\"]\s*$",
        metadata,
    ), metadata[:4000]
    assert re.search(
        r"(?mi)^Requires-Dist: google-crc32c>=1\.5; extra == ['\"]accel['\"]\s*$",
        metadata,
    ), metadata[:4000]
    return resolved


def _resolve_grafx_candidate_wheel(
    *,
    root: Path,
    uv: str,
    build_env: dict[str, str],
) -> Path:
    """Use an explicit wheel or build one explicit local Grafx checkout.

    The installed acceptance never falls back to an index.  That keeps the
    pre-publication Pulse gate on the exact candidate that will become 0.0.1,
    while ``OKTO_E2E_FINAL_WHEEL_DIR`` remains the governed Core/Community pair.
    """

    wheel_raw = os.environ.get(GRAFX_WHEEL_ENV, "").strip()
    repo_raw = os.environ.get(GRAFX_REPO_ENV, "").strip()
    assert not (wheel_raw and repo_raw), (
        f"set exactly one of {GRAFX_WHEEL_ENV} or {GRAFX_REPO_ENV}, not both"
    )
    final_pair_mode = bool(os.environ.get(FINAL_WHEEL_DIR_ENV, "").strip())
    expected_final_sha = _expected_sha256(
        FINAL_GRAFX_WHEEL_SHA256_ENV,
        required=final_pair_mode,
    )
    if final_pair_mode:
        assert wheel_raw, (
            "FINAL-pair mode may not rebuild Grafx: pass the frozen candidate via "
            f"{GRAFX_WHEEL_ENV}"
        )
    if wheel_raw:
        source_wheel = _assert_grafx_candidate_wheel(Path(wheel_raw))
        source_sha256 = _sha256(source_wheel)
        if expected_final_sha is not None:
            assert source_sha256 == expected_final_sha
        grafx_dist = root / "dist" / "grafx"
        grafx_dist.mkdir(parents=True, exist_ok=True)
        staged_wheel = grafx_dist / source_wheel.name
        if source_wheel != staged_wheel.resolve():
            shutil.copy2(source_wheel, staged_wheel)
        staged_wheel = _assert_grafx_candidate_wheel(staged_wheel)
        assert _sha256(staged_wheel) == source_sha256
        return staged_wheel

    assert repo_raw, (
        "the unpublished okto-grafx 0.0.1 candidate must be explicit: set "
        f"{GRAFX_WHEEL_ENV} to its wheel or {GRAFX_REPO_ENV} to its source checkout"
    )
    source_repo = Path(repo_raw).expanduser().resolve()
    assert source_repo.is_dir(), source_repo
    assert (source_repo / "pyproject.toml").is_file(), source_repo
    grafx_dist = root / "dist" / "grafx"
    grafx_dist.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [uv, "build", "--wheel", "--out-dir", str(grafx_dist), str(source_repo)],
        cwd=root,
        env=build_env,
    )
    return _assert_grafx_candidate_wheel(_single_wheel(grafx_dist, "okto_grafx"))


@dataclass(frozen=True)
class InstalledRuntime:
    root: Path
    venv: Path
    python: Path
    console: Path
    data_dir: Path
    database_path: Path
    api_key: str
    actor_id: str
    peer_api_key: str
    peer_actor_id: str
    core_wheel: Path
    community_wheel: Path
    grafx_wheel: Path
    core_wheel_sha256: str
    community_wheel_sha256: str
    grafx_wheel_sha256: str
    clock_file: Path
    fail_once_marker: Path
    preparation_release_file: Path
    building_gate_signal_file: Path
    fence_loss_target_file: Path
    fence_loss_signal_file: Path
    resume_gate_target_file: Path
    resume_gate_signal_file: Path
    resume_gate_release_file: Path
    resume_gate_timeout_marker: Path
    z2_target_file: Path
    z2_signal_file: Path
    z2_tripwire_marker: Path
    adoption_poll_signal_file: Path
    submit_gate_target_file: Path
    submit_gate_signal_file: Path
    submit_gate_release_file: Path
    submit_gate_timeout_marker: Path
    env: dict[str, str]
    origin_report: dict[str, Any]
    resource_manifest: dict[str, Any]


@pytest.fixture(scope="module")
def installed_runtime(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[InstalledRuntime]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["transport"] == "real-streamable-http"
    assert manifest["direct_fastmcp_fn_calls"] == []

    uv = shutil.which("uv")
    assert uv is not None, "uv is required; set the explicit opt-out marker to skip"

    root = tmp_path_factory.mktemp("global-recovery-installed-e2e")
    build_env = {**os.environ, "UV_NO_PROGRESS": "1"}

    core_wheel, community_wheel = _resolve_pulse_wheel_pair(
        root=root,
        uv=uv,
        build_env=build_env,
    )
    grafx_wheel = _resolve_grafx_candidate_wheel(
        root=root,
        uv=uv,
        build_env=build_env,
    )
    core_wheel_sha256 = _sha256(core_wheel)
    community_wheel_sha256 = _sha256(community_wheel)
    grafx_wheel_sha256 = _sha256(grafx_wheel)
    _assert_wheel_resource_ownership(core_wheel, community_wheel)

    venv = root / "venv"
    _run_checked(
        [uv, "venv", str(venv), "--python", sys.executable, "--seed"],
        cwd=root,
        env=build_env,
    )
    python = _venv_python(venv)
    _run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(core_wheel),
            str(community_wheel),
            str(grafx_wheel),
        ],
        cwd=root,
        env=build_env,
        timeout=900,
    )
    assert _sha256(core_wheel) == core_wheel_sha256
    assert _sha256(community_wheel) == community_wheel_sha256
    assert _sha256(grafx_wheel) == grafx_wheel_sha256

    data_dir = root / "pulse-home"
    clock_file = root / "confirmation-clock-offset-seconds.txt"
    clock_file.write_text("0", encoding="ascii")
    fail_once_marker = root / "recovery-dispatch-failed-once.txt"
    preparation_release_file = root / "release-first-preparation.txt"
    building_gate_signal_file = root / "building-gate-signal.txt"
    fence_loss_target_file = root / "fence-loss-target.txt"
    fence_loss_signal_file = root / "fence-loss-signal.txt"
    resume_gate_target_file = root / "resume-gate-target.txt"
    resume_gate_signal_file = root / "resume-gate-signal.txt"
    resume_gate_release_file = root / "resume-gate-release.txt"
    resume_gate_timeout_marker = root / "resume-gate-timeout-marker.txt"
    z2_target_file = root / "z2-tamper-target.txt"
    z2_signal_file = root / "z2-tamper-signal.txt"
    z2_tripwire_marker = root / "z2-writer-lease-tripwire.txt"
    adoption_poll_signal_file = root / "adoption-poll-started.txt"
    submit_gate_target_file = root / "submit-gate-target.txt"
    submit_gate_signal_file = root / "submit-gate-signal.txt"
    submit_gate_release_file = root / "submit-gate-release.txt"
    submit_gate_timeout_marker = root / "submit-gate-timeout-marker.txt"
    runtime_env = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "KG_GLOBAL_GRAPH_BACKEND": "ladybug",
        "KG_GRAPH_BACKEND": "ladybug",
        "NO_PROXY": "127.0.0.1,localhost",
        "OKTO_PULSE_HOME": str(data_dir),
        "OKTO_PULSE_METRICS_BEACON_STARTUP_DELAY_SECONDS": "600",
        "OKTO_PULSE_NO_BANNER": "1",
        "OKTO_PULSE_SHUTDOWN_TIMEOUT_SECONDS": "45",
        "OKTO_PULSE_SKIP_DEMO_SEED": "1",
        "OKTO_PULSE_STARTUP_TIMEOUT_SECONDS": "180",
        "OKTO_PULSE_TERMS_ACCEPTED": "1",
        "PYTHONUNBUFFERED": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "TRANSFORMERS_OFFLINE": "1",
    }
    runtime_env.pop("PYTHONPATH", None)
    runtime_env.pop("PYTHONHOME", None)

    origin_script = r"""
import json
import os
import sys
from importlib.metadata import distribution, version
from pathlib import Path

import okto_pulse.community
import okto_pulse.core
import okto_grafx
import google_crc32c
import numpy

workspace = Path(os.environ["E2E_WORKSPACE_ROOT"]).resolve()
venv = Path(sys.prefix).resolve()
origins = {
    "community": str(Path(okto_pulse.community.__file__).resolve()),
    "core": str(Path(okto_pulse.core.__file__).resolve()),
    "grafx": str(Path(okto_grafx.__file__).resolve()),
}
for origin in origins.values():
    path = Path(origin)
    assert venv in path.parents, (venv, path)
    assert workspace not in path.parents, (workspace, path)
for raw in sys.path:
    if not raw:
        continue
    resolved = Path(raw).resolve()
    assert workspace != resolved and workspace not in resolved.parents, resolved

core = distribution("okto-pulse-core")
community = distribution("okto-pulse")
grafx = distribution("okto-grafx")
grafx_direct_url = json.loads(grafx.read_text("direct_url.json") or "{}")
assert grafx_direct_url["url"] == os.environ["E2E_GRAFX_WHEEL_URI"]
core_files = [str(path).replace("\\", "/") for path in (core.files or ())]
community_files = [str(path).replace("\\", "/") for path in (community.files or ())]
assert not any(path.startswith("okto_pulse/community/") for path in core_files)
assert "okto_pulse/community/frontend_dist/index.html" in community_files
assert "okto_pulse/community/adapters/global_discovery_recovery_worker.py" in community_files
requirements = [str(requirement).lower() for requirement in (community.requires or ())]
assert any(
    row.replace(" ", "") == "okto-grafx[accel]==0.0.1"
    for row in requirements
), requirements
for direct_dependency in (
    "aiosqlite",
    "anyio",
    "fastapi",
    "filelock",
    "httpx",
    "sqlalchemy",
):
    assert any(row.startswith(direct_dependency) for row in requirements), direct_dependency
core_requirement = next(
    row for row in requirements if row.startswith("okto-pulse-core")
)
assert ">=0.3.3" in core_requirement and "<1.0.0" in core_requirement
console_scripts = {
    entry.name: entry.value
    for entry in community.entry_points
    if entry.group == "console_scripts"
}
assert console_scripts["okto-pulse"] == "okto_pulse.community.cli:main"

javascript = []
for package_path in community.files or ():
    normalized = str(package_path).replace("\\", "/")
    if normalized.startswith("okto_pulse/community/frontend_dist/") and normalized.endswith(".js"):
        javascript.append(community.locate_file(package_path).read_text(encoding="utf-8"))
about = [source for source in javascript if "Community Edition — v" in source]
assert len(about) == 1
assert "0.3.3" in about[0]
assert "Community Edition — v0.2.5" not in "".join(javascript)

print(json.dumps({
    "versions": {
        "core": version("okto-pulse-core"),
        "community": version("okto-pulse"),
    },
    "grafx": {
        "version": version("okto-grafx"),
        "file_count": len(grafx.files or ()),
        "accelerators": {
            "google-crc32c": version("google-crc32c"),
            "numpy": version("numpy"),
        },
        "direct_url": grafx_direct_url["url"],
    },
    "origins": origins,
    "core_file_count": len(core_files),
    "community_file_count": len(community_files),
    "community_requirement_count": len(requirements),
    "about_bundle_count": len(about),
}, sort_keys=True))
"""
    origin_env = {
        **runtime_env,
        "E2E_GRAFX_WHEEL_URI": grafx_wheel.as_uri(),
        "E2E_WORKSPACE_ROOT": str(WORKSPACE_ROOT),
    }
    origin_result = _run_checked(
        [str(python), "-I", "-c", origin_script],
        cwd=root,
        env=origin_env,
    )
    origin_report = json.loads(origin_result.stdout.strip().splitlines()[-1])
    assert origin_report["versions"] == {"community": "0.3.3", "core": "0.3.3"}
    assert origin_report["grafx"]["version"] == EXPECTED_GRAFX_VERSION
    assert origin_report["about_bundle_count"] == 1

    resource_manifest_script = r"""
import json

from okto_pulse.community.adapters.resources import (
    register_and_freeze_community_resource_catalog,
)
from okto_pulse.core.composition import RuntimeValueRegistry, runtime_value_scope

runtime_values = RuntimeValueRegistry()
with runtime_value_scope(runtime_values):
    transaction = register_and_freeze_community_resource_catalog(runtime_values)
    assert transaction._registry is runtime_values
    frozen, identity = transaction.require_frozen_projection()
    assert identity == frozen.manifest.projection_identity
    print(json.dumps(frozen.manifest.as_dict(), sort_keys=True))
    transaction.rollback()
"""
    manifest_result = _run_checked(
        [str(python), "-I", "-c", resource_manifest_script],
        cwd=root,
        env=runtime_env,
        timeout=120,
    )
    resource_manifest = json.loads(manifest_result.stdout.strip().splitlines()[-1])
    assert resource_manifest["count"] == len(resource_manifest["resources"])
    assert resource_manifest["count"] > 0
    assert (
        resource_manifest["manifest_hash"] == resource_manifest["projection_identity"]
    )

    console = _venv_script(venv, "okto-pulse")
    assert console.is_file(), console
    version_result = _run_checked(
        [str(console), "--version"], cwd=root, env=runtime_env, timeout=60
    )
    assert version_result.stdout.strip() == "okto-pulse 0.3.3 (okto-pulse-core 0.3.3)"

    initialized = _run_checked(
        [str(console), "init"],
        cwd=root,
        env=runtime_env,
        timeout=300,
    )
    key_match = re.search(r"API Key:\s+(dash_[A-Za-z0-9]+)", initialized.stdout)
    assert key_match is not None, initialized.stdout[-4000:]
    api_key = key_match.group(1)
    database_path = data_dir / "data" / "pulse.db"
    assert database_path.is_file(), database_path

    # --- Fixture mutation: global-discovery-total-loss ----------------------
    # A5 cmd_init now ALWAYS runs ``_bootstrap_global_discovery_graph`` (cli.py),
    # so a fresh ``okto-pulse init`` MATERIALIZES the Global Discovery graph and
    # prints the exact evidence line below.  Post-init the graph is therefore
    # PRESENT_READABLE_CANDIDATE, and preparation CORRECTLY refuses recovery as
    # ``global_discovery_recovery_not_admitted`` (nothing needs recovering).  To
    # drive a REAL terminal recovery we first PROVE the bootstrap happened, then
    # — while the installed server is still stopped — relocate the COMPLETE
    # storage-observed ``global`` directory out of the observed tree (into the
    # isolated runtime root) and recreate it empty.  Moving the whole directory
    # (never just ``discovery.lbug``) is required: leftover ``discovery.lbug.*``
    # sidecars would classify as residual/unreadable state instead of the
    # CONFIRMED_ABSENT total loss this recovery scenario needs.
    assert "  Global Discovery: materialized" in initialized.stdout, initialized.stdout[
        -4000:
    ]
    global_dir = data_dir / "global"
    assert sorted(global_dir.rglob("discovery.lbug")), sorted(global_dir.rglob("*"))
    total_loss_backup = root / "global-discovery-total-loss-backup"
    assert not total_loss_backup.exists(), total_loss_backup
    shutil.move(str(global_dir), str(total_loss_backup))
    global_dir.mkdir()
    # The backup preserves the materialized primary (plus any WAL/sidecars); the
    # freshly observed ``global`` directory is empty => CONFIRMED_ABSENT.
    assert sorted(total_loss_backup.rglob("discovery.lbug")), sorted(
        total_loss_backup.rglob("*")
    )
    assert list(global_dir.iterdir()) == [], sorted(global_dir.iterdir())

    peer_api_key = f"dash_{secrets.token_hex(24)}"
    peer_credential_script = r"""
import json
import os

from okto_pulse.core.services.application_agents import (
    credential_marker,
    hash_api_key,
)

key_hash = hash_api_key(os.environ["OKTO_E2E_PEER_API_KEY"])
print(json.dumps({"api_key_hash": key_hash, "api_key_marker": credential_marker(key_hash)}))
"""
    peer_credential = json.loads(
        _run_checked(
            [str(python), "-I", "-c", peer_credential_script],
            cwd=root,
            env={**runtime_env, "OKTO_E2E_PEER_API_KEY": peer_api_key},
            timeout=60,
        ).stdout
    )
    assert peer_credential["api_key_marker"].startswith("sha256:")
    assert peer_api_key not in json.dumps(peer_credential)
    peer_actor_id = "00000000-0000-4000-8000-000000000002"

    with sqlite3.connect(database_path, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        actor_rows = connection.execute("SELECT id FROM agents").fetchall()
        assert len(actor_rows) == 1
        actor_id = str(actor_rows[0][0])
        connection.execute(
            "INSERT INTO agents "
            "(id, name, description, objective, api_key, api_key_hash, "
            " is_active, permissions, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, NULL, ?)",
            (
                peer_actor_id,
                "Installed E2E Peer Global Admin",
                "Second real-HTTP recovery operator",
                "Exercise cross-actor global recovery semantics",
                peer_credential["api_key_marker"],
                peer_credential["api_key_hash"],
                "installed-e2e",
            ),
        )
        connection.execute("UPDATE boards SET realm_id = 'local'")
        current_count = int(
            connection.execute("SELECT COUNT(*) FROM boards").fetchone()[0]
        )
        assert current_count == 1
        connection.executemany(
            "INSERT INTO boards (id, name, description, owner_id, realm_id) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    f"e2e-board-{index:04d}",
                    f"Installed E2E Board {index:04d}",
                    "Zero-source authoritative census fixture",
                    "local-user",
                    "local",
                )
                for index in range(1, BOARD_CENSUS_SIZE)
            ],
        )
        observed = int(connection.execute("SELECT COUNT(*) FROM boards").fetchone()[0])
        assert observed == BOARD_CENSUS_SIZE
        # The real daily scheduler is part of this installed runtime. Persist a
        # recent completed tick so its normal catch-up policy schedules the next
        # run at the configured interval instead of the first-install +120 s
        # floor. This isolates recovery from an unrelated 1500-board tick wave;
        # explicit snapshot-drift scenarios below still mutate authoritative
        # source tables and exercise the production fence unchanged.
        connection.execute(
            "INSERT INTO kg_tick_runs "
            "(tick_id, started_at, completed_at, nodes_recomputed, duration_ms, "
            " error, boards_processed, boards_failed) "
            "VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, 0.0, NULL, 0, 0)",
            ("installed-e2e-scheduler-baseline",),
        )
        connection.commit()

    yield InstalledRuntime(
        root=root,
        venv=venv,
        python=python,
        console=console,
        data_dir=data_dir,
        database_path=database_path,
        api_key=api_key,
        actor_id=actor_id,
        peer_api_key=peer_api_key,
        peer_actor_id=peer_actor_id,
        core_wheel=core_wheel,
        community_wheel=community_wheel,
        grafx_wheel=grafx_wheel,
        core_wheel_sha256=core_wheel_sha256,
        community_wheel_sha256=community_wheel_sha256,
        grafx_wheel_sha256=grafx_wheel_sha256,
        clock_file=clock_file,
        fail_once_marker=fail_once_marker,
        preparation_release_file=preparation_release_file,
        building_gate_signal_file=building_gate_signal_file,
        fence_loss_target_file=fence_loss_target_file,
        fence_loss_signal_file=fence_loss_signal_file,
        resume_gate_target_file=resume_gate_target_file,
        resume_gate_signal_file=resume_gate_signal_file,
        resume_gate_release_file=resume_gate_release_file,
        resume_gate_timeout_marker=resume_gate_timeout_marker,
        z2_target_file=z2_target_file,
        z2_signal_file=z2_signal_file,
        z2_tripwire_marker=z2_tripwire_marker,
        adoption_poll_signal_file=adoption_poll_signal_file,
        submit_gate_target_file=submit_gate_target_file,
        submit_gate_signal_file=submit_gate_signal_file,
        submit_gate_release_file=submit_gate_release_file,
        submit_gate_timeout_marker=submit_gate_timeout_marker,
        env=runtime_env,
        origin_report=origin_report,
        resource_manifest=resource_manifest,
    )


def test_final_wheel_mode_reuses_pair_and_authenticates_grafx_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_pair = tmp_path / "final-pair"
    final_pair.mkdir()
    core_wheel = final_pair / "okto_pulse_core-0.3.3-py3-none-any.whl"
    community_wheel = final_pair / "okto_pulse-0.3.3-py3-none-any.whl"
    core_wheel.write_bytes(b"governed-core")
    community_wheel.write_bytes(b"governed-community")
    grafx_wheel = tmp_path / "okto_grafx-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(grafx_wheel, "w") as archive:
        archive.writestr(
            "okto_grafx-0.0.1.dist-info/METADATA",
            "\n".join(
                (
                    "Metadata-Version: 2.4",
                    "Name: okto-grafx",
                    "Version: 0.0.1",
                    "Provides-Extra: accel",
                    'Requires-Dist: numpy>=1.24; extra == "accel"',
                    'Requires-Dist: google-crc32c>=1.5; extra == "accel"',
                    "",
                )
            ),
        )
    grafx_sha256 = _sha256(grafx_wheel)
    monkeypatch.setenv(FINAL_WHEEL_DIR_ENV, str(final_pair))
    monkeypatch.setenv(GRAFX_WHEEL_ENV, str(grafx_wheel))
    monkeypatch.delenv(GRAFX_REPO_ENV, raising=False)
    monkeypatch.delenv(FINAL_CORE_WHEEL_SHA256_ENV, raising=False)
    monkeypatch.delenv(FINAL_COMMUNITY_WHEEL_SHA256_ENV, raising=False)
    monkeypatch.setenv(FINAL_GRAFX_WHEEL_SHA256_ENV, grafx_sha256)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_run_checked",
        lambda *_args, **_kwargs: pytest.fail("FINAL mode attempted a build"),
    )

    selected_core, selected_community = _resolve_pulse_wheel_pair(
        root=tmp_path / "unused-build-root",
        uv="uv-must-not-run",
        build_env={},
    )
    selected_grafx = _resolve_grafx_candidate_wheel(
        root=tmp_path / "staged",
        uv="uv-must-not-run",
        build_env={},
    )

    assert selected_core == core_wheel
    assert selected_community == community_wheel
    assert _sha256(selected_grafx) == grafx_sha256
    assert not (tmp_path / "unused-build-root").exists()

    monkeypatch.delenv(FINAL_GRAFX_WHEEL_SHA256_ENV)
    with pytest.raises(AssertionError, match=FINAL_GRAFX_WHEEL_SHA256_ENV):
        _resolve_grafx_candidate_wheel(
            root=tmp_path / "missing-sha",
            uv="uv-must-not-run",
            build_env={},
        )
    monkeypatch.setenv(FINAL_GRAFX_WHEEL_SHA256_ENV, "0" * 64)
    with pytest.raises(AssertionError):
        _resolve_grafx_candidate_wheel(
            root=tmp_path / "wrong-sha",
            uv="uv-must-not-run",
            build_env={},
        )
    assert not (tmp_path / "wrong-sha" / "dist").exists()


def test_installed_grafx_candidate_materializes_board_and_global_routes(
    tmp_path: Path,
) -> None:
    """Prove the unpublished Grafx wheel through an isolated Pulse install."""

    uv = shutil.which("uv")
    assert uv is not None, "uv is required; set the explicit opt-out marker to skip"
    root = tmp_path / "installed-grafx"
    root.mkdir()
    build_env = {**os.environ, "UV_NO_PROGRESS": "1"}
    core_wheel, community_wheel = _resolve_pulse_wheel_pair(
        root=root,
        uv=uv,
        build_env=build_env,
    )
    grafx_wheel = _resolve_grafx_candidate_wheel(
        root=root,
        uv=uv,
        build_env=build_env,
    )
    core_wheel_sha256 = _sha256(core_wheel)
    community_wheel_sha256 = _sha256(community_wheel)
    grafx_wheel_sha256 = _sha256(grafx_wheel)
    _assert_wheel_resource_ownership(core_wheel, community_wheel)

    venv = root / "venv"
    _run_checked(
        [uv, "venv", str(venv), "--python", sys.executable, "--seed"],
        cwd=root,
        env=build_env,
    )
    python = _venv_python(venv)
    _run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            str(core_wheel),
            str(community_wheel),
            str(grafx_wheel),
        ],
        cwd=root,
        env=build_env,
        timeout=900,
    )
    assert _sha256(core_wheel) == core_wheel_sha256
    assert _sha256(community_wheel) == community_wheel_sha256
    assert _sha256(grafx_wheel) == grafx_wheel_sha256
    _run_checked(
        [uv, "pip", "check", "--python", str(python)],
        cwd=root,
        env=build_env,
        timeout=120,
    )

    data_dir = root / "pulse-home"
    runtime_env = {
        **os.environ,
        "HF_HUB_OFFLINE": "1",
        "KG_GLOBAL_GRAPH_BACKEND": "grafx",
        "KG_GRAFX_PAGE_SIZE": "8192",
        "KG_GRAPH_BACKEND": "grafx",
        "NO_PROXY": "127.0.0.1,localhost",
        "OKTO_PULSE_HOME": str(data_dir),
        "OKTO_PULSE_NO_BANNER": "1",
        "OKTO_PULSE_SKIP_DEMO_SEED": "1",
        "OKTO_PULSE_TERMS_ACCEPTED": "1",
        "PYTHONUNBUFFERED": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "TRANSFORMERS_OFFLINE": "1",
    }
    runtime_env.pop("PYTHONPATH", None)
    runtime_env.pop("PYTHONHOME", None)
    console = _venv_script(venv, "okto-pulse")
    initialized = _run_checked(
        [str(console), "init"],
        cwd=root,
        env=runtime_env,
        timeout=300,
    )
    assert "  Knowledge Graph: board:" in initialized.stdout, initialized.stdout[-4000:]
    assert "  Global Discovery: materialized" in initialized.stdout, initialized.stdout[
        -4000:
    ]

    database_path = data_dir / "data" / "pulse.db"
    with sqlite3.connect(database_path, timeout=30) as connection:
        board_rows = connection.execute(
            "SELECT id FROM boards ORDER BY created_at, id LIMIT 1"
        ).fetchall()
    assert len(board_rows) == 1, board_rows
    board_id = str(board_rows[0][0])

    inspection_script = r"""
import json
import os
import sys
from importlib.metadata import distribution, version
from pathlib import Path

import google_crc32c
import numpy
import okto_grafx
import okto_pulse.community
from okto_grafx import connect
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.config import CommunitySettings

venv = Path(sys.prefix).resolve()
workspace = Path(os.environ["E2E_WORKSPACE_ROOT"]).resolve()
for module in (okto_grafx, okto_pulse.community):
    origin = Path(module.__file__).resolve()
    assert venv in origin.parents, (venv, origin)
    assert workspace not in origin.parents, (workspace, origin)

settings = CommunitySettings(_env_file=None)
assert settings.kg_graph_backend == "grafx"
assert settings.kg_global_graph_backend == "grafx"
assert settings.kg_grafx_page_size == 8192
store = CommunityGraphBackendBindingStore(settings.kg_base_dir)
board = store.acquire_board_binding(os.environ["E2E_BOARD_ID"])
global_route = store.acquire_global_binding()
assert board.backend == "grafx"
assert global_route.backend == "grafx"
assert board.page_size == global_route.page_size == 8192

def inspect(binding):
    # Pulse's productive pool uses the normal Grafx open, which performs the
    # same bounded recovery/checkpoint admission that a fresh server process
    # performs.  A forensic read-only open has a stricter checkpoint-complete
    # precondition and is not the installed runtime path this smoke certifies.
    with connect(binding.physical_path) as database:
        assert database.identity.page_size == binding.page_size
        return sorted(table.name for table in database.catalog.catalog.tables())

board_tables = inspect(board)
global_tables = inspect(global_route)
assert len(board_tables) == 81, board_tables
assert {"BoardMeta", "Entity", "Decision"} <= set(board_tables)
assert len(global_tables) == 11, global_tables
assert {
    "Board",
    "Topic",
    "Entity",
    "DecisionDigest",
    "HAS_TOPIC",
    "CONTAINS_DECISION",
} <= set(global_tables)

grafx_distribution = distribution("okto-grafx")
grafx_direct_url = json.loads(grafx_distribution.read_text("direct_url.json") or "{}")
assert grafx_direct_url["url"] == os.environ["E2E_GRAFX_WHEEL_URI"]
requirements = [
    str(requirement).lower().replace(" ", "")
    for requirement in (distribution("okto-pulse").requires or ())
]
assert "okto-grafx[accel]==0.0.1" in requirements, requirements
print(json.dumps({
    "backends": {"board": board.backend, "global": global_route.backend},
    "board_table_count": len(board_tables),
    "global_table_count": len(global_tables),
    "grafx_version": version("okto-grafx"),
    "grafx_direct_url": grafx_direct_url["url"],
    "accelerators": {
        "google-crc32c": version("google-crc32c"),
        "numpy": version("numpy"),
    },
    "grafx_origin": str(Path(okto_grafx.__file__).resolve()),
}, sort_keys=True))
"""
    inspected = _run_checked(
        [str(python), "-I", "-c", inspection_script],
        cwd=root,
        env={
            **runtime_env,
            "E2E_BOARD_ID": board_id,
            "E2E_GRAFX_WHEEL_URI": grafx_wheel.as_uri(),
            "E2E_WORKSPACE_ROOT": str(WORKSPACE_ROOT),
        },
        timeout=180,
    )
    report = json.loads(inspected.stdout.strip().splitlines()[-1])
    assert report["backends"] == {"board": "grafx", "global": "grafx"}
    assert report["grafx_version"] == EXPECTED_GRAFX_VERSION
    assert report["grafx_direct_url"] == grafx_wheel.as_uri()
    assert report["board_table_count"] == 81
    assert report["global_table_count"] == 11
    assert _sha256(core_wheel) == core_wheel_sha256
    assert _sha256(community_wheel) == community_wheel_sha256
    assert _sha256(grafx_wheel) == grafx_wheel_sha256


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _server_launcher(
    mode: Literal[
        "paused",
        "controlled-clock",
        "controlled-clock-fence-loss",
        "normal",
        "building-gate",
        "resume-gate",
        "z2-tamper",
        "adoption-restart",
    ],
) -> str:
    statements = ["import os", "import sys"]
    if mode == "building-gate":
        # Hard-kill fixture: after production's REAL atomic journal write whose
        # payload carries phase=='building' has returned (durable on disk), the
        # wrapper records the journal path in the signal file and blocks the
        # native worker thread forever.  The test then hard-kills this exact
        # child process; nothing is skipped, mocked or cleaned up.
        statements.extend(
            [
                "import json as _json",
                "import time as _time",
                "from pathlib import Path as _Path",
                "from okto_pulse.community.adapters import global_discovery_recovery as _recovery",
                "_signal_file = _Path(os.environ['OKTO_E2E_BUILDING_GATE_SIGNAL_FILE'])",
                "_real_write_json_atomic = _recovery.write_json_atomic",
                (
                    "def _building_gated_write(path, payload):\n"
                    "    result = _real_write_json_atomic(path, payload)\n"
                    "    if (\n"
                    "        dict(payload).get('phase') == 'building'\n"
                    "        and not _signal_file.exists()\n"
                    "    ):\n"
                    "        _signal_file.write_text(\n"
                    "            _json.dumps({'journal_path': str(path)}),\n"
                    "            encoding='utf-8',\n"
                    "        )\n"
                    "        while True:\n"
                    "            _time.sleep(0.5)\n"
                    "    return result"
                ),
                "_recovery.write_json_atomic = _building_gated_write",
            ]
        )
    elif mode == "paused":
        statements.extend(
            [
                "from okto_pulse.community.adapters import global_discovery_recovery_worker as _worker",
                "def _paused_start(self):\n    return None",
                "_worker.CommunityRecoveryPreparationPoller.start = _paused_start",
            ]
        )
    elif mode == "z2-tamper":
        # Z2 installed fixture (Codex ruling msg_c640e10d): production's REAL
        # durable worker-inputs put runs FIRST inside prepare_durable_start;
        # for the exact targeted run/epoch the wrapper then corrupts the just
        # written artifact strictly before the put returns — and therefore
        # strictly before the recovery dispatch exists — recording a signal
        # with the artifact identity and its original SHA256.  A test-only
        # tripwire on the writer lease proves the failed-inputs attempt never
        # acquired the writer lane; every other acquire passes through.
        statements.extend(
            [
                "import hashlib as _hashlib",
                "import json as _json",
                "from pathlib import Path as _Path",
                "import okto_pulse.core.kg.global_discovery_recovery as _core_recovery",
                "from okto_pulse.community.adapters import global_discovery_recovery_worker as _worker",
                "_z2_target = _Path(os.environ['OKTO_E2E_Z2_TARGET_FILE'])",
                "_z2_signal = _Path(os.environ['OKTO_E2E_Z2_SIGNAL_FILE'])",
                "_z2_tripwire = _Path(os.environ['OKTO_E2E_Z2_TRIPWIRE_MARKER'])",
                "_real_worker_inputs_put = _core_recovery.GlobalDiscoveryRecoveryWorkerInputStore.put",
                (
                    "def _tampering_put(self, inputs):\n"
                    "    persisted = _real_worker_inputs_put(self, inputs)\n"
                    "    if _z2_target.exists() and not _z2_signal.exists():\n"
                    "        target = _json.loads(_z2_target.read_text(encoding='utf-8'))\n"
                    "        run_id = str(inputs.command.binding.run_id)\n"
                    "        epoch = int(inputs.command.expected_epoch)\n"
                    "        if (\n"
                    "            run_id == str(target['run_id'])\n"
                    "            and epoch == int(target['epoch'])\n"
                    "        ):\n"
                    "            reference = _Path(\n"
                    "                self._store.reference(self._key(run_id, epoch))\n"
                    "            )\n"
                    "            digest = _hashlib.sha256(run_id.encode('utf-8')).hexdigest()\n"
                    "            assert digest in reference.name, reference\n"
                    "            assert f'__attempt-{epoch}' in reference.name, reference\n"
                    "            assert reference.is_file(), reference\n"
                    "            original_sha = _hashlib.sha256(\n"
                    "                reference.read_bytes()\n"
                    "            ).hexdigest()\n"
                    "            variant = str(target['variant'])\n"
                    "            if variant == 'missing':\n"
                    "                reference.unlink()\n"
                    "            else:\n"
                    "                reference.write_text('{', encoding='utf-8')\n"
                    "            _z2_signal.write_text(\n"
                    "                _json.dumps(\n"
                    "                    {\n"
                    "                        'run_id': run_id,\n"
                    "                        'epoch': epoch,\n"
                    "                        'variant': variant,\n"
                    "                        'artifact_path': str(reference),\n"
                    "                        'original_sha256': original_sha,\n"
                    "                    }\n"
                    "                ),\n"
                    "                encoding='utf-8',\n"
                    "            )\n"
                    "    return persisted"
                ),
                "_core_recovery.GlobalDiscoveryRecoveryWorkerInputStore.put = _tampering_put",
                "_real_z2_lease_acquire = _worker.GlobalDiscoveryWriterLease.acquire",
                (
                    "def _tripwire_acquire(**kwargs):\n"
                    "    owner = str(kwargs.get('owner_id') or '')\n"
                    "    if _z2_target.exists():\n"
                    "        target = _json.loads(_z2_target.read_text(encoding='utf-8'))\n"
                    "        if owner.startswith(str(target['run_id'])):\n"
                    "            _z2_tripwire.write_text(owner, encoding='utf-8')\n"
                    "            raise AssertionError(\n"
                    "                'z2 tripwire: writer lease acquired for failed-inputs run'\n"
                    "            )\n"
                    "    return _real_z2_lease_acquire(**kwargs)"
                ),
                "_worker.GlobalDiscoveryWriterLease.acquire = _tripwire_acquire",
            ]
        )
    elif mode == "adoption-restart":
        # Hard-kill restart (Codex rulings msg_c640e10d + msg_c6a5726c):
        # (1) record ONE UTC signal at the restarted recovery poller's first
        # entry so the bounded takeover proof anchors on the worker's real
        # start instead of wall-clock observation that includes boot time;
        # (2) gate _submit_claim for the exact target run/epoch — the hook
        # runs AFTER claim_next_dispatch returned (the claim transaction with
        # the A5R2 crash charge is durably COMMITTED) and BEFORE _run_attempt
        # starts any physical operation, so the test can read and prove the
        # EXACT settled charge/rebase before releasing any new work.  The
        # release always arrives from the test's finally; a 120s deadline
        # proceeds WITHOUT fabricating errors, leaving a timeout marker the
        # test must prove absent.  Both wrappers delegate to the real
        # implementations unchanged.
        statements.extend(
            [
                "import json as _json",
                "import time as _time",
                "from datetime import datetime as _UtcDateTime, timezone as _UtcTimezone",
                "from pathlib import Path as _Path",
                "from okto_pulse.community.adapters import global_discovery_recovery_worker as _worker",
                "_poll_signal = _Path(os.environ['OKTO_E2E_ADOPTION_POLL_SIGNAL_FILE'])",
                "_gate_target = _Path(os.environ['OKTO_E2E_SUBMIT_GATE_TARGET_FILE'])",
                "_gate_signal = _Path(os.environ['OKTO_E2E_SUBMIT_GATE_SIGNAL_FILE'])",
                "_gate_release = _Path(os.environ['OKTO_E2E_SUBMIT_GATE_RELEASE_FILE'])",
                "_gate_timeout_marker = _Path(os.environ['OKTO_E2E_SUBMIT_GATE_TIMEOUT_MARKER'])",
                "_real_recovery_poll = _worker.CommunityRecoveryWorker._poll",
                (
                    "def _signalling_poll(self, *args, **kwargs):\n"
                    "    if not _poll_signal.exists():\n"
                    "        _poll_signal.write_text(\n"
                    "            _UtcDateTime.now(_UtcTimezone.utc).strftime(\n"
                    "                '%Y-%m-%d %H:%M:%S.%f'\n"
                    "            ),\n"
                    "            encoding='ascii',\n"
                    "        )\n"
                    "    return _real_recovery_poll(self, *args, **kwargs)"
                ),
                "_worker.CommunityRecoveryWorker._poll = _signalling_poll",
                "_real_submit_claim = _worker.CommunityRecoveryWorker._submit_claim",
                (
                    "def _gated_submit_claim(self, claim):\n"
                    "    if _gate_target.exists() and not _gate_signal.exists():\n"
                    "        target = _json.loads(_gate_target.read_text(encoding='utf-8'))\n"
                    "        if (\n"
                    "            str(claim.run_id) == str(target['run_id'])\n"
                    "            and int(claim.epoch) == int(target['epoch'])\n"
                    "        ):\n"
                    "            _gate_signal.write_text(\n"
                    "                _json.dumps(\n"
                    "                    {\n"
                    "                        'run_id': str(claim.run_id),\n"
                    "                        'epoch': int(claim.epoch),\n"
                    "                        'attempt_id': str(claim.attempt_id),\n"
                    "                        'attempt_count': int(claim.attempt_count),\n"
                    "                        'claim_token': str(claim.claim_token),\n"
                    "                        'worker_id': str(claim.worker_id),\n"
                    "                        'claimed_at': str(claim.claimed_at),\n"
                    "                        'claim_expires_at': str(claim.claim_expires_at),\n"
                    "                    }\n"
                    "                ),\n"
                    "                encoding='utf-8',\n"
                    "            )\n"
                    "            deadline = _time.monotonic() + 120.0\n"
                    "            while not _gate_release.exists():\n"
                    "                if _time.monotonic() >= deadline:\n"
                    "                    _gate_timeout_marker.write_text(\n"
                    "                        'timeout', encoding='ascii'\n"
                    "                    )\n"
                    "                    break\n"
                    "                _time.sleep(0.05)\n"
                    "    return _real_submit_claim(self, claim)"
                ),
                "_worker.CommunityRecoveryWorker._submit_claim = _gated_submit_claim",
            ]
        )
    elif mode == "resume-gate":
        # Cooperative N+1 gate (S4.0R ruling): hold the REAL native operation
        # for the exact target run/epoch AFTER the durable resume admission so
        # the public resume replay can be exercised strictly before terminal.
        # The claim/heartbeat side stays fully productive during the hold; the
        # release always arrives from the test's finally (or the bounded
        # timeout proceeds WITHOUT fabricating any error, leaving a marker the
        # test must prove absent).
        statements.extend(
            [
                "import json as _json",
                "import time as _time",
                "from pathlib import Path as _Path",
                "from okto_pulse.community.adapters import global_discovery_recovery_worker as _worker",
                "_gate_target = _Path(os.environ['OKTO_E2E_RESUME_GATE_TARGET_FILE'])",
                "_gate_signal = _Path(os.environ['OKTO_E2E_RESUME_GATE_SIGNAL_FILE'])",
                "_gate_release = _Path(os.environ['OKTO_E2E_RESUME_GATE_RELEASE_FILE'])",
                "_gate_timeout_marker = _Path(os.environ['OKTO_E2E_RESUME_GATE_TIMEOUT_MARKER'])",
                "_real_native_call = _worker.CommunityGlobalDiscoveryRecoveryNativeOperation.__call__",
                (
                    "def _gated_native_call(self, **kwargs):\n"
                    "    if _gate_target.exists() and not _gate_signal.exists():\n"
                    "        target = _json.loads(_gate_target.read_text(encoding='utf-8'))\n"
                    "        if (\n"
                    "            str(kwargs.get('run_id')) == str(target['run_id'])\n"
                    "            and int(kwargs.get('epoch')) == int(target['epoch'])\n"
                    "        ):\n"
                    "            _gate_signal.write_text(\n"
                    "                _json.dumps(\n"
                    "                    {\n"
                    "                        'run_id': str(kwargs.get('run_id')),\n"
                    "                        'epoch': int(kwargs.get('epoch')),\n"
                    "                    }\n"
                    "                ),\n"
                    "                encoding='utf-8',\n"
                    "            )\n"
                    "            deadline = _time.monotonic() + 120.0\n"
                    "            while not _gate_release.exists():\n"
                    "                if _time.monotonic() >= deadline:\n"
                    "                    _gate_timeout_marker.write_text(\n"
                    "                        'timeout', encoding='ascii'\n"
                    "                    )\n"
                    "                    break\n"
                    "                _time.sleep(0.05)\n"
                    "    return _real_native_call(self, **kwargs)"
                ),
                "_worker.CommunityGlobalDiscoveryRecoveryNativeOperation.__call__ = _gated_native_call",
            ]
        )
    elif mode in ("controlled-clock", "controlled-clock-fence-loss"):
        statements.extend(
            [
                "from datetime import datetime as _RealDateTime, timedelta as _Timedelta, timezone as _Timezone",
                "from pathlib import Path as _Path",
                "import time as _time",
                "import traceback as _traceback",
                "import okto_pulse.core.kg.rebuild_confirmation as _confirmation",
                "from okto_pulse.community.adapters import global_discovery_recovery_preparation as _preparation",
                "from okto_pulse.community.adapters import global_discovery_recovery_worker as _worker",
                "_clock_file = _Path(os.environ['OKTO_E2E_CONFIRMATION_CLOCK_FILE'])",
                "_fail_once_marker = _Path(os.environ['OKTO_E2E_FAIL_ONCE_MARKER'])",
                "_preparation_release_file = _Path(os.environ['OKTO_E2E_PREPARATION_RELEASE_FILE'])",
                (
                    "class _HarnessDateTime(_RealDateTime):\n"
                    "    @classmethod\n"
                    "    def now(cls, tz=None):\n"
                    "        offset = float(_clock_file.read_text(encoding='ascii').strip() or '0')\n"
                    "        value = _RealDateTime.now(_Timezone.utc) + _Timedelta(seconds=offset)\n"
                    "        return value.replace(tzinfo=None) if tz is None else value.astimezone(tz)"
                ),
                "_confirmation.datetime = _HarnessDateTime",
                "_real_preparation_call = _preparation.CommunityGlobalDiscoveryRecoveryPreparationOperation.__call__",
                (
                    "def _observable_preparation(self, *args, **kwargs):\n"
                    "    deadline = _time.monotonic() + 30.0\n"
                    "    while not _preparation_release_file.exists():\n"
                    "        if _time.monotonic() >= deadline:\n"
                    "            raise RuntimeError('installed_e2e_preparation_release_timeout')\n"
                    "        _time.sleep(0.02)\n"
                    "    try:\n"
                    "        return _real_preparation_call(self, *args, **kwargs)\n"
                    "    except BaseException:\n"
                    "        _traceback.print_exc()\n"
                    "        raise"
                ),
                "_preparation.CommunityGlobalDiscoveryRecoveryPreparationOperation.__call__ = _observable_preparation",
                "_real_dispatch = _worker.CommunityDurableRecoveryDispatcher.dispatch",
                (
                    "def _fail_once_after_durable_dispatch(self, *args, **kwargs):\n"
                    "    result = _real_dispatch(self, *args, **kwargs)\n"
                    "    kind = kwargs.get('kind')\n"
                    "    if getattr(kind, 'value', kind) == 'recovery' and not _fail_once_marker.exists():\n"
                    "        _fail_once_marker.write_text('1', encoding='ascii')\n"
                    "        raise RuntimeError('installed_e2e_fail_once_after_durable_dispatch')\n"
                    "    return result"
                ),
                "_worker.CommunityDurableRecoveryDispatcher.dispatch = _fail_once_after_durable_dispatch",
            ]
        )
        if mode == "controlled-clock-fence-loss":
            # Deterministic REAL writer-fence loss (S4.0R ruling): capture the
            # productive GlobalDiscoveryWriterLease instance unchanged, and
            # after production's real atomic journal write with
            # phase='pointer_switched' has returned durable, release that
            # exact lease through its OFFICIAL release path (no artificial
            # exception, no SQL). The very next productive fence check raises
            # GlobalDiscoveryWriterFenceLost and the productive worker alone
            # persists PARTIAL/recovery_physical_reconciliation_pending.
            statements.extend(
                [
                    "import json as _json",
                    "from okto_pulse.community.adapters import global_discovery_recovery as _recovery",
                    "_fence_target = _Path(os.environ['OKTO_E2E_FENCE_LOSS_TARGET_FILE'])",
                    "_fence_signal = _Path(os.environ['OKTO_E2E_FENCE_LOSS_SIGNAL_FILE'])",
                    "_captured_leases = []",
                    "_real_lease_acquire = _worker.GlobalDiscoveryWriterLease.acquire",
                    (
                        "def _capturing_acquire(**kwargs):\n"
                        "    lease = _real_lease_acquire(**kwargs)\n"
                        "    if kwargs.get('operation') == 'global_discovery_recovery':\n"
                        "        _captured_leases.append(lease)\n"
                        "    return lease"
                    ),
                    "_worker.GlobalDiscoveryWriterLease.acquire = _capturing_acquire",
                    "_real_write_json_atomic_fence = _recovery.write_json_atomic",
                    (
                        "def _fence_losing_write(path, payload):\n"
                        "    result = _real_write_json_atomic_fence(path, payload)\n"
                        "    data = dict(payload)\n"
                        "    if (\n"
                        "        data.get('phase') == 'building'\n"
                        "        and _fence_target.exists()\n"
                        "        and not _fence_signal.exists()\n"
                        "        and _captured_leases\n"
                        "    ):\n"
                        "        target = _json.loads(_fence_target.read_text(encoding='utf-8'))\n"
                        "        if (\n"
                        "            str(data.get('run_id')) == str(target['run_id'])\n"
                        "            and int(data.get('epoch') or 0) == int(target['epoch'])\n"
                        "        ):\n"
                        "            released = _captured_leases[-1].release()\n"
                        "            _fence_signal.write_text(\n"
                        "                _json.dumps(\n"
                        "                    {\n"
                        "                        'journal_path': str(path),\n"
                        "                        'released': bool(released),\n"
                        "                        'run_id': str(data.get('run_id')),\n"
                        "                        'epoch': int(data.get('epoch') or 0),\n"
                        "                    }\n"
                        "                ),\n"
                        "                encoding='utf-8',\n"
                        "            )\n"
                        "    return result"
                    ),
                    "_recovery.write_json_atomic = _fence_losing_write",
                ]
            )
    statements.extend(
        [
            "from okto_pulse.community.cli import main as _main",
            "_main()",
        ]
    )
    return "\n".join(statements)


@dataclass
class RunningServer:
    process: subprocess.Popen[str]
    api_url: str
    mcp_url: str
    log_path: Path

    def log_tail(self, length: int = 12_000) -> str:
        with contextlib.suppress(OSError):
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-length:]
        return ""


def _stop_server(server: RunningServer) -> None:
    process = server.process
    if process.poll() is not None:
        return
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


@contextmanager
def _running_server(
    runtime: InstalledRuntime,
    *,
    mode: Literal[
        "paused",
        "controlled-clock",
        "controlled-clock-fence-loss",
        "normal",
        "building-gate",
        "resume-gate",
        "z2-tamper",
        "adoption-restart",
    ],
) -> Iterator[RunningServer]:
    api_port = _free_port()
    mcp_port = _free_port()
    while mcp_port == api_port:
        mcp_port = _free_port()
    log_path = runtime.root / f"server-{mode}-{api_port}-{mcp_port}.log"
    log_stream = log_path.open("w", encoding="utf-8")
    env = {
        **runtime.env,
        "OKTO_E2E_CONFIRMATION_CLOCK_FILE": str(runtime.clock_file),
        "OKTO_E2E_FAIL_ONCE_MARKER": str(runtime.fail_once_marker),
        "OKTO_E2E_PREPARATION_RELEASE_FILE": str(runtime.preparation_release_file),
        "OKTO_E2E_BUILDING_GATE_SIGNAL_FILE": str(runtime.building_gate_signal_file),
        "OKTO_E2E_FENCE_LOSS_TARGET_FILE": str(runtime.fence_loss_target_file),
        "OKTO_E2E_FENCE_LOSS_SIGNAL_FILE": str(runtime.fence_loss_signal_file),
        "OKTO_E2E_RESUME_GATE_TARGET_FILE": str(runtime.resume_gate_target_file),
        "OKTO_E2E_RESUME_GATE_SIGNAL_FILE": str(runtime.resume_gate_signal_file),
        "OKTO_E2E_RESUME_GATE_RELEASE_FILE": str(runtime.resume_gate_release_file),
        "OKTO_E2E_RESUME_GATE_TIMEOUT_MARKER": str(runtime.resume_gate_timeout_marker),
        "OKTO_E2E_Z2_TARGET_FILE": str(runtime.z2_target_file),
        "OKTO_E2E_Z2_SIGNAL_FILE": str(runtime.z2_signal_file),
        "OKTO_E2E_Z2_TRIPWIRE_MARKER": str(runtime.z2_tripwire_marker),
        "OKTO_E2E_ADOPTION_POLL_SIGNAL_FILE": str(runtime.adoption_poll_signal_file),
        "OKTO_E2E_SUBMIT_GATE_TARGET_FILE": str(runtime.submit_gate_target_file),
        "OKTO_E2E_SUBMIT_GATE_SIGNAL_FILE": str(runtime.submit_gate_signal_file),
        "OKTO_E2E_SUBMIT_GATE_RELEASE_FILE": str(runtime.submit_gate_release_file),
        "OKTO_E2E_SUBMIT_GATE_TIMEOUT_MARKER": str(runtime.submit_gate_timeout_marker),
    }
    command = [
        str(runtime.python),
        "-I",
        "-c",
        _server_launcher(mode),
        "serve",
        "--api-port",
        str(api_port),
        "--mcp-port",
        str(mcp_port),
        "--accept-terms",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        command,
        cwd=runtime.root,
        env=env,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )
    server = RunningServer(
        process=process,
        api_url=f"http://127.0.0.1:{api_port}",
        mcp_url=f"http://127.0.0.1:{mcp_port}/mcp",
        log_path=log_path,
    )
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_stream.flush()
                pytest.fail(
                    f"installed server exited during startup ({process.returncode})\n"
                    f"{server.log_tail()}"
                )
            try:
                response = httpx.get(f"{server.api_url}/health", timeout=2.0)
                if response.status_code == 200:
                    with socket.create_connection(("127.0.0.1", mcp_port), timeout=2):
                        break
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(0.2)
        else:
            log_stream.flush()
            pytest.fail(f"installed server readiness timed out\n{server.log_tail()}")
        yield server
    except BaseException:
        # Preserve the installed process diagnostics in pytest's captured
        # output.  The module-scoped temp tree is removed during fixture
        # teardown, so a failure that only points at ``log_path`` loses the
        # exception raised inside the wheel-installed server.
        _stop_server(server)
        log_stream.flush()
        print(
            f"\n--- installed server log ({mode}) ---\n{server.log_tail()}",
            file=sys.stderr,
            flush=True,
        )
        raise
    finally:
        _stop_server(server)
        log_stream.close()


def _authenticated_mcp_url(
    runtime: InstalledRuntime,
    server: RunningServer,
    *,
    api_key: str | None = None,
) -> str:
    credential = runtime.api_key if api_key is None else api_key
    return f"{server.mcp_url}?api_key={quote(credential, safe='')}"


async def _tool_payload(
    client: Client,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = await client.call_tool(
        name,
        arguments or {},
        timeout=45,
        raise_on_error=False,
    )
    envelope = result.data
    assert isinstance(envelope, dict), (name, result)
    assert result.structured_content == envelope
    assert {
        "outcome",
        "data",
        "error_code",
        "message",
        "retryable",
        "next_action",
        "meta",
    } <= envelope.keys(), (name, envelope)
    outcome = envelope["outcome"]
    assert outcome in {"success", "action_required", "error"}, (name, envelope)
    assert result.is_error is (outcome == "error"), (name, result, envelope)
    assert isinstance(envelope["retryable"], bool), (name, envelope)
    if outcome == "success":
        assert envelope["error_code"] is None, (name, envelope)
    else:
        assert isinstance(envelope["error_code"], str), (name, envelope)
        assert envelope["error_code"], (name, envelope)
    assert envelope["message"] is None or isinstance(envelope["message"], str)
    assert envelope["next_action"] is None or isinstance(envelope["next_action"], dict)
    assert envelope["meta"] == {
        "contract": "okto-pulse.mcp-tool-outcome",
        "contract_version": "2.0",
        "tool": name,
    }
    domain_data = envelope["data"]
    assert isinstance(domain_data, dict), (name, envelope)
    return domain_data


async def _wait_for_phase(
    client: Client,
    run_id: str,
    phases: set[str],
    *,
    timeout: float = 360,
    poll_interval: float = 0.2,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await _tool_payload(
            client,
            "okto_pulse_kg_global_discovery_recovery_status",
            {"run_id": run_id},
        )
        assert "error" not in last, last
        if str(last.get("phase")) in phases:
            return last
        if last.get("state") in {"failed", "cancelled", "timeout", "partial"}:
            raise AssertionError(f"recovery became terminal before {phases}: {last}")
        await asyncio.sleep(poll_interval)
    raise AssertionError(f"timed out waiting for {phases}; last={last}")


async def _wait_for_prepared_cancellation(
    client: Client,
    response: dict[str, Any],
    *,
    expected_actor_id: str,
    expected_requester_id: str,
    expected_reason: str,
    timeout: float = 120,
) -> dict[str, Any]:
    """Accept the bounded intent acknowledgement, then prove terminal truth."""

    assert response["state"] in {"pending", "cancelled"}, response
    assert response["actor_id"] == expected_actor_id
    assert response["cancel_requested_by_actor_id"] == expected_requester_id
    assert response["audit_reason"] == expected_reason
    if response["state"] == "pending":
        assert response["reason_code"] == "recovery_cancel_requested"
        response = await _wait_for_phase(
            client,
            response["run_id"],
            {"terminal"},
            timeout=timeout,
        )

    assert response["state"] == "cancelled"
    assert response["phase"] == "terminal"
    assert response["terminal_outcome"] == "cancelled"
    assert response["reason_code"] == "recovery_prepared_cancelled"
    assert response["actor_id"] == expected_actor_id
    assert response["cancel_requested_by_actor_id"] == expected_requester_id
    assert response["audit_reason"] == expected_reason
    return response


def _mutate_authoritative_board(runtime: InstalledRuntime, index: int) -> None:
    board_id = f"e2e-board-{index:04d}"
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        changed = connection.execute(
            "UPDATE boards SET name = name || ? WHERE id = ?",
            (f"-drift-{index}", board_id),
        )
        assert changed.rowcount == 1
        connection.commit()


def _seed_terminal_outbox(runtime: InstalledRuntime) -> str:
    dead_letter_id = "installed-e2e-global-dlq"
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO global_update_outbox "
            "(id, event_id, board_id, session_id, event_type, payload, "
            " created_at, processed_at, retry_count, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL, -1, ?)",
            (
                dead_letter_id,
                "installed-e2e-global-event",
                "e2e-board-0001",
                "installed-e2e-session",
                "consolidation_committed",
                json.dumps({"artifact_id": "installed-e2e-artifact"}),
                "graph_unavailable: installed E2E terminal delivery",
            ),
        )
        connection.commit()
    return dead_letter_id


def _mark_outbox_applied(runtime: InstalledRuntime, dead_letter_id: str) -> None:
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        changed = connection.execute(
            "UPDATE global_update_outbox SET processed_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (dead_letter_id,),
        )
        assert changed.rowcount == 1
        connection.commit()


async def _assert_served_about(server: RunningServer) -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=20) as http:
        health = await http.get(f"{server.api_url}/health")
        assert health.status_code == 200
        index = await http.get(f"{server.api_url}/")
        assert index.status_code == 200
        scripts = re.findall(r'src=["\']([^"\']+\.js)["\']', index.text)
        assert scripts, index.text[:1000]
        bundles = []
        for script in scripts:
            response = await http.get(f"{server.api_url}/{script.lstrip('/')}")
            assert response.status_code == 200, script
            bundles.append(response.text)
    about = [source for source in bundles if "Community Edition — v" in source]
    assert len(about) == 1
    assert "0.3.3" in about[0]
    assert "Community Edition — v0.2.5" not in "".join(bundles)


async def _assert_resource_manifest_parity(
    client: Client,
    expected_manifest: dict[str, Any],
) -> None:
    listed_by_uri: dict[str, Any] = {}
    cursor: str | None = None
    observed_cursors: set[str] = set()
    page_count = 0
    while True:
        page = await client.session.list_resources(cursor=cursor)
        page_count += 1
        for resource in page.resources:
            uri = str(resource.uri)
            assert uri not in listed_by_uri, uri
            listed_by_uri[uri] = resource
        cursor = page.nextCursor
        if cursor is None:
            break
        assert cursor not in observed_cursors, cursor
        observed_cursors.add(cursor)

    expected_entries = {
        str(entry["uri"]): entry for entry in expected_manifest["resources"]
    }
    assert page_count >= 1
    assert set(listed_by_uri) == set(expected_entries)
    assert len(listed_by_uri) == expected_manifest["count"]

    observed_unicode = False
    for uri, expected in expected_entries.items():
        listed = listed_by_uri[uri]
        assert listed.name == expected["name"]
        assert listed.mimeType == expected["mime_type"]
        contents = await client.read_resource(uri)
        assert len(contents) == 1, (uri, contents)
        content = contents[0]
        assert str(content.uri) == uri
        assert content.mimeType == expected["mime_type"]
        text = getattr(content, "text", None)
        assert isinstance(text, str), (uri, type(content))
        observed_unicode = observed_unicode or any(ord(char) > 127 for char in text)
        assert (
            hashlib.sha256(text.encode("utf-8")).hexdigest()
            == expected["content_sha256"]
        )

    assert observed_unicode
    community_entries = [
        entry
        for entry in expected_entries.values()
        if entry["owning_edition"] == "community"
    ]
    assert len(community_entries) == 4
    assert all(
        str(entry["source_identity"]).startswith("community:")
        for entry in community_entries
    )


async def _assert_mcp_inventory(
    client: Client,
    resource_manifest: dict[str, Any],
) -> None:
    tools = await client.list_tools()
    names = sorted(tool.name for tool in tools)
    resource = await client.read_resource("okto-pulse://server-manifest")
    manifest = json.loads(resource[0].text)
    assert client.initialize_result.serverInfo.version == "0.3.3"
    assert len(names) == EXPECTED_TOOL_COUNT
    assert len(names) == manifest["tool_inventory"]["count"]
    assert manifest["tool_inventory"]["aliases"] == EXPECTED_TOOL_ALIASES
    assert len(names) - len(EXPECTED_TOOL_ALIASES) == EXPECTED_CANONICAL_TOOL_COUNT
    tool_document = {
        "tools": names,
        "aliases": manifest["tool_inventory"]["aliases"],
    }
    tool_hash = hashlib.sha256(
        json.dumps(
            tool_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    assert tool_hash == manifest["tool_inventory"]["sha256"]
    assert tool_hash == EXPECTED_TOOL_INVENTORY_SHA256
    for required in (
        "okto_pulse_kg_global_discovery_recovery_preflight",
        "okto_pulse_kg_global_discovery_recovery_confirm",
        "okto_pulse_kg_global_discovery_recovery_run",
        "okto_pulse_kg_global_discovery_recovery_status",
        "okto_pulse_kg_global_discovery_recovery_resume",
        "okto_pulse_kg_global_discovery_recovery_cancel",
        "okto_pulse_kg_global_outbox_dead_letter_list",
        "okto_pulse_kg_global_outbox_dead_letter_reprocess",
        "okto_pulse_kg_global_outbox_dead_letter_verify",
    ):
        assert required in names
    await _assert_resource_manifest_parity(client, resource_manifest)


@pytest.mark.asyncio
async def test_installed_wheels_serve_exact_frozen_resource_manifest_over_real_http(
    installed_runtime: InstalledRuntime,
) -> None:
    runtime = installed_runtime
    assert runtime.origin_report["versions"] == {
        "community": "0.3.3",
        "core": "0.3.3",
    }
    assert _sha256(runtime.core_wheel) == runtime.core_wheel_sha256
    assert _sha256(runtime.community_wheel) == runtime.community_wheel_sha256
    assert _sha256(runtime.grafx_wheel) == runtime.grafx_wheel_sha256
    with _running_server(runtime, mode="normal") as server:
        async with Client(
            _authenticated_mcp_url(runtime, server),
            timeout=45,
            init_timeout=45,
        ) as client:
            await _assert_mcp_inventory(client, runtime.resource_manifest)


@pytest.mark.asyncio
async def test_installed_wheels_drive_recovery_and_dlq_over_real_http(
    installed_runtime: InstalledRuntime,
) -> None:
    runtime = installed_runtime
    assert runtime.origin_report["versions"] == {
        "community": "0.3.3",
        "core": "0.3.3",
    }
    assert _sha256(runtime.core_wheel) == runtime.core_wheel_sha256
    assert _sha256(runtime.community_wheel) == runtime.community_wheel_sha256
    assert _sha256(runtime.grafx_wheel) == runtime.grafx_wheel_sha256

    # Deterministic queued replay/cancel: only the owned preparation consumer is
    # paused. Admission, persistence, auth, transport, and cancellation are real.
    with _running_server(runtime, mode="paused") as server:
        await _assert_served_about(server)
        async with (
            Client(
                _authenticated_mcp_url(runtime, server), timeout=45, init_timeout=45
            ) as client,
            Client(
                _authenticated_mcp_url(runtime, server, api_key=runtime.peer_api_key),
                timeout=45,
                init_timeout=45,
            ) as peer_client,
        ):
            await _assert_mcp_inventory(client, runtime.resource_manifest)
            queued = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_preflight",
            )
            assert "phase" in queued, queued
            assert queued["phase"] == "queued"
            assert queued["actor_id"] == runtime.actor_id
            queued_replay = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_preflight",
            )
            assert queued_replay["run_id"] == queued["run_id"]
            assert queued_replay["idempotent_replay"] is True
            assert queued_replay["actor_id"] == runtime.actor_id
            peer_status = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_status",
                {"run_id": queued["run_id"]},
            )
            assert peer_status["actor_id"] == runtime.actor_id
            cancelled = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_cancel",
                {
                    "run_id": queued["run_id"],
                    "expected_epoch": queued["epoch"],
                    "reason": "installed E2E queued cancellation",
                },
            )
            assert cancelled["state"] == "cancelled"
            assert cancelled["terminal_outcome"] == "cancelled"
            assert cancelled["actor_id"] == runtime.actor_id
            assert cancelled["cancel_requested_by_actor_id"] == runtime.peer_actor_id

    runtime.clock_file.write_text("0", encoding="ascii")
    accepted_start: dict[str, Any]
    start_arguments: dict[str, Any]
    with _running_server(runtime, mode="controlled-clock-fence-loss") as server:
        async with (
            Client(
                _authenticated_mcp_url(runtime, server), timeout=45, init_timeout=45
            ) as client,
            Client(
                _authenticated_mcp_url(runtime, server, api_key=runtime.peer_api_key),
                timeout=45,
                init_timeout=45,
            ) as peer_client,
        ):
            # Run 1: observe real preparing/prepared replay, then refuse stale
            # fingerprint at confirm and cancel the prepared attempt.
            first = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_preflight",
            )
            assert first["actor_id"] == runtime.actor_id
            preparing = await _wait_for_phase(
                peer_client, first["run_id"], {"preparing"}
            )
            preparing_replay = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_preflight",
            )
            assert preparing_replay["run_id"] == first["run_id"]
            assert preparing_replay["phase"] == "preparing"
            assert preparing_replay["idempotent_replay"] is True
            assert preparing_replay["actor_id"] == runtime.actor_id
            runtime.preparation_release_file.write_text("release", encoding="ascii")
            prepared = await _wait_for_phase(
                peer_client,
                first["run_id"],
                {"prepared"},
                timeout=480,
                poll_interval=1.0,
            )
            assert prepared["counts"]["boards_total"] == BOARD_CENSUS_SIZE
            assert preparing["epoch"] == prepared["epoch"] == 1
            assert prepared["actor_id"] == runtime.actor_id
            prepared_replay = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_preflight",
            )
            assert prepared_replay["run_id"] == first["run_id"]
            assert prepared_replay["phase"] == "prepared"
            _mutate_authoritative_board(runtime, 1)
            stale_confirm = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_confirm",
                {
                    "run_id": prepared["run_id"],
                    "manifest_ref": prepared["manifest_ref"],
                    "preflight_hash": prepared["preflight_hash"],
                },
            )
            assert stale_confirm["error"] == "manifest_stale"
            prepared_cancel_reason = "installed E2E stale confirm cancellation"
            prepared_cancel = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_cancel",
                {
                    "run_id": prepared["run_id"],
                    "expected_epoch": prepared["epoch"],
                    "reason": prepared_cancel_reason,
                },
            )
            await _wait_for_prepared_cancellation(
                peer_client,
                prepared_cancel,
                expected_actor_id=runtime.actor_id,
                expected_requester_id=runtime.peer_actor_id,
                expected_reason=prepared_cancel_reason,
            )

            # Run 2: confirmation hash error, confirmation-token TTL expiry at
            # start, then a fresh token refused after source-fingerprint drift.
            second = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_preflight",
            )
            second_prepared = await _wait_for_phase(
                client,
                second["run_id"],
                {"prepared"},
                timeout=480,
                poll_interval=1.0,
            )
            wrong_hash = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_confirm",
                {
                    "run_id": second_prepared["run_id"],
                    "manifest_ref": second_prepared["manifest_ref"],
                    "preflight_hash": "sha256:not-the-preflight-hash",
                },
            )
            assert wrong_hash["error"] == "preflight_hash_mismatch"
            confirmation = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_confirm",
                {
                    "run_id": second_prepared["run_id"],
                    "manifest_ref": second_prepared["manifest_ref"],
                    "preflight_hash": second_prepared["preflight_hash"],
                },
            )
            assert confirmation["outcome"] == "confirmation_issued"
            ttl_arguments = {
                "confirmation_id": confirmation["confirmation_id"],
                "manifest_ref": second_prepared["manifest_ref"],
                "preflight_hash": second_prepared["preflight_hash"],
                "reason": "installed E2E expired confirmation",
            }
            runtime.clock_file.write_text("600", encoding="ascii")
            expired_start = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_run",
                ttl_arguments,
            )
            assert expired_start["error"] == "confirmation_refused"
            assert "expired" in expired_start["reason"]
            runtime.clock_file.write_text("0", encoding="ascii")

            fresh_confirmation = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_confirm",
                {
                    "run_id": second_prepared["run_id"],
                    "manifest_ref": second_prepared["manifest_ref"],
                    "preflight_hash": second_prepared["preflight_hash"],
                },
            )
            _mutate_authoritative_board(runtime, 2)
            stale_start = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_run",
                {
                    "confirmation_id": fresh_confirmation["confirmation_id"],
                    "manifest_ref": second_prepared["manifest_ref"],
                    "preflight_hash": second_prepared["preflight_hash"],
                    "reason": "installed E2E stale start",
                },
            )
            assert stale_start["error"] == "manifest_stale"
            second_cancel_reason = "installed E2E stale start cancellation"
            second_cancel = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_cancel",
                {
                    "run_id": second_prepared["run_id"],
                    "expected_epoch": second_prepared["epoch"],
                    "reason": second_cancel_reason,
                },
            )
            await _wait_for_prepared_cancellation(
                client,
                second_cancel,
                expected_actor_id=runtime.actor_id,
                expected_requester_id=runtime.actor_id,
                expected_reason=second_cancel_reason,
            )

            # Run 3: fresh confirmation/start and exact public replay.
            third = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_preflight",
            )
            third_prepared = await _wait_for_phase(
                client,
                third["run_id"],
                {"prepared"},
                timeout=480,
                poll_interval=1.0,
            )
            # Exact one-shot fence-loss target (S4.0R v3): only the durable
            # phase=building journal write of THIS run/epoch may trigger the
            # real lease release.
            runtime.fence_loss_target_file.write_text(
                json.dumps(
                    {
                        "run_id": third_prepared["run_id"],
                        "epoch": int(third_prepared["epoch"]),
                    }
                ),
                encoding="utf-8",
            )
            third_confirmation = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_confirm",
                {
                    "run_id": third_prepared["run_id"],
                    "manifest_ref": third_prepared["manifest_ref"],
                    "preflight_hash": third_prepared["preflight_hash"],
                },
            )
            start_arguments = {
                "confirmation_id": third_confirmation["confirmation_id"],
                "manifest_ref": third_prepared["manifest_ref"],
                "preflight_hash": third_prepared["preflight_hash"],
                "reason": "installed E2E fresh recovery",
            }
            crashed_start = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_run",
                start_arguments,
            )
            assert crashed_start["error"] == "global_discovery_recovery_run_failed"
            assert runtime.fail_once_marker.read_text(encoding="ascii") == "1"
            accepted_start = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_run",
                start_arguments,
            )
            assert accepted_start["outcome"] == "accepted"
            assert accepted_start["idempotent_replay"] is True
            assert accepted_start["run_id"] == third_prepared["run_id"]
            assert accepted_start["attempt_id"] == third_prepared["attempt_id"]
            assert accepted_start["epoch"] == third_prepared["epoch"]
            assert accepted_start["actor_id"] == runtime.actor_id
            assert accepted_start["confirmed_by_actor_id"] == runtime.peer_actor_id
            replayed_start = await _tool_payload(
                peer_client,
                "okto_pulse_kg_global_discovery_recovery_run",
                start_arguments,
            )
            assert replayed_start["run_id"] == accepted_start["run_id"]
            assert replayed_start["attempt_id"] == accepted_start["attempt_id"]
            assert replayed_start["idempotent_replay"] is True

            # Deterministic A5 INV-F4 proof (S4.0R v3): the fence-loss
            # injection released the REAL productive writer lease exactly once
            # right after the durable phase=building journal write of THIS
            # run/epoch, so the productive worker itself terminalizes epoch N
            # as PARTIAL with the typed reconciliation-pending reason —
            # awaited HERE, still inside the controlled-clock server, so its
            # shutdown stays clean and bounded.
            partial = await _wait_for_phase(
                client,
                accepted_start["run_id"],
                {"terminal"},
                timeout=300,
                poll_interval=1.0,
            )
            assert partial["state"] == "partial"
            assert partial["terminal_outcome"] == "partial"
            assert partial["reason_code"] == "recovery_physical_reconciliation_pending"
            assert partial["retryable"] is False
            assert partial.get("physical_truth") in (None, "unknown")
            assert int(partial["epoch"]) == int(accepted_start["epoch"])

            assert runtime.fence_loss_signal_file.exists()
            fence_evidence = json.loads(
                runtime.fence_loss_signal_file.read_text(encoding="utf-8")
            )
            assert fence_evidence["released"] is True
            assert fence_evidence["run_id"] == accepted_start["run_id"]
            assert int(fence_evidence["epoch"]) == int(accepted_start["epoch"])
            # Capture the exact N journal truth immediately after the typed
            # PARTIAL: the fenced old owner performed ZERO physical writes
            # after the loss, so the journal is frozen at durable building.
            n_journal_path = Path(fence_evidence["journal_path"])
            n_journal_bytes = n_journal_path.read_bytes()
            n_journal_sha = hashlib.sha256(n_journal_bytes).hexdigest()
            fenced_journal = json.loads(n_journal_bytes.decode("utf-8"))
            assert fenced_journal["phase"] == "building"
            assert fenced_journal["run_id"] == accepted_start["run_id"]
            assert int(fenced_journal["epoch"]) == int(accepted_start["epoch"])

    # Restart from the same installed wheels/data directory and prove the
    # durable control-plane state plus public DLQ operations survive it.
    runtime.clock_file.write_text("0", encoding="ascii")
    dead_letter_id = _seed_terminal_outbox(runtime)
    run_id = accepted_start["run_id"]
    partial_epoch = int(accepted_start["epoch"])
    resume_arguments = {
        "run_id": run_id,
        "expected_epoch": partial_epoch,
        "reason": (
            "installed E2E explicit resume after fence-loss partial (A5 INV-F4)"
        ),
    }
    # Exact one-shot gate target for the successor epoch, written BEFORE the
    # restart server exists (S4.0R ruling).
    runtime.resume_gate_target_file.write_text(
        json.dumps({"run_id": run_id, "epoch": partial_epoch + 1}),
        encoding="utf-8",
    )
    with _running_server(runtime, mode="resume-gate") as restarted:
        async with Client(
            _authenticated_mcp_url(runtime, restarted), timeout=45, init_timeout=45
        ) as client:
            try:
                # Explicit public resume #1 with an auditable reason.
                resumed = await _tool_payload(
                    client,
                    "okto_pulse_kg_global_discovery_recovery_resume",
                    resume_arguments,
                )
                assert "error" not in resumed, resumed
                assert int(resumed["epoch"]) == partial_epoch + 1
                # P1 exact admission projection (v5 ruling).
                assert resumed["state"] == "pending"
                assert resumed["phase"] == "confirmed"
                assert resumed["reason_code"] == "recovery_resume_admitted"

                # The gate signal proves the durable N+1 admission is active
                # (slot + dispatch coherent) and the REAL native operation is
                # held strictly BEFORE any terminal.
                deadline = time.monotonic() + 120
                while not runtime.resume_gate_signal_file.exists():
                    assert time.monotonic() < deadline, "resume gate never signaled"
                    time.sleep(0.1)
                gate_seen = json.loads(
                    runtime.resume_gate_signal_file.read_text(encoding="utf-8")
                )
                assert gate_seen == {"run_id": run_id, "epoch": partial_epoch + 1}

                # Canonical public replay: SAME actor, SAME reason, immediately
                # and strictly before terminal (native held by the gate). Must
                # return without error and preserve the exact persisted N+1
                # identity; ONLY the documented live-projection fields may
                # advance (v5 ruling). ZERO epoch N+2.
                replayed_resume = await _tool_payload(
                    client,
                    "okto_pulse_kg_global_discovery_recovery_resume",
                    resume_arguments,
                )
                assert "error" not in replayed_resume, replayed_resume
                allowed_live = {
                    "state",
                    "progress_seq",
                    "phase",
                    "heartbeat_at",
                    "updated_at",
                    "active_elapsed_ms",
                    "reason_code",
                }
                assert set(replayed_resume) == set(resumed)
                changed = {
                    key for key in resumed if replayed_resume[key] != resumed[key]
                }
                # Everything outside the closed live set — counts, deadlines,
                # cumulative charge, binding/manifest/preflight, confirmation
                # and operator metadata, attempt identity — stays exact.
                assert changed <= allowed_live, changed
                assert {
                    "state",
                    "progress_seq",
                    "phase",
                    "heartbeat_at",
                    "updated_at",
                    "reason_code",
                } <= changed, changed
                assert replayed_resume["state"] == "running"
                assert replayed_resume["phase"] == "cutover"
                assert replayed_resume["reason_code"] == "recovery_cutover_running"
                assert int(replayed_resume["progress_seq"]) > int(
                    resumed["progress_seq"]
                )
                assert replayed_resume["updated_at"] > resumed["updated_at"]
                assert replayed_resume["heartbeat_at"] >= resumed["heartbeat_at"]
                assert int(replayed_resume["active_elapsed_ms"]) >= int(
                    resumed["active_elapsed_ms"]
                )
            finally:
                # Unconditional cooperative release so the server drain is
                # always clean regardless of assertion outcome.
                runtime.resume_gate_release_file.write_text("release", encoding="ascii")

            resumed_terminal = await _wait_for_phase(
                client,
                run_id,
                {"terminal"},
                timeout=600,
                poll_interval=1.0,
            )
            assert int(resumed_terminal["epoch"]) == partial_epoch + 1
            assert resumed_terminal["state"] == "success"
            assert resumed_terminal["terminal_outcome"] == "success"
            assert int(resumed_terminal["supersedes_epoch"]) == partial_epoch
            assert not runtime.resume_gate_timeout_marker.exists()

            # Post-terminal read-only SQL truth: exact epoch set {N, N+1},
            # full supersession lineage and ZERO epoch N+2.
            lineage = _recovery_attempt_rows(runtime, run_id)
            assert [int(row["epoch"]) for row in lineage] == [
                partial_epoch,
                partial_epoch + 1,
            ]
            assert lineage[0]["state"] == "partial"
            assert int(lineage[0]["superseded_by_epoch"]) == partial_epoch + 1
            assert lineage[1]["state"] == "success"
            assert int(lineage[1]["supersedes_epoch"]) == partial_epoch

            # S4.0R v3 journal oracle: the N journal stays BYTE-IDENTICAL
            # through the successor's terminal SUCCESS (the fenced old owner
            # never wrote again), and the successor owns its OWN journal at a
            # DISTINCT path with the exact resumed attempt identity. Because
            # this branch loses the fence before pointer switch, no physical
            # predecessor binding is required on the successor journal.
            assert n_journal_path.read_bytes() == n_journal_bytes
            assert (
                hashlib.sha256(n_journal_path.read_bytes()).hexdigest() == n_journal_sha
            )
            successor_attempt_id = str(resumed["attempt_id"])
            successor_journal_path = (
                n_journal_path.parent.parent
                / successor_attempt_id.split("/")[-1]
                / "recovery_journal.json"
            )
            assert successor_journal_path != n_journal_path
            assert successor_journal_path.is_file(), successor_journal_path
            successor_journal = json.loads(
                successor_journal_path.read_text(encoding="utf-8")
            )
            assert successor_journal["phase"] == "completed"
            assert int(successor_journal["epoch"]) == partial_epoch + 1
            assert successor_journal["run_id"] == run_id
            assert successor_journal.get("attempt_id") in (
                None,
                successor_attempt_id,
            )

            # Exactly ONE stage=recovery dispatch for epoch N+1, bound to the
            # resumed attempt and done: the replay created neither an epoch
            # N+2 nor a duplicate N+1 dispatch (v5 ruling).
            recovery_dispatches = [
                row
                for row in _recovery_dispatch_rows(runtime, run_id)
                if row["stage"] == "recovery" and int(row["epoch"]) == partial_epoch + 1
            ]
            assert len(recovery_dispatches) == 1, recovery_dispatches
            assert recovery_dispatches[0]["attempt_id"] == successor_attempt_id
            assert recovery_dispatches[0]["state"] == "done"

            invalid_classification = await _tool_payload(
                client,
                "okto_pulse_kg_global_outbox_dead_letter_list",
                {"limit": 10, "classification": "not-a-classification"},
            )
            assert invalid_classification["error"] == "invalid_classification"
            empty_selection = await _tool_payload(
                client,
                "okto_pulse_kg_global_outbox_dead_letter_reprocess",
                {
                    "dead_letter_ids": [],
                    "reason": "installed E2E empty selection",
                    "process_now": False,
                },
            )
            assert empty_selection == {"error": "no_dlq_selected", "mutated": False}

            listed = await _tool_payload(
                client,
                "okto_pulse_kg_global_outbox_dead_letter_list",
                {"limit": 10},
            )
            assert [item["dead_letter_id"] for item in listed["items"]] == [
                dead_letter_id
            ]
            reprocessed = await _tool_payload(
                client,
                "okto_pulse_kg_global_outbox_dead_letter_reprocess",
                {
                    "dead_letter_ids": [dead_letter_id],
                    "reason": "installed E2E operator replay",
                    "process_now": False,
                },
            )
            assert reprocessed["requeued_ids"] == [dead_letter_id]
            assert reprocessed["worker_signaled"] is False
            replayed = await _tool_payload(
                client,
                "okto_pulse_kg_global_outbox_dead_letter_reprocess",
                {
                    "dead_letter_ids": [dead_letter_id],
                    "reason": "installed E2E operator replay",
                    "process_now": False,
                },
            )
            assert replayed["already_queued_ids"] == [dead_letter_id]
            verified = await _tool_payload(
                client,
                "okto_pulse_kg_global_outbox_dead_letter_verify",
                {"dead_letter_ids": [dead_letter_id, "installed-e2e-missing"]},
            )
            by_id = {item["dead_letter_id"]: item for item in verified["items"]}
            assert by_id[dead_letter_id]["state"] == "queued"
            assert by_id["installed-e2e-missing"]["state"] == "absent"
            _mark_outbox_applied(runtime, dead_letter_id)
            applied = await _tool_payload(
                client,
                "okto_pulse_kg_global_outbox_dead_letter_verify",
                {"dead_letter_ids": [dead_letter_id]},
            )
            assert applied["items"][0]["state"] == "applied"

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["direct_fastmcp_fn_calls"] == []
    source = Path(__file__).read_text(encoding="utf-8")
    direct_fn_call_marker = ".fn" + "("
    assert direct_fn_call_marker not in source


# ---------------------------------------------------------------------------
# Seq4 installed proofs: Z2 unusable worker inputs and real hard-kill adoption
# ---------------------------------------------------------------------------


def _relocate_global_for_recovery(runtime: InstalledRuntime, label: str) -> None:
    """Declared fixture mutation (global-discovery-total-loss), reapplied with
    the server STOPPED so a new recovery admission is legitimate after an
    earlier successful recovery.  The complete ``global`` tree moves to a
    unique backup inside the isolated root; nothing is deleted."""

    global_dir = runtime.data_dir / "global"
    backup = runtime.root / f"global-discovery-total-loss-{label}"
    assert not backup.exists(), backup
    assert global_dir.exists(), global_dir
    shutil.move(str(global_dir), str(backup))
    global_dir.mkdir()
    assert list(global_dir.iterdir()) == [], sorted(global_dir.iterdir())


def _global_tree_snapshot(runtime: InstalledRuntime) -> dict[str, str]:
    """Complete physical inventory of the observed global tree: every path,
    directories marked, files by SHA256."""

    root = runtime.data_dir / "global"
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        snapshot[rel] = "<dir>" if path.is_dir() else _sha256(path)
    return snapshot


def _attempt_naive_utc(value: object):
    """Deterministic naive-UTC parse for attempts-table timestamps, which are
    persisted as ISO WITH offset (unlike the naive dispatch/poll values)."""

    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(str(value).replace(" ", "T"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _recovery_dispatch_rows(
    runtime: InstalledRuntime, run_id: str
) -> list[dict[str, Any]]:
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT dispatch_id, run_id, attempt_id, epoch, stage, state, "
            "claim_token, worker_id, claimed_at, claim_expires_at, "
            "attempt_count, completed_at "
            "FROM global_discovery_recovery_dispatches "
            "WHERE run_id = ? ORDER BY dispatch_id",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _recovery_attempt_rows(
    runtime: InstalledRuntime, run_id: str
) -> list[dict[str, Any]]:
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT run_id, epoch, attempt_id, state, phase, progress_seq, "
            "cumulative_active_ms, active_elapsed_ms, heartbeat_at, "
            "updated_at, active_deadline_at, attempt_budget_ms, errors, "
            "supersedes_epoch, superseded_by_epoch "
            "FROM global_discovery_recovery_attempts "
            "WHERE run_id = ? ORDER BY epoch",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _recovery_slot_rows(runtime: InstalledRuntime) -> list[dict[str, Any]]:
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM global_discovery_recovery_slots ORDER BY slot_id"
        ).fetchall()
    return [dict(row) for row in rows]


def _recovery_transition_rows(
    runtime: InstalledRuntime, run_id: str
) -> list[dict[str, Any]]:
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM global_discovery_recovery_transitions "
            "WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _stale_surface_snapshot(runtime: InstalledRuntime, run_id: str) -> dict[str, Any]:
    """Full durable surface for the stale-token loser proof: status/attempt,
    dispatches, slot and transitions — byte-identity across a loser call."""

    return {
        "attempts": _recovery_attempt_rows(runtime, run_id),
        "dispatches": _recovery_dispatch_rows(runtime, run_id),
        "slots": _recovery_slot_rows(runtime),
        "transitions": _recovery_transition_rows(runtime, run_id),
    }


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


_STALE_TOKEN_SCRIPT = r"""
import json
import os
from datetime import datetime, timezone

from sqlalchemy import create_engine

from okto_pulse.community.adapters.global_discovery_recovery_worker import (
    SQLAlchemyRecoveryRunStore,
)
from okto_pulse.core.ports.global_discovery_recovery_control import (
    RecoveryDispatchClaimConflict,
    RecoveryProgressCounts,
    RecoveryTerminalOutcome,
    RecoveryWorkerResult,
)

params = json.loads(os.environ["OKTO_E2E_STALE_TOKEN_PARAMS"])


class _Revoker:
    def revoke_prepared(self, **_kwargs):
        return None

    def is_prepared_revoked(self, **_kwargs):
        return False


def _aware(value):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


engine = create_engine(
    "sqlite:///" + params["database_path"].replace("\\", "/"), future=True
)
store = SQLAlchemyRecoveryRunStore(engine=engine, prepared_revoker=_Revoker())
outcome = {"call": params["call"]}
try:
    if params["call"] == "heartbeat":
        store.heartbeat_recovery(
            dispatch_id=params["dispatch_id"],
            claim_token=params["stale_token"],
            observed_at=_aware(params["observed_at"]),
            requested_expires_at=_aware(params["requested_expires_at"]),
            active_elapsed_ms=int(params["active_elapsed_ms"]),
            counts=RecoveryProgressCounts(sources_total=1),
        )
    else:
        store.complete_recovery(
            dispatch_id=params["dispatch_id"],
            claim_token=params["stale_token"],
            expected_progress_seq=int(params["progress_seq"]),
            completed_at=_aware(params["observed_at"]),
            active_elapsed_ms=int(params["active_elapsed_ms"]),
            result=RecoveryWorkerResult(
                outcome=RecoveryTerminalOutcome.SUCCESS,
                reason_code="global_discovery_recovery_completed",
                retryable=False,
                counts=RecoveryProgressCounts(sources_total=1),
            ),
        )
    outcome["result"] = "UNEXPECTED_SUCCESS"
except RecoveryDispatchClaimConflict as exc:
    outcome["result"] = "RecoveryDispatchClaimConflict"
    outcome["run_id"] = getattr(exc, "run_id", None)
    outcome["epoch"] = getattr(exc, "epoch", None)
except Exception as exc:  # noqa: BLE001 - any other type must fail the test
    outcome["result"] = type(exc).__name__
    outcome["detail"] = str(exc)
finally:
    engine.dispose()
print(json.dumps(outcome))
"""


def _recovery_transition_count(runtime: InstalledRuntime, run_id: str) -> int:
    with sqlite3.connect(runtime.database_path, timeout=30) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM global_discovery_recovery_transitions "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )


async def _prepare_confirm(
    client: Client, peer_client: Client
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drive a fresh admission to prepared + issued confirmation over real
    HTTP and return (prepared_status, confirmation)."""

    admitted = await _tool_payload(
        client,
        "okto_pulse_kg_global_discovery_recovery_preflight",
    )
    prepared = await _wait_for_phase(
        peer_client,
        admitted["run_id"],
        {"prepared"},
        timeout=480,
        poll_interval=1.0,
    )
    confirmation = await _tool_payload(
        peer_client,
        "okto_pulse_kg_global_discovery_recovery_confirm",
        {
            "run_id": prepared["run_id"],
            "manifest_ref": prepared["manifest_ref"],
            "preflight_hash": prepared["preflight_hash"],
        },
    )
    assert confirmation["outcome"] == "confirmation_issued"
    return prepared, confirmation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "expected_reason"),
    [
        ("missing", "recovery_worker_inputs_missing"),
        ("corrupt", "recovery_worker_inputs_invalid"),
    ],
)
async def test_installed_z2_unusable_worker_inputs_terminalize_failed(
    installed_runtime: InstalledRuntime,
    variant: str,
    expected_reason: str,
) -> None:
    """Z2 installed: a started attempt whose durable worker-inputs artifact is
    absent or corrupt terminalizes FAILED with the exact typed reason over
    real HTTP, retryable=false, with zero writer lease, zero
    reconcile/cutover and zero physical mutation of the observed global
    tree.  Production creates the artifact INSIDE prepare_durable_start
    (core global_discovery_recovery.py, worker-inputs put), so the declared
    z2-tamper injection corrupts it immediately after the REAL durable put
    and strictly before the recovery dispatch exists."""

    runtime = installed_runtime
    _relocate_global_for_recovery(runtime, f"z2-{variant}")
    for stale in (
        runtime.z2_target_file,
        runtime.z2_signal_file,
        runtime.z2_tripwire_marker,
    ):
        assert not stale.exists(), stale
    with _running_server(runtime, mode="z2-tamper") as server:
        async with (
            Client(
                _authenticated_mcp_url(runtime, server), timeout=45, init_timeout=45
            ) as client,
            Client(
                _authenticated_mcp_url(runtime, server, api_key=runtime.peer_api_key),
                timeout=45,
                init_timeout=45,
            ) as peer_client,
        ):
            prepared, confirmation = await _prepare_confirm(client, peer_client)
            run_id = prepared["run_id"]

            # Target written after prepare+confirm and BEFORE the MCP run:
            # the launcher wrapper lets production's REAL put land durably,
            # then corrupts that exact artifact before the put returns.
            runtime.z2_target_file.write_text(
                json.dumps({"run_id": run_id, "epoch": 1, "variant": variant}),
                encoding="utf-8",
            )
            pre_run_tree = _global_tree_snapshot(runtime)

            accepted = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_run",
                {
                    "confirmation_id": confirmation["confirmation_id"],
                    "manifest_ref": prepared["manifest_ref"],
                    "preflight_hash": prepared["preflight_hash"],
                    "reason": f"installed E2E Z2 {variant} worker inputs",
                },
            )
            assert accepted["outcome"] == "accepted"

            terminal = await _wait_for_phase(
                client, run_id, {"terminal"}, timeout=300, poll_interval=1.0
            )
            assert terminal["state"] == "failed"
            assert terminal["terminal_outcome"] == "failed"
            assert terminal["reason_code"] == expected_reason
            assert terminal["retryable"] is False
            assert terminal.get("physical_truth") is None

    # The injection saw the REAL durable put for exactly this run and
    # corrupted the hash-keyed artifact (worker_inputs_<sha256(run_id)>).
    signal = json.loads(runtime.z2_signal_file.read_text(encoding="utf-8"))
    assert signal["run_id"] == run_id
    assert int(signal["epoch"]) == 1
    assert signal["variant"] == variant
    tampered = Path(signal["artifact_path"])
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    assert run_digest in tampered.name, tampered
    assert "__attempt-1" in tampered.name, tampered
    assert len(signal["original_sha256"]) == 64
    if variant == "missing":
        assert not tampered.exists()
    else:
        assert tampered.read_text(encoding="utf-8") == "{"

    # The failed-inputs attempt never touched the writer lane (tripwire
    # absent => zero writer lease) and the observed global tree is
    # byte-identical => zero reconcile/cutover/physical work.  The input READ
    # itself legitimately happens inside the native wrapper, which fails
    # typed before any lease or physical operation.
    assert not runtime.z2_tripwire_marker.exists()
    assert _global_tree_snapshot(runtime) == pre_run_tree
    attempts = _recovery_attempt_rows(runtime, run_id)
    assert [row["state"] for row in attempts] == ["failed"]
    assert attempts[0]["errors"] >= 1
    # Reset the shared one-shot files for the next parametrized variant.
    runtime.z2_target_file.unlink()
    runtime.z2_signal_file.unlink()


@pytest.mark.asyncio
async def test_installed_hard_kill_at_building_is_adopted_charged_and_completes(
    installed_runtime: InstalledRuntime,
) -> None:
    """Real hard-kill adoption on the SAME installed wheels/data: kill the
    server child at an atomically durable phase=building journal, restart,
    and prove same-run adoption (attempt_count +1, changed worker/token,
    bounded takeover, positive non-double crash charge) through to SUCCESS."""

    from datetime import datetime, timedelta

    runtime = installed_runtime
    _relocate_global_for_recovery(runtime, "hard-kill")
    assert not runtime.building_gate_signal_file.exists()

    with _running_server(runtime, mode="building-gate") as server:
        async with (
            Client(
                _authenticated_mcp_url(runtime, server), timeout=45, init_timeout=45
            ) as client,
            Client(
                _authenticated_mcp_url(runtime, server, api_key=runtime.peer_api_key),
                timeout=45,
                init_timeout=45,
            ) as peer_client,
        ):
            prepared, confirmation = await _prepare_confirm(client, peer_client)
            run_id = prepared["run_id"]
            accepted = await _tool_payload(
                client,
                "okto_pulse_kg_global_discovery_recovery_run",
                {
                    "confirmation_id": confirmation["confirmation_id"],
                    "manifest_ref": prepared["manifest_ref"],
                    "preflight_hash": prepared["preflight_hash"],
                    "reason": "installed E2E hard-kill adoption",
                },
            )
            assert accepted["outcome"] == "accepted"

            deadline = time.monotonic() + 180
            while not runtime.building_gate_signal_file.exists():
                assert time.monotonic() < deadline, "building gate never signaled"
                assert server.process.poll() is None, server.log_tail()
                time.sleep(0.2)

        signal_payload = json.loads(
            runtime.building_gate_signal_file.read_text(encoding="utf-8")
        )
        journal_path = Path(signal_payload["journal_path"])
        assert journal_path.is_file(), journal_path
        killed_journal_bytes = journal_path.read_bytes()
        killed_journal = json.loads(killed_journal_bytes.decode("utf-8"))
        assert killed_journal["phase"] == "building"
        killed_journal_sha = hashlib.sha256(killed_journal_bytes).hexdigest()

        pre_dispatches = _recovery_dispatch_rows(runtime, run_id)
        recovery_pre = [
            row
            for row in pre_dispatches
            if row["stage"] == "recovery" and row["worker_id"] is not None
        ]
        assert len(recovery_pre) == 1, pre_dispatches
        pre = recovery_pre[0]
        pre_attempts = _recovery_attempt_rows(runtime, run_id)
        assert len(pre_attempts) == 1
        pre_attempt = pre_attempts[0]
        pre_cumulative = int(pre_attempt["cumulative_active_ms"])
        pre_active = int(pre_attempt["active_elapsed_ms"])
        pre_heartbeat = _attempt_naive_utc(pre_attempt["heartbeat_at"])
        pre_deadline = _attempt_naive_utc(pre_attempt["active_deadline_at"])
        pre_transitions = _recovery_transition_count(runtime, run_id)

        # Arm the post-commit/pre-run submit gate for the exact adopted
        # run/epoch BEFORE the restart server exists.
        for stale in (
            runtime.submit_gate_target_file,
            runtime.submit_gate_signal_file,
            runtime.submit_gate_release_file,
            runtime.submit_gate_timeout_marker,
        ):
            assert not stale.exists(), stale
        runtime.submit_gate_target_file.write_text(
            json.dumps({"run_id": run_id, "epoch": 1}), encoding="utf-8"
        )

        # Hard kill ONLY the test-created child (isolated root, unique ports).
        assert Path(runtime.root).is_absolute()
        assert "global-recovery-installed-e2e" in runtime.root.as_posix()
        assert server.process.poll() is None
        server.process.kill()
        server.process.wait(timeout=30)

    claim_expires_raw = str(pre["claim_expires_at"])
    claim_expires = datetime.fromisoformat(claim_expires_raw.replace(" ", "T"))
    adoption_bound_seconds = 4.0 + 10.0  # installed poll interval + fixed margin

    assert not runtime.adoption_poll_signal_file.exists()
    with _running_server(runtime, mode="adoption-restart") as restarted:
        try:
            # The submit gate fires AFTER the claim transaction (with the
            # A5R2 crash charge) committed and BEFORE any physical work, so
            # the charge proof below is deterministic, not a racy read.
            deadline = time.monotonic() + 240
            while not runtime.submit_gate_signal_file.exists():
                assert time.monotonic() < deadline, "submit gate never signaled"
                assert restarted.process.poll() is None, restarted.log_tail()
                time.sleep(0.5)
            gate = json.loads(
                runtime.submit_gate_signal_file.read_text(encoding="utf-8")
            )
            assert gate["run_id"] == run_id
            assert int(gate["epoch"]) == 1
            assert int(gate["attempt_count"]) == int(pre["attempt_count"]) + 1
            assert gate["claim_token"] != pre["claim_token"]
            assert gate["worker_id"] != pre["worker_id"]

            adopted_rows = [
                row
                for row in _recovery_dispatch_rows(runtime, run_id)
                if row["stage"] == pre["stage"]
                and int(row["attempt_count"]) == int(pre["attempt_count"]) + 1
            ]
            assert len(adopted_rows) == 1, adopted_rows
            adopted = adopted_rows[0]
            assert str(adopted["claim_token"]) == gate["claim_token"]

            # Bounded takeover on GROUND TRUTH (Codex ruling msg_c640e10d):
            # the adopted claim's persisted ``claimed_at`` must fall after
            # the old claim expiry and within one poll interval plus the
            # fixed margin of whichever is later — the old expiry or the
            # restarted recovery poller's actual first entry.
            poll_started_raw = runtime.adoption_poll_signal_file.read_text(
                encoding="ascii"
            ).strip()
            poll_started = datetime.fromisoformat(poll_started_raw.replace(" ", "T"))
            adopted_claimed_raw = str(adopted["claimed_at"])
            adopted_claimed = datetime.fromisoformat(
                adopted_claimed_raw.replace(" ", "T")
            )
            assert claim_expires <= adopted_claimed, (
                claim_expires_raw,
                adopted_claimed_raw,
            )
            assert adopted_claimed <= max(claim_expires, poll_started) + timedelta(
                seconds=adoption_bound_seconds
            ), (
                adopted_claimed_raw,
                claim_expires_raw,
                poll_started_raw,
            )
            assert adopted["worker_id"] != pre["worker_id"]
            assert adopted["claim_token"] != pre["claim_token"]
            assert int(adopted["epoch"]) == int(pre["epoch"])

            # EXACT exactly-once crash charge (Codex ruling msg_ece5bdb2 /
            # msg_c6a5726c), read AT THE GATE before any new work: the killed
            # owner's window [pre heartbeat, min(old expiry, deadline)] is
            # charged in full, and liveness is rebased on the adopting
            # claim's own claimed_at.
            at_gate_rows = _recovery_attempt_rows(runtime, run_id)
            assert len(at_gate_rows) == 1
            at_gate = at_gate_rows[0]
            charge_through = min(claim_expires, pre_deadline)
            expected_gap_ms = int(
                (charge_through - pre_heartbeat).total_seconds() * 1_000
            )
            assert expected_gap_ms > 0
            gate_active = int(at_gate["active_elapsed_ms"])
            gate_cumulative = int(at_gate["cumulative_active_ms"])
            assert gate_active == pre_active + expected_gap_ms
            assert gate_cumulative == pre_cumulative + expected_gap_ms
            assert gate_cumulative <= 15 * 60 * 1000
            gate_heartbeat = _attempt_naive_utc(at_gate["heartbeat_at"])
            gate_updated = _attempt_naive_utc(at_gate["updated_at"])
            assert gate_heartbeat == adopted_claimed
            assert gate_updated == adopted_claimed

            # R6 (Codex msg_8b48e4c4): the OLD owner's EXACT claim token must
            # be fenced by the INSTALLED store for both heartbeat_recovery
            # and complete_recovery.  The adopted claim is held at the gate,
            # so the durable surface is quiescent and byte-identity across
            # each loser call is deterministic.  Times sit INSIDE the new
            # lease and progress_seq is exact, so the ONLY failing predicate
            # is the stale token.
            adopted_expires = datetime.fromisoformat(
                str(adopted["claim_expires_at"]).replace(" ", "T")
            )
            observed = adopted_claimed + timedelta(seconds=2)
            requested_expiry = observed + timedelta(seconds=10)
            assert observed < adopted_expires
            at_gate_seq = int(at_gate["progress_seq"])
            stale_audit: dict[str, Any] = {
                "run_id": run_id,
                "epoch": 1,
                "dispatch_id": str(pre["dispatch_id"]),
                "stale_token": str(pre["claim_token"]),
                "stale_worker": str(pre["worker_id"]),
                "adopted_token": str(gate["claim_token"]),
                "adopted_worker": str(gate["worker_id"]),
                "adopted_claimed_at": adopted_claimed_raw,
                "adopted_claim_expires_at": str(adopted["claim_expires_at"]),
                "observed_at": observed.isoformat(),
                "progress_seq": at_gate_seq,
                "losers": [],
            }
            for call, extra in (
                (
                    "heartbeat",
                    {"requested_expires_at": requested_expiry.isoformat()},
                ),
                ("complete", {}),
            ):
                before = _stale_surface_snapshot(runtime, run_id)
                params = {
                    "database_path": str(runtime.database_path),
                    "call": call,
                    "dispatch_id": str(pre["dispatch_id"]),
                    "stale_token": str(pre["claim_token"]),
                    "observed_at": observed.isoformat(),
                    "active_elapsed_ms": gate_active,
                    "progress_seq": at_gate_seq,
                    **extra,
                }
                completed = _run_checked(
                    [str(runtime.python), "-I", "-c", _STALE_TOKEN_SCRIPT],
                    cwd=runtime.root,
                    env={
                        **runtime.env,
                        "OKTO_E2E_STALE_TOKEN_PARAMS": json.dumps(params),
                    },
                    timeout=120,
                )
                outcome = json.loads(completed.stdout.strip().splitlines()[-1])
                assert outcome["result"] == "RecoveryDispatchClaimConflict", outcome
                after = _stale_surface_snapshot(runtime, run_id)
                assert after == before
                stale_audit["losers"].append(
                    {
                        "call": call,
                        "params": {
                            key: value
                            for key, value in params.items()
                            if key != "database_path"
                        },
                        "outcome": outcome,
                        "surface_sha256_before": _snapshot_sha256(before),
                        "surface_sha256_after": _snapshot_sha256(after),
                    }
                )
            (runtime.root / "hard-kill-stale-token-audit.json").write_text(
                json.dumps(stale_audit, indent=2), encoding="utf-8"
            )
        finally:
            runtime.submit_gate_release_file.write_text("release", encoding="ascii")

        async with Client(
            _authenticated_mcp_url(runtime, restarted), timeout=45, init_timeout=45
        ) as client:
            terminal = await _wait_for_phase(
                client, run_id, {"terminal"}, timeout=600, poll_interval=1.0
            )
            assert terminal["state"] == "success"
            assert terminal["terminal_outcome"] == "success"
        # The cooperative gate never timed out (it was released by the test).
        assert not runtime.submit_gate_timeout_marker.exists()

    final_attempts = _recovery_attempt_rows(runtime, run_id)
    assert [row["state"] for row in final_attempts] == ["success"]
    final_attempt = final_attempts[0]
    stable_first = int(final_attempt["cumulative_active_ms"])
    # Post-terminal accounting: everything after the gate is the NEW owner's
    # own active time — cumulative and active advanced by the SAME delta, so
    # the crashed gap was charged exactly once and never re-entered.
    assert stable_first - gate_cumulative == (
        int(final_attempt["active_elapsed_ms"]) - gate_active
    )
    time.sleep(1.0)
    stable_second = int(
        _recovery_attempt_rows(runtime, run_id)[0]["cumulative_active_ms"]
    )
    # Stabilized: the SAME crashed gap is never charged twice after terminal.
    assert stable_second == stable_first
    assert _recovery_transition_count(runtime, run_id) > pre_transitions
    # The adopting claim remained the only takeover: attempt_count is stable.
    final_recovery_rows = [
        row
        for row in _recovery_dispatch_rows(runtime, run_id)
        if row["stage"] == pre["stage"]
    ]
    assert [int(row["attempt_count"]) for row in final_recovery_rows] == [
        int(pre["attempt_count"]) + 1
    ]

    final_journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert final_journal["phase"] == "completed"
    assert hashlib.sha256(journal_path.read_bytes()).hexdigest() != killed_journal_sha
    # Orphan/quarantine inventory for the evidence packet: the killed building
    # candidate must not survive outside the quarantine/generations discipline.
    tree = _global_tree_snapshot(runtime)
    stray_tmp = [rel for rel in tree if rel.endswith(".tmp")]
    assert stray_tmp == [], stray_tmp
